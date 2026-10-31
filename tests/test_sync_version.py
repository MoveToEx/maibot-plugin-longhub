import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web

from backend.client import HubClient, HubError
from backend.models import Settings
from backend.service import Service
from backend.sync import SyncEngine
from tests.conftest import publish
from tests.test_maibot import mai as mai

STAMP = "2026-10-27T08:30:00.123456Z"
NEW_STAMP = "2026-10-27T08:30:00.123457Z"


@pytest.mark.parametrize("timestamp", [STAMP, None, "2026-10-27T08:30:00Z"])
async def test_version_endpoint_auth_and_fragmented_json(serve, timestamp):
    seen = {}

    async def version(request):
        seen.update(request.headers)
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        raw = json.dumps({"error": None, "data": {"timestamp": timestamp}}).encode()
        for offset in range(0, len(raw), 7):
            await response.write(raw[offset : offset + 7])
            await asyncio.sleep(0)
        return response

    app = web.Application()
    app.router.add_get("/sync/version", version)
    client = HubClient(Settings(api_base=await serve(app), app_key="version-key"))
    await client.open()
    try:
        assert await client.catalog_version() == timestamp
        assert seen["Authorization"] == "Key version-key"
        assert seen["Accept-Encoding"] == "identity"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"error": "upstream error", "data": {"timestamp": STAMP}},
        {"data": None},
        {"data": {}},
        *(
            {"data": {"timestamp": value}}
            for value in [
                0,
                True,
                "",
                "invalid",
                "2026-02-30T08:30:00Z",
                "2026-10-27T08:30:00",
                "2026-10-27T08:30:00+08:00",
            ]
        ),
    ],
)
async def test_version_rejects_missing_or_invalid_timestamp(serve, payload):
    async def version(_):
        return web.json_response(payload)

    app = web.Application()
    app.router.add_get("/sync/version", version)
    client = HubClient(Settings(api_base=await serve(app)))
    await client.open()
    try:
        with pytest.raises(ValueError):
            await client.catalog_version()
    finally:
        await client.close()


@pytest.mark.parametrize("kind", ["oversize", "redirect", "content_type", "json"])
async def test_version_rejects_unsafe_responses(serve, kind):
    redirected = []

    async def version(_):
        if kind == "redirect":
            return web.Response(status=302, headers={"Location": "/redirected"})
        body = json.dumps({"data": {"timestamp": STAMP}})
        if kind == "oversize":
            body += " " * 65536
        if kind == "json":
            body = "not JSON"
        return web.Response(
            text=body, content_type="text/html" if kind == "content_type" else "application/json"
        )

    async def target(_):
        redirected.append(True)
        return web.json_response({"data": {"timestamp": STAMP}})

    app = web.Application()
    app.router.add_get("/sync/version", version)
    app.router.add_get("/redirected", target)
    client = HubClient(Settings(api_base=await serve(app)))
    await client.open()
    try:
        with pytest.raises(ValueError):
            await client.catalog_version()
        assert not redirected
    finally:
        await client.close()


def fake_client(items, timestamp=STAMP):
    async def export(consume):
        await consume(items)
        return {"export_id": "version-export", "record_count": len(items)}

    return SimpleNamespace(
        catalog_version=AsyncMock(return_value=timestamp),
        export=AsyncMock(side_effect=export),
        get_image=AsyncMock(return_value=items[0] if items else None),
    )


@pytest.mark.parametrize("timestamp", [STAMP, None])
async def test_first_sync_records_version_then_identical_check_skips(db, image, timestamp):
    client = fake_client([image()], timestamp)
    engine = SyncEngine(db, client, Settings())
    await engine.sync()
    previous = await db.get_meta("last_sync")
    generation = await db.get_meta("active_generation")
    revision = db.catalog_revision
    assert previous["catalog_timestamp"] == timestamp
    assert previous["api_base"] == Settings().api_base
    await db.set_meta("sync_error", "old failure")
    engine.status = {"status": "queued", "id": "skip-job"}
    await engine.sync()
    assert engine.status["status"] == "done" and engine.status["skipped"]
    assert engine.status["id"] == "skip-job" and engine.status["imported"] == 0
    assert await db.get_meta("active_generation") == generation
    assert await db.get_meta("last_sync") == previous
    assert db.catalog_revision == revision and await db.get_meta("sync_error") is None
    assert client.catalog_version.await_count == 2
    client.export.assert_awaited_once()


async def test_timestamp_precision_and_pre_export_race_are_preserved(db, image):
    client = fake_client([image()])
    engine = SyncEngine(db, client, Settings())
    export = client.export.side_effect

    async def change_during_export(consume):
        client.catalog_version.return_value = NEW_STAMP
        return await export(consume)

    client.export.side_effect = change_during_export
    await engine.sync()
    assert (await db.get_meta("last_sync"))["catalog_timestamp"] == STAMP
    await engine.sync()
    assert (await db.get_meta("last_sync"))["catalog_timestamp"] == NEW_STAMP
    assert client.export.await_count == 2 and client.catalog_version.await_count == 2


async def test_legacy_catalog_without_version_requires_one_full_sync(db, image):
    await publish(db, [image(text="旧目录")])
    client = fake_client([image(text="校准后的目录")], None)
    engine = SyncEngine(db, client, Settings())
    await engine.sync()
    client.export.assert_awaited_once()
    assert (await db.get_image(1))["text"] == "校准后的目录"
    assert "catalog_timestamp" in await db.get_meta("last_sync")


async def test_same_timestamp_from_another_api_source_does_not_skip(db, image):
    client = fake_client([image()])
    await SyncEngine(db, client, Settings()).sync()
    settings = Settings(api_base="https://other-hub.test")
    await SyncEngine(db, client, settings).sync()
    assert client.export.await_count == 2
    assert (await db.get_meta("last_sync"))["api_base"] == settings.api_base


async def test_saved_version_without_an_active_catalog_cannot_skip(db, image):
    client = fake_client([image()])
    engine = SyncEngine(db, client, Settings())
    await engine.sync()
    await db.set_meta("active_generation", None)
    await engine.sync()
    assert client.export.await_count == 2 and await db.get_image(1)


async def test_skip_keeps_webhook_receipts_available_for_the_consumer(db, image):
    client = fake_client([image()])
    engine = SyncEngine(db, client, Settings())
    await engine.sync()
    await db.record_webhook(body="{}", valid=True, status=204, event="update", ident=1)
    await engine.sync()
    assert len(await db.pending(all_pending=True)) == 1
    client.get_image.assert_not_awaited()
    client.get_image.return_value = image(text="通知刷新")
    assert await engine.consume_once()
    assert not await db.pending(all_pending=True)
    assert (await db.get_image(1))["text"] == "通知刷新"
    assert (await db.get_meta("last_sync"))["catalog_timestamp"] == STAMP


async def test_failed_export_does_not_advance_saved_version(db, image):
    client = fake_client([image()])
    engine = SyncEngine(db, client, Settings())
    await engine.sync()
    previous = await db.get_meta("last_sync")
    generation = await db.get_meta("active_generation")
    client.catalog_version.return_value = NEW_STAMP

    async def invalid_export(consume):
        await consume([image(2)])
        raise ValueError("interrupted export")

    client.export.side_effect = invalid_export
    with pytest.raises(ValueError):
        await engine.sync()
    assert await db.get_meta("last_sync") == previous
    assert await db.get_meta("active_generation") == generation
    assert await db.get_image(1) and not await db.get_image(2)


@pytest.mark.parametrize(
    "error",
    [
        HubError(404, "HTTP 404"),
        HubError(500, "HTTP 500"),
        ValueError("bad version"),
        TimeoutError(),
    ],
)
async def test_unavailable_version_falls_back_without_saving_an_unknown_hint(db, image, error):
    client = fake_client([image()])
    engine = SyncEngine(db, client, Settings())
    await engine.sync()
    client.catalog_version.side_effect = error
    await engine.sync()
    assert client.export.await_count == 2
    assert "catalog_timestamp" not in await db.get_meta("last_sync")
    client.catalog_version.side_effect = None
    await engine.sync()
    assert client.export.await_count == 3


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_version_auth_or_rate_limit_does_not_start_a_full_download(db, status):
    client = fake_client([])
    client.catalog_version.side_effect = HubError(status, "HTTP error", retry_after=15)
    engine = SyncEngine(db, client, Settings())
    with pytest.raises(HubError):
        await engine.sync()
    client.export.assert_not_awaited()
    assert engine.status["auth_error"] is (status != 429)
    assert engine.status["retry_after"] == 15
    assert await db.get_meta("last_sync") is None


async def test_force_full_sync_bypasses_identical_version(db, image):
    client = fake_client([image()])
    engine = SyncEngine(db, client, Settings())
    await engine.sync()
    first = await db.get_meta("active_generation")
    await engine.sync(force=True)
    assert client.export.await_count == 2
    assert await db.get_meta("active_generation") != first
    assert not engine.status.get("skipped")


async def test_version_survives_restart_and_manual_checks_keep_the_job_id(tmp_path):
    settings = Settings(app_key="key", webhook_secret="secret", webhook_ready=True, webhook_port=0)
    for iteration in range(2):
        service = Service(tmp_path, settings, AsyncMock())
        service.client.catalog_version = AsyncMock(return_value=STAMP)
        service.client.export = AsyncMock(return_value={"export_id": "startup", "record_count": 0})
        await service.start()
        try:
            await service.sync_task
            if iteration == 0:
                service.client.export.assert_awaited_once()
            else:
                assert service.sync.status["skipped"]
                job = service.start_sync()
                await service.sync_task
                assert service.sync.status["id"] == job["id"]
                assert service.sync.status["skipped"]
                assert service.next_sync > time.time() and service.sync_failures == 0
                service.client.export.assert_not_awaited()
                service.start_sync(force=True)
                await service.sync_task
                service.client.export.assert_awaited_once()
        finally:
            await service.close()


@pytest.mark.parametrize("force", [False, True])
async def test_sync_api_forwards_force_boolean(mai, force):
    plugin, _ = mai
    plugin.service.start_sync = Mock(return_value={"status": "queued"})
    await plugin.panel_write("/sync", json.dumps({"force": force}))
    plugin.service.start_sync.assert_called_once_with(force=force)


@pytest.mark.parametrize("force", ["true", 1, None])
async def test_sync_api_rejects_nonboolean_force(mai, force):
    plugin, _ = mai
    plugin.service.start_sync = Mock()
    with pytest.raises(ValueError):
        await plugin.panel_write("/sync", json.dumps({"force": force}))
    plugin.service.start_sync.assert_not_called()
