"""Run model-authored SELECTs against an isolated, public catalog snapshot."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path
from threading import Event

from .models import PROMPT_VERSION

MAX_SQL_BYTES = 8192
MAX_RESULT_BYTES = 32768
QUERY_SECONDS = 1.0
SNAPSHOT_SECONDS = 10.0
MAX_QUERY_STEPS = 2_000_000

FUNCTIONS = frozenset(
    "abs avg char coalesce concat concat_ws count cume_dist dense_rank first_value glob "
    "group_concat hex ifnull iif instr json json_array json_array_length json_error_position "
    "json_extract json_group_array json_group_object json_object json_quote json_type json_valid "
    "lag last_value lead length like likelihood likely lower ltrim max min nth_value ntile "
    "nullif percent_rank random rank replace round row_number rtrim sign substr substring "
    "sum total trim typeof unicode unlikely upper".split()
)


def validate_query(sql, limit):
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("请输入 SQL 查询语句")
    if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise ValueError("SQL 最多 8192 字节")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit 须为 1–50 的整数")
    # This is just a helpful syntax check. SQLite's authorizer enforces permissions.
    head = sql.lstrip()
    while head.startswith(("--", "/*")):
        if head.startswith("--"):
            _, _, head = head.partition("\n")
        else:
            _, _, head = head.partition("*/")
        head = head.lstrip()
    if not re.match(r"(?:SELECT|WITH)\b", head, re.I):
        raise ValueError("只允许单条 SELECT 或 WITH … SELECT 查询")


def _authorize(action, name, column, database, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        # COUNT(*) may report a table read with an empty column and no database name.
        if database in ("main", None) and name in ("memes", "meme_tags"):
            return sqlite3.SQLITE_OK
        if name == "json_each":
            return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and column in FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _json_value(value):
    if isinstance(value, bytes) or isinstance(value, float) and not math.isfinite(value):
        raise ValueError("查询结果须为文本、有限数值或 null，请勿返回二进制数据")
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    return value


def query_catalog(path: Path, sql: str, limit: int, ratings: tuple[str, ...], stop: Event):
    validate_query(sql, limit)
    conn = sqlite3.connect(":memory:", uri=True, timeout=2)
    deadline = time.monotonic() + SNAPSHOT_SECONDS
    try:
        conn.set_progress_handler(lambda: int(stop.is_set() or time.monotonic() >= deadline), 1000)
        conn.execute("PRAGMA temp_store=MEMORY")
        # Copy only sendable metadata in one SQLite read snapshot. The source is
        # opened read-only and detached BEFORE any model SQL is prepared. Neither
        # CTE aliases nor schema tricks can expose private tables or staging data.
        conn.execute("ATTACH DATABASE ? AS catalog", (path.resolve().as_uri() + "?mode=ro",))
        conn.execute("""CREATE TABLE memes(
            id TEXT PRIMARY KEY, text TEXT NOT NULL, rating TEXT NOT NULL,
            description TEXT NOT NULL, tags TEXT NOT NULL, annotation_fresh INTEGER NOT NULL)
        """)
        placeholders = ",".join("?" for _ in ratings) or "NULL"
        conn.execute(
            f"""INSERT INTO memes
            SELECT CAST(i.id AS TEXT),i.text,i.rating,COALESCE(o.description,a.description,''),
                (SELECT json_group_array(tag) FROM (
                    SELECT value AS tag FROM json_each(i.hub_tags)
                    UNION SELECT value FROM json_each(COALESCE(a.tags,'[]'))
                    UNION SELECT value FROM json_each(COALESCE(o.manual_tags,'[]'))
                    EXCEPT SELECT value FROM json_each(COALESCE(o.excluded_tags,'[]'))
                )),
                CASE WHEN a.image_key=i.image_key AND a.source_text=i.text
                    AND a.prompt_version=? THEN 1 ELSE 0 END
            FROM catalog.images i
            LEFT JOIN catalog.overrides o ON o.image_id=i.id
            LEFT JOIN catalog.annotations a ON a.image_id=i.id
            WHERE i.generation=(SELECT json_extract(value,'$') FROM catalog.meta
                WHERE key='active_generation')
                AND COALESCE(o.enabled,1)=1 AND i.rating IN ({placeholders})""",
            (PROMPT_VERSION, *ratings),
        )
        conn.commit()
        conn.execute("DETACH DATABASE catalog")
        conn.execute("""CREATE TABLE meme_tags(
            image_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(image_id,tag))""")
        conn.execute("""INSERT INTO meme_tags
            SELECT m.id,t.value FROM memes m,json_each(m.tags) t""")
        conn.execute("CREATE INDEX meme_tags_by_tag ON meme_tags(tag,image_id)")
        conn.commit()
        # Initialize JSON's virtual table before installing the deny-by-default authorizer.
        conn.execute("SELECT value FROM json_each('[]')").close()
        conn.execute("PRAGMA query_only=ON")
        for category, value in (
            (sqlite3.SQLITE_LIMIT_LENGTH, 65536),
            (sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES),
            (sqlite3.SQLITE_LIMIT_COLUMN, 32),
            (sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 50),
            (sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 10),
            (sqlite3.SQLITE_LIMIT_ATTACHED, 0),
            (sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 1000),
        ):
            conn.setlimit(category, value)
        conn.set_authorizer(_authorize)
        deadline = time.monotonic() + QUERY_SECONDS
        steps = 0

        def progress():
            nonlocal steps
            steps += 1000
            return int(stop.is_set() or steps >= MAX_QUERY_STEPS or time.monotonic() >= deadline)

        conn.set_progress_handler(progress, 1000)
        cursor = conn.execute(sql)
        columns = [item[0] for item in cursor.description]
        result = {"columns": columns, "rows": [], "truncated": False}
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        for row in cursor:
            if len(result["rows"]) == limit:
                result["truncated"] = True
                break
            values = [_json_value(value) for value in row]
            row_size = len(json.dumps(values, ensure_ascii=False).encode("utf-8")) + 2
            if size + row_size > MAX_RESULT_BYTES:
                if not result["rows"]:
                    raise ValueError("单行结果过大，请减少查询字段或用 substr 限制文本长度")
                result["truncated"] = True
                break
            result["rows"].append(values)
            size += row_size
        return result
    except sqlite3.Error as exc:
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise ValueError("SQL 超出执行限额或已取消，请缩小查询范围") from None
        # Do not echo filesystem paths from errors that occur while opening the source.
        if getattr(exc, "sqlite_errorcode", None) in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_BUSY):
            raise ValueError("目录暂时不可读取，请稍后重试") from None
        raise ValueError(f"SQL 查询被拒绝或执行失败：{exc}") from None
    finally:
        conn.close()
