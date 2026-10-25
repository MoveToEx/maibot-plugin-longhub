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
