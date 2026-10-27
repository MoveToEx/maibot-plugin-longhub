import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import publish
from tests.test_plugin import Event, delivered_parts, llm_response
from tests.test_plugin import plugin as plugin
from tests.test_plugin import plugin_module as plugin_module


@pytest.mark.parametrize("position", ["before", "after"])
async def test_tool_finishes_a_reply_whose_body_was_already_sent(
    plugin, plugin_module, db, image, position
):
    await publish(db, [image()])
    event, root = Event(), object()
    await plugin.prepare_meme_order(event, root)
    await event.send(plugin_module.MessageChain([plugin_module.Plain("第一段完整回答")]))

    result = await plugin.send_longhub_meme(event, "1", position=position)
    assert "本轮已有正文" in result
    assert "不要重新回答" in result and "换一种说法" in result
    assert plugin_module.IMAGE_ONLY_MARKER in result
    assert "若需要配文则正常回复" not in result
    response = llm_response(plugin_module.IMAGE_ONLY_MARKER)
    await plugin.complete_meme_order(event, root, response)
    if response.result_chain:
        await event.send(response.result_chain)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["第一段完整回答", "image"]


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("use_chain", [False, True])
async def test_exact_repeat_after_tool_is_removed_even_without_end_marker(
    plugin, plugin_module, db, image, position, use_chain
):
    await publish(db, [image()])
    event, root = Event(), object()
    await plugin.prepare_meme_order(event, root)
    await event.send(plugin_module.MessageChain([plugin_module.Plain("已经交付的正文")]))
    await plugin.send_longhub_meme(event, "1", position=position)
    response = llm_response("已经交付的正文")
    if use_chain:
        response.result_chain = plugin_module.MessageChain(
            [plugin_module.At(), plugin_module.Plain("已经交付的正文")]
        )
    await plugin.complete_meme_order(event, root, response)
    assert response.completion_text == ""
    if response.result_chain:
        await event.send(response.result_chain)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["已经交付的正文", "image"]


async def test_new_information_after_tool_is_still_allowed(plugin, plugin_module, db, image):
    await publish(db, [image()])
    event, root = Event(), object()
    await plugin.prepare_meme_order(event, root)
    await event.send(plugin_module.MessageChain([plugin_module.Plain("先说第一件事")]))
    await plugin.send_longhub_meme(event, "1", position="after")
    response = llm_response("还有一条此前没说过的重要信息")
    await plugin.complete_meme_order(event, root, response)
    assert response.completion_text == "还有一条此前没说过的重要信息"
    await event.send(plugin_module.MessageChain([plugin_module.Plain(response.completion_text)]))
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["先说第一件事", "还有一条此前没说过的重要信息", "image"]


async def test_streamed_body_is_known_before_a_buffered_adapter_flushes(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event, root = Event(), object()

    async def buffered_adapter(stream):
        messages = [message async for message in stream]
        for message in messages:
            await Event.send(event, message)

    event.send_streaming = buffered_adapter
    await plugin.prepare_meme_order(event, root)

    async def stream():
        yield plugin_module.MessageChain([plugin_module.Plain("流式输出的完整回答")])
        assert not event.sent
        result = await plugin.send_longhub_meme(event, "1", position="after")
        assert "本轮已有正文" in result and "不要重新回答" in result
        yield plugin_module.MessageChain([plugin_module.Plain(plugin_module.IMAGE_ONLY_MARKER)])
        await plugin.complete_meme_order(event, root, llm_response(plugin_module.IMAGE_ONLY_MARKER))

    await event.send_streaming(stream())
    assert delivered_parts(event) == ["流式输出的完整回答", "image"]


@pytest.mark.parametrize("failure", [OSError("send failed"), asyncio.CancelledError()])
async def test_failed_text_send_does_not_count_as_delivered(
    plugin, plugin_module, db, image, failure
):
    await publish(db, [image()])
    event = Event()
    event.send = AsyncMock(side_effect=failure)
    await plugin.prepare_meme_order(event, object())
    with pytest.raises(type(failure)):
        await event.send(plugin_module.MessageChain([plugin_module.Plain("未成功发送")]))
    result = await plugin.send_longhub_meme(event, "1", position="after")
    assert "本轮已有正文" not in result


async def test_caption_tracking_survives_adapter_mutation(plugin, plugin_module, db, image):
    await publish(db, [image()])
    event, root = Event(), object()

    async def mutating_send(message):
        await Event.send(event, message.derive(list(message.chain)))
        message.chain.clear()

    event.send = mutating_send
    await plugin.prepare_meme_order(event, root)
    await event.send(plugin_module.MessageChain([plugin_module.Plain("发送后会被适配器改写的链")]))
    result = await plugin.send_longhub_meme(event, "1", position="after")
    assert "本轮已有正文" in result
    response = llm_response("发送后会被适配器改写的链")
    await plugin.complete_meme_order(event, root, response)
    await event.send(response.result_chain)
    assert delivered_parts(event) == ["发送后会被适配器改写的链", "image"]


async def test_reply_tracking_is_isolated_between_overlapping_events(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    first, second = Event(), Event()
    await plugin.prepare_meme_order(first, object())
    await plugin.prepare_meme_order(second, object())
    await first.send(plugin_module.MessageChain([plugin_module.Plain("第一位用户的回答")]))
    results = await asyncio.gather(
        plugin.send_longhub_meme(first, "1", position="after"),
        plugin.send_longhub_meme(second, "1", position="after"),
    )
    assert "本轮已有正文" in results[0]
    assert "本轮已有正文" not in results[1]


@pytest.mark.parametrize("kind", ["reasoning", "tool_call", "agent_stats", "break"])
async def test_stream_control_messages_do_not_count_as_reply_body(
    plugin, plugin_module, db, image, kind
):
    await publish(db, [image()])
    event, root = Event(), object()
    await plugin.prepare_meme_order(event, root)

    async def stream():
        control = plugin_module.MessageChain([plugin_module.Plain("非正文")])
        control.type = kind
        yield control
        result = await plugin.send_longhub_meme(event, "1", position="after")
        assert "本轮已有正文" not in result
        await plugin.complete_meme_order(event, root, llm_response(""))

    await event.send_streaming(stream())


async def test_native_messaging_tool_delivery_also_changes_completion_hint(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    event.set_extra("_send_message_to_user_current_session_plain_texts", ["已由原生工具发送"])
    result = await plugin.send_longhub_meme(event, "1", position="after")
    assert "本轮已有正文" in result and "不要重新回答" in result


async def test_failed_meme_send_does_not_hide_followup_error_text(plugin, plugin_module, db, image):
    await publish(db, [image()])
    event, root = Event(), object()
    await plugin.prepare_meme_order(event, root)
    await event.send(plugin_module.MessageChain([plugin_module.Plain("原先的正文")]))
    plugin.service.client.image_bytes.side_effect = OSError("fetch failed")
    assert "发送失败" in await plugin.send_longhub_meme(event, "1", position="before")
    response = llm_response("图片未能发送，请稍后重试")
    await plugin.complete_meme_order(event, root, response)
    assert response.completion_text == "图片未能发送，请稍后重试"


async def test_reply_tracking_starts_in_request_hook_and_is_idempotent(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()
    plugin.service.candidates = AsyncMock(return_value=[])
    await plugin.provide_candidates(event, SimpleNamespace(system_prompt=""))
    wrapped = event.send
    await plugin.prepare_meme_order(event, object())
    assert event.send is wrapped
    await event.send(plugin_module.MessageChain([plugin_module.Plain("在工具之前已经输出")]))
    result = await plugin.send_longhub_meme(event, "1", position="after")
    assert "本轮已有正文" in result
