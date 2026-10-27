import pytest

from backend.models import Settings
from backend.sync import SyncEngine
from tests.conftest import publish


async def receipt(db, ident=1, event="update"):
    return await db.record_webhook(body="{}", valid=True, status=204, event=event, ident=ident)


async def test_replacement_preserves_overrides_and_int64(db, image):
    huge = 2**63 - 1
    await publish(db, [image(1), image(huge)])
    await db.update_override(
        huge,
        {
            "manual_tags": ["摸鱼", "人工标签"],
            "excluded_tags": ["approving"],
            "description": "人工描述",
            "enabled": False,
        },
    )
    await publish(db, [image(huge, text="更新后的文字")])
    assert await db.get_image(1) is None
    item = await db.get_image(huge)
    assert item["id"] == str(huge)
    assert item["description"] == "人工描述" and not item["enabled"]
    assert item["effective_tags"] == ["赞同", "摸鱼", "人工标签"]
    assert item["raw"]["id"] == str(huge)
    assert not await db.search("摸鱼", 5)
    await db.update_override(huge, {"enabled": True})
    assert (await db.search("今天摸鱼了", 5))[0]["id"] == str(huge)


async def test_rating_filter_applied_before_limit(db, image):
    await publish(db, [image(i, rating="none" if i == 1 else "violent") for i in range(1, 110)])
    result = await db.search("确实", 1, ratings=["none"])
    assert result[0]["id"] == "1"
    assert await db.search("确实", 5, ratings=[]) == []


async def test_annotation_keeps_manual_sources_and_marks_stale(db, image):
    await publish(db, [image()])
    await db.update_override(1, {"manual_tags": ["人工"], "excluded_tags": ["AI屏蔽"]})
    assert await db.enqueue_tags([1], provider_id="vision") == 1
    job = await db.claim_tag()
    await db.finish_tag(
        job, await db.get_image(1), {"description": "模型描述", "tags": ["AI", "AI屏蔽"]}, "vision"
    )
    item = await db.get_image(1)
    assert item["annotation_fresh"] and item["description"] == "模型描述"
    assert "人工" in item["effective_tags"] and "AI屏蔽" not in item["effective_tags"]
    assert await db.enqueue_tags([1], provider_id="vision") == 0
    await publish(db, [image(text="文字已更新")])
    assert not (await db.get_image(1))["annotation_fresh"]
    assert await db.missing_annotation_ids() == [1]


async def test_snapshot_handoff_does_not_ack_later_arrivals(db, image):
    await publish(db, [image(1, text="旧目录"), image(2)])
    first = await receipt(db, 1, "delete")

    class Client:
        reads = 0

        async def catalog_version(self):
            return "2026-10-27T08:30:00.123456Z"

        async def export(self, consume):
            await consume([image(1, text="快照"), image(3)])
            return {"export_id": "new", "record_count": 2}

        async def get_image(self, ident):
            self.reads += 1
            if self.reads == 1:
                await receipt(db, 1, "restore")
                return image(1, text="边界前更新")
            return image(1, text="边界后更新")

    engine = SyncEngine(db, Client(), Settings())
    await engine.sync()
    assert (await db.get_image(1))["text"] == "边界前更新"
    assert await db.get_image(2) is None
    assert await db.get_image(3)
    pending = await db.pending(all_pending=True)
    assert len(pending) == 1 and pending[0]["id"] != first
    assert await engine.consume_once()
    assert (await db.get_image(1))["text"] == "边界后更新"
    assert not await db.pending(all_pending=True)


async def test_failed_export_keeps_active_catalog_and_receipts(db, image):
    old = await publish(db, [image(1, text="旧目录")])
    await receipt(db)

    class Client:
        async def catalog_version(self):
            return "2026-10-27T08:30:00.123456Z"

        async def export(self, consume):
            await consume([image(2)])
            raise ValueError("missing end")

    engine = SyncEngine(db, Client(), Settings())
    with pytest.raises(ValueError):
        await engine.sync()
    assert await db.get_meta("active_generation") == old
    assert await db.get_image(1) and await db.get_image(2) is None
    assert len(await db.pending(all_pending=True)) == 1


async def test_delayed_delete_refreshes_current_state_and_404_removes(db, image):
    await publish(db, [image()])
    await receipt(db, event="delete")

    class Client:
        current = image(text="已经恢复")

        async def get_image(self, ident):
            return self.current

    client = Client()
    engine = SyncEngine(db, client, Settings())
    await engine.consume_once()
    assert (await db.get_image(1))["text"] == "已经恢复"
    client.current = None
    await receipt(db)
    await engine.consume_once()
    assert await db.get_image(1) is None


async def test_failed_refresh_is_not_a_deletion(db, image):
    await publish(db, [image()])
    await receipt(db)

    class Client:
        async def get_image(self, ident):
            raise OSError("temporary failure")

    await SyncEngine(db, Client(), Settings()).consume_once()
    assert await db.get_image(1)
    pending = await db.pending(all_pending=True)
    assert pending[0]["attempts"] == 1 and pending[0]["error"]
    assert not await db.pending()
    await db.retry_notifications()
    assert len(await db.pending()) == 1


async def test_pending_work_survives_log_pruning_and_restart(db, image):
    await publish(db, [image()])
    await receipt(db)
    await db.prune_logs(1, 0)
    assert (await db.logs())["total"] == 0
    assert len(await db.pending(all_pending=True)) == 1
    await db.enqueue_tags([1], provider_id="vision")
    await db.claim_tag()
    await db.close()
    await db.open()
    assert len(await db.pending(all_pending=True)) == 1
    assert await db.claim_tag() is not None
