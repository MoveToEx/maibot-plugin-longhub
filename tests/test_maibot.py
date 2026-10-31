"""MaiBot contracts exercised through the installed SDK and real SQLite stores."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from maibot_sdk.context import PluginContext, PluginPaths
from PIL import Image

from backend.database import Database
from backend.models import Settings
from tests.conftest import publish
from tools.migrate_astrbot import migrate

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "longhub_maibot_test", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)]
)
ENTRY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ENTRY
SPEC.loader.exec_module(ENTRY)
ADAPTER = sys.modules["longhub_maibot_test.maibot_adapter.plugin"]
PROVIDERS = sys.modules["longhub_maibot_test.maibot_adapter.providers"]
VECTOR = sys.modules["longhub_maibot_test.maibot_adapter.vector_store"]
POLICY = sys.modules["longhub_maibot_test.maibot_adapter.policy"]
ERROR = sys.modules["longhub_maibot_test.backend.vector_store"].SemanticError
SEMANTIC = sys.modules["longhub_maibot_test.backend.semantic"].SemanticIndex


def snapshot(ident="user-1", *, kind="UserMessageItem", text="好开心"):
    return {
        "item_type": kind,
        "meta": {
            "item_id": ident,
            "logical_turn_id": None,
            "timestamp": "2026-10-31T00:00:00+00:00",
        },
        "parts": [{"type": "text", "text": text}],
    }


class Host:
    def __init__(self):
        self.plugin = None
        self.calls = []
        self.model = "embedding-test"
        self.failure = False
        self.send_mode = "success"

    async def rpc(self, method, plugin_id, payload, **kwargs):
        assert method == "cap.call" and plugin_id == "movetoex.longhub"
        capability, args = payload["capability"], payload["args"]
        self.calls.append((capability, args))
        if capability == "llm.embed":
            if self.failure:
                return {"success": False, "error": "private upstream error"}

            def vector(text):
                return {
                    "embedding": [3.0, 0.0, 0.0]
                    if text == "开心" or "画面与使用场景：开心" in text
                    else [0.0, 2.0, 0.0],
                    "model_name": self.model,
                }

            return {
                "success": True,
                **(
                    {"results": [vector(text) for text in args["texts"]]}
                    if args.get("texts")
                    else vector(args["text"])
                ),
            }
        if capability == "llm.get_available_models":
            return {"success": True, "models": ["vision-test", "vlm"]}
        if capability == "llm.generate":
            return {
                "success": True,
                "response": '{"tags":["开心","龙脸"],"description":"开心的龙脸"}',
            }
        if capability == "maisaka.proactive.trigger":
            return {"success": not self.failure, "task_id": "native-task"}
        if capability == "send.hybrid":
            assert args["sync_to_maisaka_history"] is True
            assert args["maisaka_source_kind"] == "guided_reply"
            segments = [dict(segment) for segment in args["segments"]]
            if self.send_mode == "drop":
                segments = [{"type": "text", "data": "another plugin replaced the image"}]
            elif self.send_mode == "wrong-hash":
                segments[0]["hash"] = "other"
            elif self.send_mode == "wrong-session":
                args = args | {"stream_id": "other-chat"}
            await self.plugin.track_delivery(
                message={"session_id": args["stream_id"], "raw_message": segments},
                sent=self.send_mode != "failed",
            )
            if self.send_mode == "post-send-error":
                raise RuntimeError("host history append failed after confirmed IM delivery")
            return {"success": self.send_mode != "failed", "message_id": "qq-receipt-1"}
        raise AssertionError(capability)


@pytest_asyncio.fixture
async def mai(db, image, tmp_path):
    plugin, host = ENTRY.create_plugin(), Host()
    host.plugin = plugin
    plugin._set_context(
        PluginContext("movetoex.longhub", host.rpc, PluginPaths(tmp_path, tmp_path / "runtime"))
    )
    plugin.service = SimpleNamespace(
        db=db,
        settings=Settings(auto_tagging=False),
        client=SimpleNamespace(image_bytes=AsyncMock(return_value=b"GIF89a-content")),
        semantic=SimpleNamespace(status=AsyncMock(return_value={"available": False})),
        candidates=AsyncMock(return_value=[]),
    )
    await publish(db, [image(1), image(2, text="开心"), image(3, rating="violent")])
    plugin.deliveries.enter("chat-a", "turn-a")
    return plugin, host


def test_real_sdk_registration_and_required_parameters():
    plugin = ENTRY.create_plugin()
    components = {item["name"]: item for item in plugin.get_components()}
    assert set(components) == {
        "search_longhub_memes",
        "query_longhub_memes_sql",
        "search_longhub_memes_semantic",
        "send_longhub_meme",
        "meme",
        "longhub_context",
        "planner_policy",
        "track_delivery",
        "panel_read",
        "panel_write",
    }
    for name, required in [
        ("send_longhub_meme", "image_id"),
        ("search_longhub_memes", "query"),
        ("query_longhub_memes_sql", "sql"),
        ("meme", "image_id"),
    ]:
        metadata = components[name]["metadata"]
        if "parameters_schema" in metadata:
            assert required in metadata["parameters_schema"]["required"]
            assert "stream_id" not in metadata["parameters_schema"]["properties"]
        else:
            assert metadata["parameters_raw"][required]["required"] is True
            assert "stream_id" not in metadata["parameters_raw"]
    assert plugin.build_default_config()["plugin"] == {"enabled": False, "config_version": "1.0.0"}


async def test_planner_preserves_context_and_gates_semantic(mai):
    plugin, _ = mai
    items = [
        snapshot(),
        {
            "item_type": "ProviderOpaqueItem",
            "meta": {"item_id": "opaque"},
            "payload": {"opaque": [1, 2]},
        },
    ]
    tools = [
        {"type": "function", "function": {"name": name}}
        for name in ("reply", "search_longhub_memes_semantic", "query_longhub_memes_sql")
    ]
    result = await plugin.planner_policy(
        items=items, session_id="chat-a", tool_definitions=tools, item_schema_version=1
    )
    kwargs = result["modified_kwargs"]
    assert kwargs["items"][1:] == items
    assert [item["function"]["name"] for item in kwargs["tool_definitions"]] == [
        "reply",
        "query_longhub_memes_sql",
    ]
    assert kwargs["items"][0]["item_type"] == "SystemMessageItem"
    plugin.service.semantic.status.return_value = {"available": True}
    result = await plugin.planner_policy(**kwargs)
    assert len(result["modified_kwargs"]["items"]) == 3
    text = result["modified_kwargs"]["items"][0]["parts"][0]["text"]
    assert all(
        phrase in text
        for phrase in ["有意义的上下文", "genshin_impact", "只发一张图", "movetoex.longhub.meme"]
    )


async def test_search_sql_uses_real_read_only_catalog_and_logs(mai, caplog):
    plugin, _ = mai
    with caplog.at_level(logging.INFO):
        result = await plugin.search_sql("SELECT id FROM memes WHERE id='2'", stream_id="chat-a")
    assert json.loads(result["content"])["rows"] == [["2"]]
    assert "SELECT id" in caplog.text and "query_longhub_memes_sql" in caplog.text
    assert plugin.deliveries.current("chat-a").searched
    with pytest.raises(ValueError):
        await plugin.search_sql("DELETE FROM memes", stream_id="chat-a")


async def test_send_only_native_history_reentrant_receipt_and_dedup(mai):
    plugin, host = mai
    result = await asyncio.wait_for(plugin.send_image_only("1", stream_id="chat-a"), 2)
    assert (
        result["success"]
        and result["stop_after_execution"]
        and result["message_id"] == "qq-receipt-1"
    )
    sent = (await plugin.service.db.recent_sent_memes("chat-a", "maibot"))[0]
    assert sent["image_id"] == "1" and "binary_data_base64" not in json.dumps(sent)
    segment = host.calls[-1][1]["segments"][0]
    assert (
        segment["type"] == "emoji"
        and base64.b64decode(segment["binary_data_base64"]) == b"GIF89a-content"
    )
    with pytest.raises(ValueError, match="重复"):
        await plugin.send_image_only("2", stream_id="chat-a")


@pytest.mark.parametrize("mode", ["drop", "wrong-hash", "wrong-session", "failed"])
async def test_unconfirmed_sends_never_claim_success(mai, mode):
    plugin, host = mai
    host.send_mode = mode
    result = await plugin.send_image_only("1", stream_id="chat-a")
    assert not result["success"] and "stop_after_execution" not in result
    assert await plugin.service.db.recent_sent_memes("chat-a", "maibot") == []


async def test_confirmed_send_with_later_host_error_is_not_retried(mai):
    plugin, host = mai
    host.send_mode = "post-send-error"
    assert (await plugin.send_image_only("1", stream_id="chat-a"))["success"]
    with pytest.raises(ValueError):
        await plugin.send_image_only("1", stream_id="chat-a")


async def test_confirmed_receipt_finishes_persisting_when_caller_is_cancelled(mai):
    plugin, _ = mai
    started, release = asyncio.Event(), asyncio.Event()
    original = plugin.service.db.record_sent_meme

    async def delayed(*args):
        started.set()
        await release.wait()
        await original(*args)

    plugin.service.db.record_sent_meme = delayed
    task = asyncio.create_task(plugin.send_image_only("1", stream_id="chat-a"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(await plugin.service.db.recent_sent_memes("chat-a", "maibot")) == 1
    with pytest.raises(ValueError):
        await plugin.send_image_only("2", stream_id="chat-a")


async def test_semantic_errors_log_details_without_secrets(mai, caplog):
    plugin, _ = mai
    plugin.service.settings = replace(plugin.service.settings, app_key="hub-secret")
    plugin.service.semantic.provider = SimpleNamespace(
        provider_config={"api_key": "provider-secret"}
    )
    plugin.service.semantic.search = AsyncMock(
        side_effect=RuntimeError("upstream failure: hub-secret provider-secret")
    )
    with caplog.at_level(logging.ERROR):
        result = await plugin.search_semantic("开心", stream_id="chat-a")
    assert result["success"] is False and "secret" not in json.dumps(result)
    assert "upstream failure" in caplog.text and "Traceback" in caplog.text
    assert "hub-secret" not in caplog.text and "provider-secret" not in caplog.text


@pytest.mark.parametrize(
    "position,only,expected",
    [
        ("before", False, ["emoji", "text"]),
        ("after", False, ["text", "emoji"]),
        ("after", True, ["emoji"]),
    ],
)
async def test_reply_extension_order_and_only_image(mai, position, only, expected):
    plugin, _ = mai
    parameters = {"image_id": "1", "position": position, "only_image": only}
    messages = [{"segments": [{"type": "text", "data": "配文"}], "quote_previous": True}]
    kwargs = dict(
        parameters=parameters, reply_id="native-reply-1", session_id="chat-a", messages=messages
    )
    assert "extra_prompt" in await plugin.reply_meme(phase="prepare", **kwargs)
    output = (await plugin.reply_meme(phase="before_send", **kwargs))["messages"]
    assert [message["segments"][0]["type"] for message in output] == expected
    if not only:
        assert (
            next(message for message in output if message["segments"][0]["type"] == "text")
            == messages[0]
        )
    assert await plugin.service.db.recent_sent_memes("chat-a", "maibot") == []
    await plugin.track_delivery(
        message={
            "session_id": "chat-a",
            "raw_message": next(
                message["segments"]
                for message in output
                if message["segments"][0]["type"] == "emoji"
            ),
        },
        sent=True,
    )
    assert len(await plugin.service.db.recent_sent_memes("chat-a", "maibot")) == 1
    with pytest.raises(ValueError):
        await plugin.reply_meme(phase="before_send", **kwargs)


async def test_extension_rejects_cross_session_or_stale_parameters(mai):
    plugin, _ = mai
    kwargs = dict(
        parameters={"image_id": "1"}, reply_id="reply-1", session_id="chat-a", messages=[]
    )
    await plugin.reply_meme(phase="prepare", **kwargs)
    plugin.deliveries.enter("chat-b", "turn-b")
    for changed in [{"session_id": "chat-b"}, {"parameters": {"image_id": "2"}}]:
        with pytest.raises(ValueError, match="已变化"):
            await plugin.reply_meme(phase="before_send", **(kwargs | changed))
    with pytest.raises(ValueError):
        await plugin.reply_meme(phase="prepare", **kwargs)


async def test_send_respects_disabled_ratings_and_ipc_limit(mai):
    plugin, _ = mai
    plugin.service.settings = replace(plugin.service.settings, allowed_ratings=["none"])
    with pytest.raises(ValueError, match="不可用"):
        await plugin.send_image_only("3", stream_id="chat-a")
    await plugin.service.db.update_override(1, {"enabled": False})
    with pytest.raises(ValueError, match="不可用"):
        await plugin.send_image_only("1", stream_id="chat-a")
    plugin.service.client.image_bytes.return_value = b"x" * (11 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="IPC"):
        await plugin.send_image_only("2", stream_id="chat-a")


async def test_l_command_native_trigger_and_failure_cleanup(mai):
    plugin, host = mai
    assert await plugin.context_meme(stream_id="chat-a") == (True, None, True)
    assert host.calls[-1][0] == "maisaka.proactive.trigger"
    assert plugin.deliveries.enter("chat-a", "next-turn").context_only
    host.failure = True
    with pytest.raises(ValueError):
        await plugin.context_meme(stream_id="chat-b")
    assert "chat-b" not in plugin.deliveries.context_requests


async def test_provider_real_sdk_shapes_and_vision_input(mai):
    plugin, host = mai
    models = PROVIDERS.Models(plugin.ctx)
    models.task = "embedding"
    await models._prepare()
    provider = models.lookup("embedding")
    assert await provider.get_embeddings_batch(["开心", "难过"], batch_size=16) == [
        [3.0, 0.0, 0.0],
        [0.0, 2.0, 0.0],
    ]
    assert provider.get_dim() == 3
    await models.generate(
        provider_id="vision-test",
        prompt="prompt",
        system_prompt="system",
        image_url="data:image/png;base64,AA==",
    )
    call = host.calls[-1]
    assert call[0] == "llm.generate" and call[1]["prompt"][1]["content"][1]["image_url"][
        "url"
    ].startswith("data:")
    host.model = "different-model"
    with pytest.raises(ERROR, match="切换"):
        await provider.get_embedding("开心")
    host.failure = True
    with pytest.raises(ERROR) as exc:
        await provider.get_embedding("开心")
    assert "private upstream" not in str(exc.value)


async def test_sqlite_vectors_semantic_gate_persistence_and_updates(mai, tmp_path):
    plugin, host = mai
    models = PROVIDERS.Models(plugin.ctx)
    models.task = "embedding"
    await models._prepare()
    settings = replace(
        plugin.service.settings, embedding_provider_id="embedding", embedding_rpm=600
    )
    db = plugin.service.db
    for ident, text in [(1, "开心"), (2, "难过")]:
        await db.update_override(ident, {"description": text, "manual_tags": [text]})
    index = SEMANTIC(db, tmp_path, settings, models.lookup, VECTOR.SQLiteVectorStore)
    try:
        await index.run_once()
        assert (await index.status())["available"]
        result = await index.search("开心", 2)
        assert result[0]["id"] == "1" and result[0]["similarity"] == pytest.approx(1.0)
        directory = index.directory
        await index.close()
        index = SEMANTIC(db, tmp_path, settings, models.lookup, VECTOR.SQLiteVectorStore)
        await index.run_once()
        assert index.directory == directory
        await db.update_override(1, {"enabled": False})
        assert not (await index.status())["available"]  # Exactly 50% coverage.
        await db.update_override(3, {"description": "难过"})
        await index.run_once()
        assert all(row["id"] != "1" for row in await index.search("开心", 5))
        assert all(
            path.suffix in {".sqlite3", ".json"} or path.name.startswith("catalog.sqlite3-")
            for path in tmp_path.rglob("*")
            if path.is_file()
        )
    finally:
        await index.close()


async def test_panel_read_write_validation_preview_and_secrets(mai):
    plugin, _ = mai
    plugin.service.settings = replace(
        plugin.service.settings, app_key="private-app-key", webhook_secret="private-hook-secret"
    )
    settings = await plugin.panel_read("/settings")
    assert "private-" not in json.dumps(settings)
    assert settings["providers"][0]["id"] == "vision-test"
    edited = await plugin.panel_write(
        "/memes/1/edit", json.dumps({"manual_tags": ["龙脸"], "description": "开心的龙脸"})
    )
    assert "龙脸" in edited["manual_tags"]
    output = io.BytesIO()
    Image.new("RGB", (10, 10)).save(output, "PNG")
    plugin.service.client.image_bytes.return_value = output.getvalue()
    preview = await plugin.panel_read("/memes/1/preview")
    assert preview["data_url"].startswith("data:image/jpeg;base64,")
    assert (await plugin.panel_read("/memes/1/preview")) == preview
    assert plugin.service.client.image_bytes.await_count == 1
    for path in [
        "https://evil.test/status",
        "/memes?page=1&page=2",
        "/memes/0",
        "/memes?limit=1000",
    ]:
        with pytest.raises(ValueError):
            await plugin.panel_read(path)
    with pytest.raises(ValueError):
        await plugin.panel_read("/settings/save")
    with pytest.raises(ValueError):
        await plugin.panel_write("/memes/1/edit", '{"enabled": "false"}')


async def test_slow_thumbnail_does_not_hold_lifecycle_lock(mai):
    plugin, _ = mai
    started, release = asyncio.Event(), asyncio.Event()
    output = io.BytesIO()
    Image.new("RGB", (5, 5)).save(output, "PNG")

    async def download(url):
        started.set()
        await release.wait()
        return output.getvalue()

    plugin.service.client.image_bytes.side_effect = download
    preview = asyncio.create_task(plugin.panel_read("/memes/1/preview"))
    await asyncio.wait_for(started.wait(), 2)
    async with asyncio.timeout(1):
        assert (await plugin.panel_read("/memes/1"))["id"] == "1"
    release.set()
    assert (await preview)["data_url"].startswith("data:")


async def test_import_preserves_metadata_without_images_or_old_provider_ids(db, image, tmp_path):
    await publish(db, [image(9223372036854775807)])
    await db.update_override(
        9223372036854775807, {"manual_tags": ["龙脸"], "description": "人工描述"}
    )
    config = tmp_path / "astrbot-config.json"
    config.write_text(
        json.dumps(
            {
                "app_key": "secret",
                "vision_provider_id": "old-vision",
                "embedding_provider_id": "old-embed",
            }
        ),
        encoding="utf-8",
    )
    target = tmp_path.with_name(tmp_path.name + "-mai-data")
    await asyncio.to_thread(migrate, db.path.parent, target, config)
    saved = json.loads((target / "settings.json").read_text(encoding="utf-8"))
    assert (
        saved["app_key"] == "secret"
        and saved["embedding_provider_id"] == saved["vision_provider_id"] == ""
        and not saved["webhook_ready"]
    )
    copied = Database(target / "catalog.sqlite3")
    await copied.open()
    try:
        assert (await copied.get_image(9223372036854775807))["manual_tags"] == ["龙脸"]
    finally:
        await copied.close()
    with pytest.raises(ValueError):
        migrate(db.path.parent, target)


async def test_start_and_stop_using_native_plugin_paths(tmp_path, unused_tcp_port):
    plugin, host = ENTRY.create_plugin(), Host()
    host.plugin = plugin
    plugin._set_context(
        PluginContext("movetoex.longhub", host.rpc, PluginPaths(tmp_path, tmp_path / "runtime"))
    )
    adapter_settings = sys.modules["longhub_maibot_test.maibot_adapter.settings"].SettingsFile(
        tmp_path
    )
    adapter_settings.save(Settings(webhook_port=unused_tcp_port, auto_tagging=False))
    await plugin.on_load()
    assert (await plugin.panel_read("/status"))["receiver"]["started"]
    await plugin.on_unload()
    assert plugin.service is plugin.models is None


@pytest.mark.parametrize("confirm_with_secrets", [False, True])
async def test_save_credentials_and_confirm_subscription_survive_reload(
    tmp_path, unused_tcp_port, monkeypatch, confirm_with_secrets
):
    started = asyncio.Event()

    async def running_sync(service, *, force=False):
        assert service.settings.ready
        service.sync.status.update(status="running")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ADAPTER.Service, "_run_sync", running_sync)
    settings_file = sys.modules["longhub_maibot_test.maibot_adapter.settings"].SettingsFile(
        tmp_path
    )
    settings_file.save(Settings(webhook_port=unused_tcp_port, auto_tagging=False))
    for reload in (False, True):
        plugin, host = ENTRY.create_plugin(), Host()
        host.plugin = plugin
        plugin._set_context(
            PluginContext("movetoex.longhub", host.rpc, PluginPaths(tmp_path, tmp_path / "runtime"))
        )
        await plugin.on_load()
        try:
            if not reload:
                status = await plugin.panel_read("/status")
                assert not status["ready"]
                assert status["configuration"] == {
                    "app_key_configured": False,
                    "webhook_secret_configured": False,
                    "webhook_ready": False,
                }
                assert plugin.service.sync_task is None
                await plugin.panel_write(
                    "/settings/save",
                    json.dumps(
                        {
                            "app_key": "test-app-key",
                            "webhook_secret": "test-hook-secret",
                            "webhook_ready": confirm_with_secrets,
                        }
                    ),
                )
                status = await plugin.panel_read("/status")
                assert status["ready"] is confirm_with_secrets
                assert status["configuration"] == {
                    "app_key_configured": True,
                    "webhook_secret_configured": True,
                    "webhook_ready": confirm_with_secrets,
                }
                assert "test-app-key" not in json.dumps(status)
                assert "test-hook-secret" not in json.dumps(status)
                if not confirm_with_secrets:
                    assert plugin.service.sync_task is None
                    with pytest.raises(ValueError, match="订阅"):
                        await plugin.panel_write("/sync")
                    await plugin.panel_write("/settings/save", '{"webhook_ready":true}')
            status = await plugin.panel_read("/status")
            assert status["ready"] and status["receiver"]["started"]
            original_job = status["sync"]["id"]
            job = await plugin.panel_write("/sync")
            assert job["id"] == original_job
            await asyncio.wait_for(started.wait(), 2)
            persisted = settings_file.load()
            assert persisted.ready and persisted.webhook_ready
            assert (
                persisted.app_key == "test-app-key"
                and persisted.webhook_secret == "test-hook-secret"
            )
        finally:
            await plugin.on_unload()
        started.clear()
