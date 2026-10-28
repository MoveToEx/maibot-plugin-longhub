"""Run model-authored SELECTs against an isolated, public catalog snapshot."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path
from threading import Event

from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool, StaticPool

from .orm import (
    Annotation,
    CatalogImage,
    Meta,
    Override,
    PublicBase,
    PublicMeme,
    PublicMemeTag,
    configure_sqlite,
    image_view,
    json_dump,
)

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


def _copy_catalog(path, target, ratings, stop, deadline):
    if not ratings:
        return
    source = create_engine(
        "sqlite://",
        creator=lambda: sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2),
        poolclass=NullPool,
    )
    configure_sqlite(source, readonly=True)
    try:
        with Session(source) as session, session.begin():
            driver = session.connection().connection.driver_connection
            driver.set_progress_handler(
                lambda: int(stop.is_set() or time.monotonic() >= deadline), 1000
            )
            active = session.get(Meta, "active_generation")
            if active is None or active.value is None:
                return
            statement = (
                select(CatalogImage, Override, Annotation)
                .outerjoin(Override, Override.image_id == CatalogImage.id)
                .outerjoin(Annotation, Annotation.image_id == CatalogImage.id)
                .where(
                    CatalogImage.generation == active.value,
                    CatalogImage.rating.in_(ratings),
                    func.coalesce(Override.enabled, True).is_(True),
                )
                .execution_options(yield_per=128)
            )
            for partition in session.execute(statement).partitions():
                if stop.is_set() or time.monotonic() >= deadline:
                    raise ValueError("目录快照超出执行限额或已取消，请稍后重试")
                memes, tags = [], []
                for row in partition:
                    view = image_view(*row)
                    effective = sorted(view["effective_tags"])
                    memes.append(
                        {
                            "id": view["id"],
                            "text": view["text"],
                            "rating": view["rating"],
                            "description": view["description"],
                            "tags": effective,
                            "annotation_fresh": view["annotation_fresh"],
                        }
                    )
                    tags.extend({"image_id": view["id"], "tag": tag} for tag in effective)
                target.execute(insert(PublicMeme), memes)
                if tags:
                    target.execute(insert(PublicMemeTag), tags)
    except SQLAlchemyError:
        raise ValueError("目录暂时不可读取，请稍后重试") from None
    finally:
        source.dispose()


def query_catalog(path: Path, sql: str, limit: int, ratings: tuple[str, ...], stop: Event):
    validate_query(sql, limit)
    conn = sqlite3.connect(":memory:", uri=True, timeout=2)
    engine = create_engine(
        "sqlite://", creator=lambda: conn, poolclass=StaticPool, json_serializer=json_dump
    )
    deadline = time.monotonic() + SNAPSHOT_SECONDS
    try:
        conn.set_progress_handler(lambda: int(stop.is_set() or time.monotonic() >= deadline), 1000)
        conn.execute("PRAGMA temp_store=MEMORY")
        # Close the independent read-only source BEFORE preparing any model SQL.
        # Private tables and staging data never enter the ORM-built public snapshot.
        with engine.begin() as target:
            PublicBase.metadata.create_all(target)
            _copy_catalog(path, target, ratings, stop, deadline)
            # Initialize JSON's virtual table before installing the authorizer.
            target.execute(select(func.json_each("[]").table_valued("value").c.value)).close()
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
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
        engine.dispose()
        conn.close()
