import asyncio
import json
import sqlite3
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from backend.database import Database
from backend.models import PROMPT_VERSION, tokenize
from backend.orm import CatalogImage, Meta, Notification, TagJob
from backend.semantic import document
from tests.conftest import publish


async def test_orm_opens_existing_v2_database_without_rewriting_user_data(tmp_path, image):
    path = tmp_path / "catalog.sqlite3"
    ident = 2**63 - 1
    raw = image(ident, tag_names=["赞同", "错误"])
    schema = (Path(__file__).parent / "fixtures/catalog_v2.sql").read_text(encoding="utf-8")
    summary = {
        "export_id": "old-export",
        "catalog_timestamp": "2026-10-27T08:30:00.123456Z",
        "completed_at": 10,
    }
    # Seed through the old TEXT/INTEGER schema rather than using new ORM serializers.
    with sqlite3.connect(path) as legacy:
        legacy.executescript(schema)
        legacy.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [
                ("active_generation", json.dumps("old-generation")),
                ("last_sync", json.dumps(summary)),
                ("semantic_paused", "true"),
            ],
        )
        legacy.execute("INSERT INTO generations VALUES(?,?,?)", ("old-generation", 0, "old-export"))
        legacy.execute(
            "INSERT INTO images VALUES(?,?,?,?,?,?,?,?)",
            (
                "old-generation",
                ident,
                raw["text"],
                raw["rating"],
                raw["imageUrl"],
                raw["imageKey"],
                json.dumps(["赞同", "错误"]),
                json.dumps(raw),
            ),
        )
        legacy.execute(
            "INSERT INTO overrides VALUES(?,?,?,?,?)",
            (ident, '["摸鱼"]', '["错误"]', "人工描述", 1),
        )
        legacy.execute(
            "INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?)",
            (
                ident,
                raw["imageKey"],
                raw["text"],
                "vision",
                PROMPT_VERSION,
                "AI描述",
                '["龙脸"]',
                1,
            ),
        )
        legacy.execute(
            "INSERT INTO notifications(image_id,event,received) VALUES(?,?,?)", (ident, "update", 1)
        )
        legacy.execute(
            "INSERT INTO webhook_logs(received,receipt_id,event,image_id,signature_valid,http_status,body) VALUES(?,?,?,?,?,?,?)",
            (1, 1, "update", str(ident), 1, 204, "{}"),
        )
        legacy.execute(
            "INSERT INTO tag_jobs VALUES(?,?,?,?,?,?,?)",
            (ident, "running", 1, 0, "old-error", "old-lease", 1),
        )
        legacy.execute(
            "INSERT INTO sent_memes(receipt_id,session,conversation,sent_at,image_id,details) VALUES(?,?,?,?,?,?)",
            (
                "receipt",
                "group",
                "conversation",
                1,
                str(ident),
                json.dumps({"image_id": str(ident), "text": "过去发过"}),
            ),
        )
        legacy.execute(
            "INSERT INTO search_index VALUES(?,?,?,?,?,?)",
            (
                "old-generation",
                str(ident),
                tokenize("确实"),
                tokenize("摸鱼"),
                tokenize("赞同 龙脸 摸鱼"),
                tokenize("人工描述"),
            ),
        )
        before = legacy.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    database = Database(path)
    await database.open()
    try:
        item = await database.get_image(ident)
        assert item["id"] == str(ident) and item["raw"]["id"] == str(ident)
        assert item["annotation_fresh"] and item["description"] == "人工描述"
        assert set(item["effective_tags"]) == {"赞同", "龙脸", "摸鱼"}
        assert (await database.search("摸鱼", 5))[0]["id"] == str(ident)
        assert await database.get_meta("last_sync") == summary
        assert await database.get_meta("semantic_paused") is True
        # Frozen output of the v0.4.6 document builder: keep existing vectors reusable.
        assert document((await database.semantic_catalog())[0])["fingerprint"] == (
            "692ebba00bb82ab16d75e950769bd2df17c3a7453e491829ef863bb8a4952f09"
        )
        job = await database.claim_tag()
        assert job["image_id"] == ident and job["attempts"] == 1 and job["lease"] != "old-lease"
        assert len(await database.pending(all_pending=True)) == 1
        assert (await database.logs())["items"][0]["image_id"] == str(ident)
        assert (await database.recent_sent_memes("group", "conversation"))[0]["text"] == "过去发过"
        assert (await database.query_sql("SELECT id,description FROM memes"))["rows"] == [
            [str(ident), "人工描述"]
        ]
        await database.update_override(ident, {"manual_tags": ["迁移后可修改"]})
        assert (await database.search("迁移后可修改", 1))[0]["id"] == str(ident)
    finally:
        await database.close()
    with sqlite3.connect(path) as legacy:
        after = legacy.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        assert before == after
        assert json.loads(legacy.execute("SELECT manual_tags FROM overrides").fetchone()[0]) == [
            "迁移后可修改"
        ]


async def test_batch_failure_rolls_back_rows_and_fts_together(db, image):
    generation = await publish(db, [image(1, text="旧内容")])
    with pytest.raises(ValueError):
        await db.stage_batch(generation, [image(1, text="不应留下的内容"), {"id": 2}])
    assert (await db.get_image(1))["text"] == "旧内容"
    assert await db.search("不应留下", 5) == []
    assert (await db.search("旧内容", 5))[0]["id"] == "1"


async def test_publish_failure_rolls_back_generation_version_and_receipts(db, image, monkeypatch):
    previous_generation = await publish(db, [image(1)])
    previous_summary = await db.get_meta("last_sync")
    receipt = await db.record_webhook(body="{}", valid=True, status=204, event="update", ident=1)
    generation = await db.new_generation()
    await db.stage_batch(generation, [image(2)])
    original = db._acknowledge
    monkeypatch.setattr(db, "_acknowledge", AsyncMock(side_effect=OSError("write failed")))
    with pytest.raises(OSError):
        await db.publish(
            generation, {"export_id": "new", "catalog_timestamp": "2026-10-28T00:00:00Z"}, [receipt]
        )
    assert await db.get_meta("active_generation") == previous_generation
    assert await db.get_meta("last_sync") == previous_summary
    assert len(await db.pending(all_pending=True)) == 1
    assert await db.get_image(2) is None
    monkeypatch.setattr(db, "_acknowledge", original)
    await db.publish(generation, {"export_id": "new"}, [receipt])
    assert await db.get_image(1) is None and await db.get_image(2)
    assert not await db.pending(all_pending=True)


async def test_cancelled_orm_transaction_rolls_back_and_releases_connection(db):
    flushed = asyncio.Event()

    async def writer():
        async with db.transaction() as session:
            session.add(Meta(key="uncommitted", value={"value": "temporary"}))
            session.add(Notification(image_id=1, event="update", received=1))
            await session.flush()
            flushed.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(writer())
    await asyncio.wait_for(flushed.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await db.get_meta("uncommitted") is None
    assert not await db.pending(all_pending=True)
    await db.set_meta("still_usable", True)
    assert await db.get_meta("still_usable") is True


async def test_concurrent_claims_and_stale_leases_do_not_complete_other_jobs(db, image):
    await publish(db, [image(1), image(2)])
    await db.enqueue_tags([1, 2])
    jobs = await asyncio.gather(db.claim_tag(), db.claim_tag(), db.claim_tag())
    claimed = [job for job in jobs if job]
    assert len(claimed) == 2 and {job["image_id"] for job in claimed} == {1, 2}
    wrong = {**claimed[0], "lease": "stale"}
    await db.finish_tag(
        wrong,
        await db.get_image(wrong["image_id"]),
        {"description": "过期结果", "tags": ["过期"]},
        "vision",
    )
    await db.fail_tag(wrong, "过期错误")
    async with db.transaction() as session:
        job = await session.get(TagJob, wrong["image_id"])
        assert job.status == "running" and job.attempts == 0


async def test_model_snapshot_stays_consistent_during_a_catalog_switch(db, image, monkeypatch):
    from backend import sql_query

    await publish(db, [image(1, text="旧快照")])
    started, release = Event(), Event()
    original = sql_query.configure_sqlite

    def configure(engine, **kwargs):
        original(engine, **kwargs)

        @event.listens_for(engine, "after_cursor_execute")
        def pause_after_meta(connection, cursor, statement, parameters, context, many):
            if "FROM meta" in statement and not started.is_set():
                started.set()
                assert release.wait(timeout=5)

    monkeypatch.setattr(sql_query, "configure_sqlite", configure)
    task = asyncio.create_task(db.query_sql("SELECT id,text FROM memes"))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=6)
        await publish(db, [image(2, text="新快照")])
    finally:
        release.set()
    assert (await task)["rows"] == [["1", "旧快照"]]
    assert (await db.query_sql("SELECT id,text FROM memes"))["rows"] == [["2", "新快照"]]


async def test_orm_json_null_booleans_and_int64_roundtrip(db, image):
    await db.set_meta("empty", None)
    await db.set_meta("flag", False)
    await db.set_meta("nested", {"id": 2**63 - 1, "array": [None, False, "龙脸"]})
    assert await db.get_meta("empty", "default") == "default"
    assert await db.get_meta("flag") is False
    assert (await db.get_meta("nested"))["id"] == 2**63 - 1
    await publish(db, [image(2**63 - 1)])
    async with db.transaction() as session:
        row = await session.scalar(select(CatalogImage))
        assert isinstance(row.hub_tags, list) and isinstance(row.raw, dict)
        assert row.id == 2**63 - 1
