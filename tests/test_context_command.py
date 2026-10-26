import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import publish
from tests.test_plugin import Event, delivered_parts, llm_response
from tests.test_plugin import plugin as plugin
from tests.test_plugin import plugin_module as plugin_module


async def invoke(plugin, event):
    return [result async for result in plugin.context_meme(event)]


async def test_l_command_binds_current_conversation_without_copying_history(plugin, plugin_module):
    event = Event()
    event.message_str = "/l"
    event.messages = [plugin_module.Plain("/l")]
    original = event.messages
    results = await invoke(plugin, event)
    manager = plugin.context.conversation_manager
    manager.get_curr_conversation_id.assert_awaited_once_with(event.unified_msg_origin)
    manager.get_conversation.assert_awaited_once_with(event.unified_msg_origin, "conversation-1")
    manager.new_conversation.assert_not_awaited()
    assert plugin.context_meme.test_command_name == "l"
    assert len(results) == 1 and results[0].conversation is manager.get_conversation.return_value
    assert results[0].prompt == plugin_module.CONTEXT_MEME_PROMPT and results[0].contexts == []
    assert event.get_extra("longhub_context_command")
    assert event.message_str == "/l" and event.messages is original
    assert event.call_llm is True  # Suppress a second automatic request after yielding this one.
    assert not event.sent
    plugin.service.client.image_bytes.assert_not_awaited()


@pytest.mark.parametrize("cid", [None, "deleted-conversation"])
async def test_l_command_creates_session_when_missing_or_removed(plugin, cid):
    manager = plugin.context.conversation_manager
    manager.get_curr_conversation_id.return_value = cid
    conversation = SimpleNamespace(cid="new-conversation", history="[]")
    manager.get_conversation.side_effect = [None, conversation] if cid else [conversation]
    event = Event()
    results = await invoke(plugin, event)
    manager.new_conversation.assert_awaited_once_with(event.unified_msg_origin, "test-platform")
    assert results[0].conversation is conversation


@pytest.mark.parametrize("unavailable", ["cancelled", "uninitialized", "context_error"])
async def test_l_command_handles_cancel_and_unavailable_context(plugin, unavailable):
    event = Event()
    manager = plugin.context.conversation_manager
    if unavailable == "cancelled":
        event.stopped = True
    elif unavailable == "uninitialized":
        plugin.service = None
    else:
        manager.get_curr_conversation_id.side_effect = OSError("database unavailable")
    results = await invoke(plugin, event)
    assert not event.get_extra("longhub_context_command") and not event.sent
    if unavailable == "cancelled":
        assert not results and event.call_llm is True
        manager.get_curr_conversation_id.assert_not_awaited()
    else:
        assert len(results) == 1
        expected = "上下文" if unavailable == "context_error" else "尚未初始化"
        assert expected in results[0].text
        assert event.call_llm is True


async def test_l_command_preserves_native_context_and_skips_command_text_search(
    plugin, plugin_module
):
    event = Event()
    event.message_str = "/l"
    (req,) = await invoke(plugin, event)
    # The native builder reloads bound conversation history under its session lock.
    history = json.loads(req.conversation.history)
    req.contexts = history
    req.system_prompt = "原人格"
    plugin.service.candidates = AsyncMock(return_value=[])
    await plugin.provide_candidates(event, req)
    await plugin.provide_candidates(event, req)
    assert req.contexts is history and req.prompt == plugin_module.CONTEXT_MEME_PROMPT
    assert req.system_prompt.startswith("原人格")
    assert req.system_prompt.count("[本轮用户使用 /l 指令]") == 1
    assert plugin_module.IMAGE_ONLY_MARKER in req.system_prompt
    assert "不要把 /l 指令本身当作检索词" in req.system_prompt
    plugin.service.candidates.assert_not_awaited()


async def test_mentions_have_no_special_context_selection(plugin, plugin_module):
    event = Event()
    event.message_str = ""
    event.messages = [plugin_module.At("bot")]
    plugin.service.candidates = AsyncMock(return_value=[])
    req = SimpleNamespace(system_prompt="原人格", prompt="", contexts=[])
    await plugin.provide_candidates(event, req)
    assert event.message_str == "" and req.prompt == "" and req.contexts == []
    assert not event.get_extra("longhub_context_command")
    assert "[本轮用户使用 /l 指令]" not in req.system_prompt
    assert not event.sent and not event.call_llm


async def test_l_command_can_search_send_image_only_and_record_current_conversation(
    plugin, plugin_module, db, image
):
    await publish(db, [image(1, tag_names=["sleeping"]), image(2, tag_names=["happy"])])
    event = Event()
    event.message_str = "/l"
    (req,) = await invoke(plugin, event)
    req.contexts = json.loads(req.conversation.history)
    await plugin.provide_candidates(event, req)
    result = json.loads(
        await plugin.query_longhub_memes_sql(
            event, "SELECT image_id FROM meme_tags WHERE tag IN ('happy','开心')"
        )
    )
    assert result["rows"] == [["2"]]
    await plugin.send_longhub_meme(event, "2", position="after")
    response = llm_response(text=plugin_module.IMAGE_ONLY_MARKER)
    await plugin.complete_meme_order(event, None, response)
    await event.send(response.result_chain)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["image"]
    records = await db.recent_sent_memes(event.unified_msg_origin, req.conversation.cid)
    assert len(records) == 1 and records[0]["image_id"] == "2"
