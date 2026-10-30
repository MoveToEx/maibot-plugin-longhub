"""Exercise Private Companion 7.0.x's per-event callback contract.

The harness rebuilds/restores the same layers as core_reply.py,
core_reply_tracking.py and core_reply_segmentation.py. No third-party plugin,
model or platform service is installed or called by these tests.
"""

import asyncio
from functools import wraps
from unittest.mock import AsyncMock

import pytest

from backend.delivery import COMPANION_CONTEXT
from tests.conftest import publish
from tests.test_delivery_memory import begin, records
from tests.test_plugin import Event, delivered_parts, llm_response
from tests.test_plugin import plugin as plugin
from tests.test_plugin import plugin_module as plugin_module


class Companion:
    def __init__(self, event, module, *, managed_stream=False, drop_image=False):
        self.event, self.module = event, module
        self.managed_stream, self.drop_image = managed_stream, drop_image
        self.state = {
            "_original_send": event.send,
            "_original_send_streaming": event.send_streaming,
            "_send_wrapper": self.send,
            "_authorize_delivery": AsyncMock(),
            "delivery_guard_active": False,
            "send_returns": 0,
            "send_unknown": 0,
        }
        event.set_extra(COMPANION_CONTEXT, self.state)
        self.decorate()

    async def send(self, message, *args, **kwargs):
        if self.state.get("group_participation", {}).get("mode") == "skip":
            return None
        if self.drop_image and any(not isinstance(p, self.module.Plain) for p in message.chain):
            # Another successful send must not acknowledge the intercepted image.
            other = self.module.MessageChain([self.module.Image.fromBytes(b"other")])
            await self.state["_original_send"](other)
            self.state["send_returns"] += 1
            return None
        result = await self.state["_original_send"](message, *args, **kwargs)
        if result is False:
            return False
        self.state["send_returns"] += 1
        return result

    async def send_streaming(self, stream, *args, **kwargs):
        try:
            if self.managed_stream:
                # QQ's managed stream re-arms event.send inside the stream call,
                # bypassing the captured send_streaming callback entirely.
                self.decorate()
                buffered = []
                async for message in stream:
                    buffered.extend(message.chain)
                if buffered:
                    await self.event.send(self.module.MessageChain(buffered))
            else:
                return await self.state["_original_send_streaming"](stream, *args, **kwargs)
        finally:
            self.restore()

    def decorate(self):
        self.state["delivery_guard_active"] = True
        self.event.send = self.send
        self.event.send_streaming = self.send_streaming

    def restore(self):
        self.state["delivery_guard_active"] = False
        self.event.send = self.state["_original_send"]
        self.event.send_streaming = self.state["_original_send_streaming"]


@pytest.mark.parametrize("rearm", [False, True])
async def test_image_only_after_survives_companion_replacing_and_restoring_send(
    plugin, plugin_module, db, image, rearm
):
    await publish(db, [image()])
    event = Event()
    companion = Companion(event, plugin_module)
    await begin(plugin, event)
    await plugin.send_longhub_meme(event, "1", position="after")
    response = llm_response(plugin_module.IMAGE_ONLY_MARKER)
    await plugin.complete_meme_order(event, None, response)
    companion.decorate()
    if rearm:
        await plugin.rearm_meme_delivery(event)
    await event.send(response.result_chain)
    companion.restore()
    await plugin.send_meme_after_reply(event)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["image"]
    assert len(await records(db, event)) == 1
    assert plugin.recent[event.unified_msg_origin] == ["1"]
    assert "[LONG Hub 已发送表情记录]" in (await begin(plugin, Event())).system_prompt


@pytest.mark.parametrize("position", ["before", "after"])
async def test_companion_rearms_between_caption_segments_without_losing_order(
    plugin, plugin_module, db, image, position
):
    await publish(db, [image()])
    event = Event()
    companion = Companion(event, plugin_module)
    await begin(plugin, event)
    await plugin.send_longhub_meme(event, "1", position=position)
    await plugin.complete_meme_order(event, None, llm_response("第一段第二段"))
    for text in ("第一段", "第二段"):
        companion.decorate()
        await plugin.rearm_meme_delivery(event)
        await event.send(plugin_module.MessageChain([plugin_module.Plain(text)]))
        companion.restore()
    await plugin.send_meme_after_reply(event)
    expected = (
        ["image", "第一段", "第二段"] if position == "before" else ["第一段", "第二段", "image"]
    )
    assert delivered_parts(event) == expected
    assert len(await records(db, event)) == 1


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("caption", [False, True])
async def test_replaced_stream_hides_marker_and_flushes_only_after_buffered_text(
    plugin, plugin_module, db, image, managed, caption
):
    await publish(db, [image()])
    event, root = Event(), object()
    companion = Companion(event, plugin_module, managed_stream=managed)
    await begin(plugin, event)
    companion.decorate()
    # Streams can skip on_decorating_result; the late agent hook must re-arm.
    await plugin.prepare_meme_order(event, root)

    async def stream():
        if caption:
            yield plugin_module.MessageChain([plugin_module.Plain("正文")])
        await plugin.send_longhub_meme(event, "1", position="after")
        marker = plugin_module.IMAGE_ONLY_MARKER
        yield plugin_module.MessageChain([plugin_module.Plain(marker[:9])])
        yield plugin_module.MessageChain([plugin_module.Plain(marker[9:])])
        await plugin.complete_meme_order(event, root, llm_response(marker))
        assert not event.sent  # Both senders are still buffering here.

    if not managed:

        async def buffered(stream, *args, **kwargs):
            messages = [message async for message in stream]
            for message in messages:
                await event.send(message)

        # Simulate an adapter buffering below Companion and our outer observer.
        companion.state["_original_send_streaming"] = buffered
        await plugin.rearm_meme_delivery(event)
    await event.send_streaming(stream())
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == (["正文", "image"] if caption else ["image"])
    assert len(await records(db, event)) == 1
    assert not event.get_extra("longhub_streaming_active")


async def test_silently_dropped_image_is_not_confirmed_by_another_successful_send(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()
    companion = Companion(event, plugin_module, drop_image=True)
    await begin(plugin, event)
    result = await plugin.send_longhub_meme(event, "1")
    assert "发送失败" in result and "未获得发送确认" in result
    assert companion.state["send_returns"] == 1
    assert not await records(db, event)
    assert not event.get_extra("longhub_delivery_confirmed")
    assert not plugin.recent.get(event.unified_msg_origin)
    # Unknown delivery is not retried by another model tool call this turn.
    repeated = await plugin.send_longhub_meme(event, "1")
    assert "不要再次调用" in repeated and len(event.sent) == 1


@pytest.mark.parametrize(
    "blocked",
    [
        {"group_participation": {"mode": "skip"}},
        {"denial_notified": True},
        {"send_unknown": 1},
        {"_reply_segmentation": {"stopped": True}},
    ],
)
async def test_companion_policy_is_respected_even_after_restoring_native_sender(
    plugin, plugin_module, db, image, blocked
):
    await publish(db, [image()])
    event = Event()
    companion = Companion(event, plugin_module)
    await begin(plugin, event)
    await plugin.send_longhub_meme(event, "1", position="after")
    response = llm_response(plugin_module.IMAGE_ONLY_MARKER)
    await plugin.complete_meme_order(event, None, response)
    companion.state.update(blocked)
    companion.restore()
    with pytest.raises(plugin_module.DeliveryUnconfirmed):
        await event.send(response.result_chain)
    await plugin.send_meme_after_reply(event)
    assert not event.sent and not await records(db, event)
    result = await plugin.send_longhub_meme(event, "1")
    assert "已发送一张" not in result and "不要重复发送" in result


@pytest.mark.parametrize("failure", [False, OSError("adapter failed"), asyncio.CancelledError()])
async def test_companion_platform_failure_never_creates_receipts(
    plugin, plugin_module, db, image, failure
):
    await publish(db, [image()])
    event = Event()
    event.send = (
        AsyncMock(return_value=False) if failure is False else AsyncMock(side_effect=failure)
    )
    Companion(event, plugin_module)
    await begin(plugin, event)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await plugin.send_longhub_meme(event, "1")
    else:
        assert "发送失败" in await plugin.send_longhub_meme(event, "1")
    assert not await records(db, event)
    assert not event.get_extra("longhub_delivery_confirmed")


async def test_repeated_hooks_and_cooperative_wrappers_do_not_duplicate_delivery(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()
    companion = Companion(event, plugin_module)
    await begin(plugin, event)
    callback = companion.state["_original_send"]
    for _ in range(20):
        companion.decorate()
        await plugin.rearm_meme_delivery(event)
        wrapped = event.send
        await plugin.rearm_meme_delivery(event)
        assert event.send is wrapped
        assert companion.state["_original_send"] is callback
    original = event.send

    @wraps(original)
    async def cooperative(message):
        return await original(message)

    event.send = cooperative
    await plugin.send_longhub_meme(event, "1", position="after")
    response = llm_response("")
    await plugin.complete_meme_order(event, None, response)
    await event.send(response.result_chain)
    await event.send(response.result_chain)
    assert delivered_parts(event) == ["image"]
    assert len(await records(db, event)) == 1


async def test_companion_capturing_an_existing_longhub_tracker_does_not_form_a_cycle(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()
    await begin(plugin, event)
    Companion(event, plugin_module)
    await plugin.rearm_meme_delivery(event)
    assert (await plugin.send_longhub_meme(event, "1")).startswith("表情已发送")
    assert delivered_parts(event) == ["image"]
    assert len(await records(db, event)) == 1


async def test_overlapping_companion_events_keep_callbacks_and_receipts_isolated(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    first, second = Event(), Event()
    Companion(first, plugin_module)
    Companion(second, plugin_module)
    await begin(plugin, first, "first")
    await begin(plugin, second, "second")
    await asyncio.gather(
        plugin.send_longhub_meme(first, "1"), plugin.send_longhub_meme(second, "1")
    )
    assert delivered_parts(first) == delivered_parts(second) == ["image"]
    assert len(await records(db, first, "first")) == 1
    assert len(await records(db, second, "second")) == 1


async def test_unknown_companion_callback_contract_fails_without_claiming_delivery(
    plugin, db, image
):
    await publish(db, [image()])
    event = Event()
    event.set_extra(COMPANION_CONTEXT, {"new_layout": True})
    await begin(plugin, event)
    result = await plugin.send_longhub_meme(event, "1")
    assert "发送接口已变化" in result
    assert not event.sent and not await records(db, event)


async def test_captured_native_stream_survives_outer_wrapper_replacement(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()
    companion = Companion(event, plugin_module)
    await begin(plugin, event)
    companion.decorate()  # Lose the outer observer after it was already installed.

    async def stream():
        await plugin.send_longhub_meme(event, "1", position="after")
        yield plugin_module.MessageChain([plugin_module.Plain(plugin_module.IMAGE_ONLY_MARKER)])
        await plugin.complete_meme_order(event, None, llm_response(""))

    await event.send_streaming(stream())
    assert delivered_parts(event) == ["image"]
    assert len(await records(db, event)) == 1


async def test_companion_receipt_uses_identity_before_platform_mutates_chain(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()

    async def mutating_send(message):
        await Event.send(event, message.derive(list(message.chain)))
        message.chain.clear()

    event.send = mutating_send
    Companion(event, plugin_module)
    await begin(plugin, event)
    assert (await plugin.send_longhub_meme(event, "1")).startswith("表情已发送")
    assert delivered_parts(event) == ["image"]
    assert len(await records(db, event)) == 1


async def test_delayed_image_rechecks_authorization_after_companion_restores_sender(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()
    companion = Companion(event, plugin_module)
    await begin(plugin, event)
    await plugin.send_longhub_meme(event, "1", position="after")
    await plugin.complete_meme_order(event, None, llm_response("正文"))
    await event.send(plugin_module.MessageChain([plugin_module.Plain("正文")]))
    companion.restore()
    companion.state["_authorize_delivery"].side_effect = RuntimeError("permission revoked")
    await plugin.send_meme_after_reply(event)
    companion.state["_authorize_delivery"].assert_awaited_once()
    assert delivered_parts(event) == ["正文"]
    assert not await records(db, event)


async def test_changed_companion_contract_cannot_use_an_old_trusted_callback(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()
    companion = Companion(event, plugin_module)
    await begin(plugin, event)
    await plugin.send_longhub_meme(event, "1", position="after")
    response = llm_response("")
    await plugin.complete_meme_order(event, None, response)
    companion.restore()
    event.set_extra(COMPANION_CONTEXT, {"new_layout": True})
    with pytest.raises(plugin_module.DeliveryUnconfirmed, match="发送接口已变化"):
        await event.send(response.result_chain)
    assert not event.sent and not await records(db, event)
