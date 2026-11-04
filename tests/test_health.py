"""Health diagnostics exercise actual SDK calls and isolate independent failures."""

import asyncio
import json
import logging
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_maibot import ENTRY, ROOT
from tests.test_maibot import mai as mai

HEALTH = sys.modules["longhub_maibot_test.runtime.health"]


@pytest.fixture
def health_client(mai, monkeypatch):
    plugin, _ = mai
    client = SimpleNamespace(open=AsyncMock(), close=AsyncMock(), healthcheck=AsyncMock())
    monkeypatch.setattr(ENTRY, "HubClient", lambda settings: client)
    return client


async def test_command_probes_apis_and_sends_one_plain_report(mai, health_client):
    plugin, host = mai
    # A stuck chat operation must not prevent the independent health probes.
    async with plugin.lifecycle_lock:
        result = await asyncio.wait_for(plugin.health(stream_id="chat-a"), 2)
    assert result == (True, None, True)
    assert [name for name, _ in host.calls] == ["llm.generate", "send.text"]
    generate = host.calls[0][1]
    assert generate["task_name"] == "replyer" and generate["max_tokens"] == 64
    assert [message["role"] for message in generate["prompt"]] == ["system", "user"]
    assert all(isinstance(message["content"], str) for message in generate["prompt"])
    sent = host.calls[1][1]
    assert sent["stream_id"] == "chat-a" and sent["sync_to_maisaka_history"] is False
    assert re.fullmatch(
        r"Bot：在线\nLLM API：可用，\d+ ms\nLONG Hub API：可用，\d+ ms", sent["text"]
    )
    assert "tags" not in sent["text"]  # The diagnostic completion is discarded.
    health_client.open.assert_awaited_once()
    health_client.healthcheck.assert_awaited_once()
    health_client.close.assert_awaited_once()
    assert not plugin.deliveries.current("chat-a").searched


def test_health_command_registration_and_exact_match():
    command = next(
        item for item in ENTRY.create_plugin().get_components() if item["name"] == "longhub_health"
    )
    assert command["type"] == "COMMAND" and command["metadata"]["intercept_message_level"] == 2
    pattern = command["metadata"]["command_pattern"]
    assert re.fullmatch(pattern, "/health") and re.fullmatch(pattern, "/health \n")
    assert not re.fullmatch(pattern, "/healthy") and not re.fullmatch(pattern, "/health extra")
    manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    assert "send.text" in manifest["capabilities"]


async def test_llm_and_hub_probes_run_concurrently(mai):
    plugin, _ = mai
    llm_started, hub_started = asyncio.Event(), asyncio.Event()

    async def llm(**kwargs):
        llm_started.set()
        await hub_started.wait()
        return {"success": True, "response": "OK"}

    async def hub():
        hub_started.set()
        await llm_started.wait()

    plugin.ctx.llm.generate = llm
    report = await asyncio.wait_for(
        HEALTH.health_report(plugin.ctx, SimpleNamespace(healthcheck=hub)), 2
    )
    assert "LLM API：可用" in report and "LONG Hub API：可用" in report


@pytest.mark.parametrize(
    "response",
    [
        {"success": False, "error": "provider-secret"},
        {"success": True, "response": ""},
        {"success": True, "response": None},
    ],
)
async def test_failed_or_empty_completion_is_unavailable_but_hub_still_checked(mai, response):
    plugin, _ = mai
    plugin.ctx.llm.generate = AsyncMock(return_value=response)
    client = SimpleNamespace(healthcheck=AsyncMock())
    report = await HEALTH.health_report(plugin.ctx, client)
    assert "Bot：在线" in report and "LLM API：不可用" in report and "LONG Hub API：可用" in report
    assert "provider-secret" not in report
    client.healthcheck.assert_awaited_once()


async def test_hub_http_failure_keeps_llm_status_and_never_echoes_body(mai, caplog):
    plugin, _ = mai
    client = SimpleNamespace(
        healthcheck=AsyncMock(side_effect=HEALTH.HubError(503, "hub-secret response body"))
    )
    with caplog.at_level(logging.INFO):
        report = await HEALTH.health_report(plugin.ctx, client)
    assert "LLM API：可用" in report and "LONG Hub API：不可用（HTTP 503）" in report
    assert "hub-secret" not in report + caplog.text and "response body" not in caplog.text
    assert "latency_ms=" in caplog.text


async def test_unexpected_provider_error_is_not_echoed_or_logged(mai, caplog):
    plugin, _ = mai
    plugin.ctx.llm.generate = AsyncMock(side_effect=RuntimeError("Authorization: provider-secret"))
    with caplog.at_level(logging.WARNING):
        report = await HEALTH.health_report(plugin.ctx, SimpleNamespace(healthcheck=AsyncMock()))
    assert "LLM API：不可用（调用失败：RuntimeError）" in report
    assert "provider-secret" not in report + caplog.text and "Authorization" not in caplog.text


async def test_timeout_still_returns_other_probe_result(mai, monkeypatch):
    plugin, _ = mai
    cancelled = asyncio.Event()

    async def pending(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    plugin.ctx.llm.generate = pending
    monkeypatch.setattr(HEALTH, "PROBE_TIMEOUT_SECONDS", 0.01)
    report = await asyncio.wait_for(
        HEALTH.health_report(plugin.ctx, SimpleNamespace(healthcheck=AsyncMock())), 1
    )
    assert "LLM API：不可用（请求超时）" in report and "LONG Hub API：可用" in report
    assert cancelled.is_set()


async def test_health_cancellation_closes_own_session_without_sending(mai, health_client):
    plugin, host = mai
    started = asyncio.Event()

    async def pending(**kwargs):
        started.set()
        await asyncio.Event().wait()

    plugin.ctx.llm.generate = pending
    task = asyncio.create_task(plugin.health(stream_id="chat-a"))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    health_client.close.assert_awaited_once()
    assert not any(name == "send.text" for name, _ in host.calls)


async def test_command_rejects_missing_session_before_api_calls(mai, health_client):
    plugin, host = mai
    with pytest.raises(ValueError, match="会话"):
        await plugin.health()
    assert not host.calls
    health_client.open.assert_not_awaited()


async def test_report_send_failure_is_not_claimed_successful(mai, health_client):
    plugin, host = mai
    host.send_mode = "failed"
    with pytest.raises(RuntimeError, match="发送失败"):
        await plugin.health(stream_id="chat-a")
    health_client.close.assert_awaited_once()
