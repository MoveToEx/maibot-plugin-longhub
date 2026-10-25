import asyncio
import json

import pytest

from backend.models import PROMPT_VERSION
from tests.conftest import publish


async def test_sql_exact_text_tag_intersection_and_exclusion(db, image):
    await publish(
        db,
        [
            image(1, text="确实", tag_names=["龙", "开心"]),
            image(2, text="确实如此", tag_names=["龙", "开心"]),
            image(3, text="确实", tag_names=["龙", "开心", "哭泣"]),
            image(4, text="确实", tag_names=["开心"]),
        ],
    )
    result = await db.query_sql("""
        SELECT m.id,m.text FROM memes m WHERE m.text='确实'
        AND EXISTS (SELECT 1 FROM meme_tags t WHERE t.image_id=m.id AND t.tag='开心')
        AND EXISTS (SELECT 1 FROM meme_tags t WHERE t.image_id=m.id AND t.tag='龙')
        AND NOT EXISTS (SELECT 1 FROM meme_tags t WHERE t.image_id=m.id AND t.tag='哭泣')
        ORDER BY m.id
    """)
    assert result == {"columns": ["id", "text"], "rows": [["1", "确实"]], "truncated": False}


async def test_sql_effective_tags_descriptions_json_and_big_ids(db, image):
    ident = 2**63 - 1
    await publish(db, [image(ident, tag_names=["龙", "错误", "重复"])])
    await db.conn.execute(
        "INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?)",
        (ident, f"{ident}.gif", "确实", "vision", PROMPT_VERSION, "AI描述", '["AI","重复"]', 0),
    )
    await db.conn.commit()
    await db.update_override(
        ident,
        {"manual_tags": ["人工", "重复"], "excluded_tags": ["错误"], "description": "人工描述"},
    )
    result = await db.query_sql("SELECT id,description,tags,annotation_fresh FROM memes")
    row = result["rows"][0]
    assert row[0] == str(ident) and row[1] == "人工描述" and row[3] == 1
    assert set(json.loads(row[2])) == {"龙", "重复", "AI", "人工"}
    assert (await db.query_sql("SELECT COUNT(*) FROM meme_tags"))["rows"] == [[4]]
    assert (
        await db.query_sql("SELECT m.id FROM memes m,json_each(m.tags) t WHERE t.value='人工'")
    )["rows"] == [[str(ident)]]
    assert (await db.query_sql("SELECT CAST(id AS INTEGER) FROM memes"))["rows"] == [[str(ident)]]
    await db.update_override(ident, {"description": ""})
    assert (await db.query_sql("SELECT description FROM memes"))["rows"] == [[""]]


async def test_sql_hides_staging_disabled_and_restricted_ratings(db, image):
    await publish(db, [image(1), image(2), image(3, rating="violent")])
    await db.update_override(2, {"enabled": False})
    staging = await db.new_generation()
    await db.stage_batch(staging, [image(4, text="未发布")])
    result = await db.query_sql("SELECT id FROM memes ORDER BY id", ratings=["none"])
    assert result["rows"] == [["1"]]
    assert (await db.query_sql("SELECT COUNT(*) FROM memes", ratings=[]))["rows"] == [[0]]
    assert (await db.query_sql("SELECT DISTINCT image_id FROM meme_tags", ratings=["none"]))[
        "rows"
    ] == [["1"]]


async def test_sql_with_join_aggregate_and_comments(db, image):
    await publish(db, [image(1), image(2), image(3, tag_names=["别的"])])
    result = await db.query_sql("""-- count exact tags
        /* ordinary SQLite CTE */ WITH chosen AS (
            SELECT image_id FROM meme_tags WHERE tag='赞同'
        )
        SELECT rating,COUNT(*) AS n FROM memes m JOIN chosen c ON c.image_id=m.id
        GROUP BY rating HAVING COUNT(*)>=2;
    """)
    assert result["columns"] == ["rating", "n"] and result["rows"] == [["none", 2]]


async def test_sql_random_selection_respects_sendable_scope_and_optional_tags(db, image):
    await publish(
        db,
        [image(1, tag_names=["开心"]), image(2), image(3, rating="violent"), image(4)],
    )
    await db.update_override(4, {"enabled": False})
    staging = await db.new_generation()
    await db.stage_batch(staging, [image(99)])
    result = await db.query_sql("SELECT id FROM memes ORDER BY RANDOM() LIMIT 1", ratings=["none"])
    assert len(result["rows"]) == 1 and result["rows"][0][0] in {"1", "2"}
    result = await db.query_sql(
        """SELECT m.id FROM memes m WHERE EXISTS (
        SELECT 1 FROM meme_tags t WHERE t.image_id=m.id AND t.tag IN ('happy','开心'))
        ORDER BY RANDOM() LIMIT 1""",
        ratings=["none"],
    )
    assert result["rows"] == [["1"]]


async def test_sql_reads_committed_changes_without_stale_cache(db, image):
    generation = await publish(db, [image(1)])
    assert (await db.query_sql("SELECT id FROM memes"))["rows"] == [["1"]]
    await db.update_override(1, {"enabled": False})
    assert not (await db.query_sql("SELECT id FROM memes"))["rows"]
    await db.update_override(1, {"enabled": True, "manual_tags": ["新标签"]})
    assert (await db.query_sql("SELECT image_id FROM meme_tags WHERE tag='新标签'"))["rows"] == [
        ["1"]
    ]
    await db.refresh(generation, 1, None)
    assert (await db.query_sql("SELECT COUNT(*) FROM memes"))["rows"] == [[0]]
    await publish(db, [image(2)])
    assert (await db.query_sql("SELECT id FROM memes"))["rows"] == [["2"]]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM memes",
        "UPDATE memes SET text='changed'",
        "CREATE TABLE stolen AS SELECT * FROM memes",
        "WITH t AS (SELECT 1) DELETE FROM memes",
        "PRAGMA query_only=OFF",
        "ATTACH DATABASE ':memory:' AS other",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE memes",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM pragma_database_list",
        "SELECT * FROM pragma_table_info('memes')",
        "SELECT * FROM catalog.images",
        "SELECT * FROM images",
        "SELECT * FROM meta",
        "SELECT * FROM webhook_logs",
        "SELECT * FROM annotations",
        "SELECT * FROM sent_memes",
        "SELECT raw FROM memes",
        "WITH memes AS (SELECT * FROM catalog.images) SELECT * FROM memes",
        "SELECT load_extension('nope')",
        "SELECT readfile('catalog.sqlite3')",
        "SELECT writefile('test.txt','changed')",
        "SELECT randomblob(1000000000)",
    ],
)
async def test_sql_cannot_modify_or_escape_public_snapshot(db, image, sql):
    await publish(db, [image()])
    await db.set_meta("private", "private-value")
    with pytest.raises(ValueError):
        await db.query_sql(sql)
    assert (await db.get_image(1))["text"] == "确实"
    assert await db.get_meta("private") == "private-value"
    # A rejected query must not poison the normal writer or subsequent queries.
    await db.update_override(1, {"description": "仍可写"})
    assert (await db.query_sql("SELECT description FROM memes"))["rows"] == [["仍可写"]]


async def test_sql_limits_and_empty_results(db, image):
    await publish(db, [image(i) for i in range(1, 6)])
    result = await db.query_sql("SELECT id FROM memes ORDER BY id", limit=2)
    assert result["rows"] == [["1"], ["2"]] and result["truncated"]
    assert not (await db.query_sql("SELECT id FROM memes LIMIT 2", limit=2))["truncated"]
    empty = await db.query_sql("SELECT id,text FROM memes WHERE text='不存在'")
    assert empty == {"columns": ["id", "text"], "rows": [], "truncated": False}
    # Duplicate SQL column names stay unambiguous because rows use arrays.
    assert (await db.query_sql("SELECT 1 AS x,2 AS x"))["rows"] == [[1, 2]]
    with pytest.raises(ValueError, match="过大"):
        await db.query_sql("""WITH RECURSIVE t(x) AS (
            SELECT '龙' UNION ALL SELECT x||x FROM t WHERE length(x)<16384
        ) SELECT x FROM t WHERE length(x)=16384""")


async def test_sql_execution_budget_and_recovery(db, monkeypatch):
    from backend import sql_query

    monkeypatch.setattr(sql_query, "MAX_QUERY_STEPS", 5000)
    with pytest.raises(ValueError, match="执行限额"):
        await db.query_sql("""WITH RECURSIVE n(x) AS (
            SELECT 1 UNION ALL SELECT x+1 FROM n
        ) SELECT sum(x) FROM n""")
    assert (await db.query_sql("SELECT count(*) FROM memes"))["rows"] == [[0]]


async def test_sql_result_byte_budget_does_not_silently_shorten_cells(db, image):
    text = "龙" * 3000
    await publish(db, [image(i, text=text) for i in range(1, 10)])
    result = await db.query_sql("SELECT id,text FROM memes ORDER BY id", limit=50)
    assert result["truncated"]
    assert 0 < len(result["rows"]) < 9
    assert all(row[1] == text for row in result["rows"])
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 32768


async def test_sql_semicolons_in_literals_are_data_and_errors_are_recoverable(db):
    assert (await db.query_sql("SELECT 'a; DROP TABLE memes' AS text; -- a comment"))["rows"] == [
        ["a; DROP TABLE memes"]
    ]
    with pytest.raises(ValueError):
        await db.query_sql("SELECT bad syntax FROM memes")
    with pytest.raises(ValueError):
        await db.query_sql("SELECT CAST('text' AS BLOB)")
    with pytest.raises(ValueError):
        await db.query_sql("SELECT 1e999")
    assert (await db.query_sql("SELECT NULL,1,1.5"))["rows"] == [[None, 1, 1.5]]


@pytest.mark.parametrize(
    ("sql", "limit"),
    [
        ("", 10),
        (None, 10),
        ("SELECT 1", True),
        ("SELECT 1", 0),
        ("SELECT 1", 51),
        ("SELECT 1", 1.5),
        ("SELECT '" + "龙" * 3000 + "'", 10),
    ],
)
async def test_sql_argument_validation(db, sql, limit):
    with pytest.raises(ValueError):
        await db.query_sql(sql, limit)


async def test_sql_runs_off_event_loop_and_leaves_writer_usable(db, image, monkeypatch):
    from backend import database

    original = database.query_catalog
    started = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def query(*args):
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
        return original(*args)

    monkeypatch.setattr(database, "query_catalog", query)
    await publish(db, [image()])
    task = asyncio.create_task(db.query_sql("SELECT id FROM memes"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await db.update_override(1, {"description": "写入不被查询阻塞"})
    finally:
        release.set()
        await task
