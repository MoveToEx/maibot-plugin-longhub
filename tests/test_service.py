import asyncio
import hashlib
import hmac
from unittest.mock import AsyncMock

import aiohttp

from backend.database import Database
from backend.models import Settings
from backend.service import Service
from tests.conftest import publish


async def test_startup_sync_and_job_identity(tmp_path):
    settings = Settings(app_key="key", webhook_secret="secret", webhook_ready=True, webhook_port=0)
    service = Service(tmp_path, settings, AsyncMock())
    service.client.export = AsyncMock(return_value={"export_id": "startup", "record_count": 0})
    await service.start()
    try:
        job = service.start_sync()
        await service.sync_task
        status = await service.status()
        assert status["sync"]["status"] == "done"
        assert status["sync"]["id"] == job["id"]
        assert status["last_sync"]["export_id"] == "startup"
        assert service.client.export.await_count == 1
    finally:
        await service.close()


async def test_cancellation_keeps_catalog_and_accepts_notifications_during_export(tmp_path, image):
    path = tmp_path / "catalog.sqlite3"
    seed = Database(path)
    await seed.open()
    old = await publish(seed, [image(text="旧目录")])
    await seed.close()
    settings = Settings(app_key="key", webhook_secret="secret", webhook_ready=True, webhook_port=0)
    service = Service(tmp_path, settings, AsyncMock())
    started = asyncio.Event()

    async def export(consume):
        await consume([image(2)])
        started.set()
        await asyncio.Event().wait()

    service.client.export = export
    await service.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        site = next(iter(service.webhook.runner.sites))
        port = site._server.sockets[0].getsockname()[1]
        body = b'{"event":"update","imageId":1}'
        signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{port}/hooks/longhub",
                data=body,
                headers={"X-Signature": signature},
            ) as response:
                assert response.status == 204
        assert (await service.db.get_image(1))["text"] == "旧目录"
        assert len(await service.db.pending(all_pending=True)) == 1
    finally:
        await service.close()
    restored = Database(path)
    await restored.open()
    try:
        assert await restored.get_meta("active_generation") == old
        assert await restored.get_image(2) is None
        assert len(await restored.pending(all_pending=True)) == 1
    finally:
        await restored.close()


async def test_unconfigured_startup_still_exposes_status_without_network(tmp_path):
    service = Service(tmp_path, Settings(webhook_port=0), AsyncMock())
    service.client.export = AsyncMock()
    await service.start()
    try:
        status = await service.status()
        assert not status["ready"] and status["receiver"]["started"]
        service.client.export.assert_not_awaited()
    finally:
        await service.close()


async def test_auto_enqueue_tracks_current_missing_annotations_and_persists(tmp_path, image):
    settings = Settings(vision_provider_id="vision", webhook_port=0)
    service = Service(tmp_path, settings, AsyncMock())
    await service.db.open()
    try:
        await publish(service.db, [image(1), image(2)])
        await service.db.update_override(2, {"description": "已有人工标注"})
        assert await service.auto_enqueue_once() == 0
        await service.set_auto_tagging(True)
        assert await service.auto_enqueue_once() == 1
        assert await service.auto_enqueue_once() == 0
        job = await service.db.claim_tag()
        for _ in range(3):
            await service.db.fail_tag(job, "模型失败")
            await service.db.conn.execute("UPDATE tag_jobs SET next_attempt=0")
            await service.db.conn.commit()
            job = await service.db.claim_tag()
            if job is None:
                break
        assert (await service.db.get_image(1))["tag_job"]["status"] == "failed"
        generation = await service.db.get_meta("active_generation")
        await service.db.refresh(generation, 3, image(3))
        assert await service.auto_enqueue_once() == 1
        assert (await service.db.get_image(1))["tag_job"]["status"] == "failed"
        assert (await service.bulk_tags(force=True))["queued"] == 1
        assert (await service.bulk_tags(force=True))["queued"] == 0
    finally:
        await service.close()
    resumed = Service(tmp_path, settings, AsyncMock())
    await resumed.db.open()
    try:
        assert (await resumed.status())["tagging_auto"]
        await resumed.set_auto_tagging(False)
        generation = await resumed.db.get_meta("active_generation")
        await resumed.db.refresh(generation, 4, image(4))
        assert await resumed.auto_enqueue_once() == 0
        assert (await resumed.db.get_image(4))["tag_job"] is None
    finally:
        await resumed.close()


async def test_auto_enqueue_worker_wakes_without_starting_model_calls(tmp_path, image):
    service = Service(tmp_path, Settings(vision_provider_id="vision"), AsyncMock())
    await service.db.open()
    queued = asyncio.Event()
    enqueue = service.db.enqueue_tags

    async def enqueue_and_signal(*args, **kwargs):
        count = await enqueue(*args, **kwargs)
        queued.set()
        return count

    service.db.enqueue_tags = enqueue_and_signal
    worker = asyncio.create_task(service._auto_tag_worker())
    try:
        await publish(service.db, [image()])
        await service.set_auto_tagging(True)
        await asyncio.wait_for(queued.wait(), timeout=2)
        assert (await service.db.get_image(1))["tag_job"]["status"] == "queued"
        service.tagger.generate.assert_not_awaited()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await service.close()
