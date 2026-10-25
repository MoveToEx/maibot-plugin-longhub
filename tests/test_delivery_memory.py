import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import publish
from tests.test_plugin import Event, delivered_parts, llm_response
from tests.test_plugin import plugin as plugin
from tests.test_plugin import plugin_module as plugin_module


def request(cid="conversation-1", history=None):
    if history is None:
        history = [{"role": "user", "content": "上一轮对话"}]
    return SimpleNamespace(
        conversation=SimpleNamespace(cid=cid, history=json.dumps(history)),
        contexts=history,
        system_prompt="原人格",
        prompt="当前消息",
    )


async def begin(plugin, event, cid="conversation-1"):
    plugin.service.candidates = AsyncMock(return_value=[])
    req = request(cid)
    await plugin.provide_candidates(event, req)
    return req


async def records(db, event, cid="conversation-1"):
    return await db.recent_sent_memes(event.unified_msg_origin, cid)


async def test_successful_send_is_visible_to_next_turn_and_survives_reload(
    plugin, db, image, plugin_module
):
    ident = 2**63 - 1
    await publish(db, [image(ident, text="这张已发过", tag_names=["happy", "龙脸"])])
    await db.update_override(ident, {"description": "龙脸在笑"})
    event = Event()
    await begin(plugin, event)
    result = await plugin.send_longhub_meme(event, str(ident))
    assert "已发送表情元数据" in result and str(ident) in result
    rows = await records(db, event)
    assert len(rows) == 1
    assert rows[0]["image_id"] == str(ident) and rows[0]["description"] == "龙脸在笑"
    assert rows[0]["tags"] == ["happy", "龙脸"]
    assert "https://" not in json.dumps(rows) and "base64" not in json.dumps(rows)
    # A metadata snapshot still describes the past send after the Hub item disappears.
    generation = await db.get_meta("active_generation")
    await db.refresh(generation, ident, None)
    await db.close()
    await db.open()
    restarted = plugin_module.LongHubPlugin(plugin.context, plugin.config)
    restarted.service = plugin.service
    next_event = Event()
    req = await begin(restarted, next_event)
    assert "[LONG Hub 已发送表情记录]" in req.system_prompt
    assert "这张已发过" in req.system_prompt and str(ident) in req.system_prompt
    assert req.contexts == [{"role": "user", "content": "上一轮对话"}]
    assert req.prompt == "当前消息" and not next_event.sent
    await restarted.provide_candidates(next_event, req)
    assert req.system_prompt.count("[LONG Hub 已发送表情记录]") == 1


async def test_delivery_memory_is_isolated_by_session_and_conversation(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    await begin(plugin, event)
    await plugin.send_longhub_meme(event, "1")
    same = await begin(plugin, Event())
    other_conv = await begin(plugin, Event(), "new-conversation")
    other_chat = await begin(plugin, Event("aiocqhttp:GroupMessage:other"))
    assert "[LONG Hub 已发送表情记录]" in same.system_prompt
    assert "[LONG Hub 已发送表情记录]" not in other_conv.system_prompt
    assert "[LONG Hub 已发送表情记录]" not in other_chat.system_prompt


@pytest.mark.parametrize("history", [[], "[]"])
async def test_clearing_native_history_clears_only_its_receipts(plugin, db, image, history):
    await publish(db, [image()])
    for cid in ("conversation-1", "other-conversation"):
        event = Event()
        await begin(plugin, event, cid)
        await plugin.send_longhub_meme(event, "1")
    event = Event()
    req = request()
    req.conversation.history = history
    await plugin.provide_candidates(event, req)
    assert "[LONG Hub 已发送表情记录]" not in req.system_prompt
    assert not await records(db, event)
    assert len(await records(db, event, "other-conversation")) == 1


async def test_unbound_custom_request_does_not_leak_into_native_conversation(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    plugin.service.candidates = AsyncMock(return_value=[])
    await plugin.provide_candidates(event, SimpleNamespace(system_prompt="", contexts=[]))
    await plugin.send_longhub_meme(event, "1")
    assert not await records(db, event)


@pytest.mark.parametrize("mode", ["before", "after-caption", "after-empty", "after-stream"])
async def test_receipts_follow_real_delivery_in_every_reply_path(
    plugin, plugin_module, db, image, mode
):
    await publish(db, [image()])
    event = Event()
    await begin(plugin, event)
    if mode == "before":
        await plugin.send_longhub_meme(event, "1")
    elif mode == "after-caption":
        await plugin.send_longhub_meme(event, "1", position="after")
        assert not await records(db, event)
        await plugin.complete_meme_order(event, None, llm_response("配文"))
        await event.send(plugin_module.MessageChain([plugin_module.Plain("配文")]))
        assert not await records(db, event)
        await plugin.send_meme_after_reply(event)
    elif mode == "after-empty":
        await plugin.send_longhub_meme(event, "1", position="after")
        response = llm_response(plugin_module.IMAGE_ONLY_MARKER)
        await plugin.complete_meme_order(event, None, response)
        assert event.get_extra("longhub_sent") and not await records(db, event)
        assert not plugin.recent.get(event.unified_msg_origin)
        # Text and other plugins' image components cannot confirm our pending image.
        await event.send(plugin_module.MessageChain([plugin_module.Plain("其他内容")]))
        await event.send(plugin_module.MessageChain([plugin_module.Image.fromBytes(b"other")]))
        assert not await records(db, event)
        await event.send(response.result_chain)
    else:

        async def stream():
            await plugin.send_longhub_meme(event, "1", position="after")
            yield plugin_module.MessageChain([plugin_module.Plain(plugin_module.IMAGE_ONLY_MARKER)])
            await plugin.complete_meme_order(
                event, None, llm_response(plugin_module.IMAGE_ONLY_MARKER)
            )

        await event.send_streaming(stream())
    await plugin.send_meme_after_reply(event)
    await plugin.send_meme_after_reply(event)
    assert len(await records(db, event)) == 1
    assert plugin.recent[event.unified_msg_origin] == ["1"]


@pytest.mark.parametrize("mode", ["fetch", "before", "after-caption", "after-empty", "cancelled"])
async def test_failed_or_cancelled_delivery_does_not_create_success_memory(
    plugin, plugin_module, db, image, mode
):
    await publish(db, [image()])
    event = Event()
    await begin(plugin, event)
    if mode == "fetch":
        plugin.service.client.image_bytes.side_effect = OSError("fetch failed")
    else:
        event.send = AsyncMock(side_effect=OSError("platform rejected image"))
    if mode in ("before", "fetch"):
        result = await plugin.send_longhub_meme(event, "1")
        assert "发送失败" in result
    else:
        await plugin.send_longhub_meme(event, "1", position="after")
        if mode == "cancelled":
            event.stopped = True
        response = llm_response("" if mode == "after-empty" else "正文")
        await plugin.complete_meme_order(event, None, response)
        if response.result_chain:
            with pytest.raises(OSError):
                await event.send(response.result_chain)
        # AstrBot can still invoke this hook after a swallowed platform error.
        await plugin.send_meme_after_reply(event)
    assert not await records(db, event)
    assert not event.get_extra("longhub_delivery_confirmed")


async def test_image_sent_before_later_cancellation_is_still_a_historical_fact(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    await begin(plugin, event)

    async def send_then_cancel(message):
        await Event.send(event, message)
        event.stopped = True

    event.send = send_then_cancel
    await plugin.send_longhub_meme(event, "1")
    assert delivered_parts(event) == ["image"]
    assert len(await records(db, event)) == 1


async def test_task_cancellation_after_send_preserves_its_receipt(plugin, db, image, monkeypatch):
    await publish(db, [image()])
    event = Event()
    await begin(plugin, event)
    started, release = asyncio.Event(), asyncio.Event()
    original = db.record_sent_meme

    async def save(*args):
        started.set()
        await release.wait()
        await original(*args)

    monkeypatch.setattr(db, "record_sent_meme", save)
    task = asyncio.create_task(plugin.send_longhub_meme(event, "1"))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert delivered_parts(event) == ["image"]
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(await records(db, event)) == 1


async def test_candidate_search_failure_cannot_hide_delivery_facts(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    await begin(plugin, event)
    await plugin.send_longhub_meme(event, "1")
    plugin.service.candidates.side_effect = OSError("FTS unavailable")
    req = request()
    await plugin.provide_candidates(Event(), req)
    assert "[LONG Hub 已发送表情记录]" in req.system_prompt


async def test_memory_write_failure_does_not_turn_a_sent_image_into_tool_failure(
    plugin, db, image, monkeypatch
):
    await publish(db, [image()])
    event = Event()
    await begin(plugin, event)
    monkeypatch.setattr(db, "record_sent_meme", AsyncMock(side_effect=OSError("disk unavailable")))
    result = await plugin.send_longhub_meme(event, "1")
    assert result.startswith("表情已发送。") and delivered_parts(event) == ["image"]


async def test_receipt_storage_deduplicates_and_bounds_context(db):
    for i in range(30):
        await db.record_sent_meme(
            "chat", "conv", f"receipt-{i}", {"image_id": str(i + 1), "text": f"sent-{i}"}
        )
    await db.record_sent_meme(
        "chat", "conv", "receipt-29", {"image_id": "999", "text": "duplicate"}
    )
    rows = await db.recent_sent_memes("chat", "conv", 50)
    assert len(rows) == 20
    assert rows[0]["text"] == "sent-10" and rows[-1]["text"] == "sent-29"
    recent = await db.recent_sent_memes("chat", "conv")
    assert [row["text"] for row in recent] == [f"sent-{i}" for i in range(25, 30)]
