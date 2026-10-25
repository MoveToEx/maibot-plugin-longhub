"""Exercise the plugin boundary with the documented AstrBot API surface."""

import asyncio
import importlib.util
import json
import logging
import sys
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import Settings
from tests.conftest import publish


@pytest.fixture
def plugin_module(monkeypatch, tmp_path):
    def module(name, **attrs):
        value = ModuleType(name)
        value.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    def decorator(*args, **kwargs):
        return lambda function: function

    class Star:
        def __init__(self, context):
            self.context = context

    class Image:
        @staticmethod
        def fromBytes(data):
            return SimpleNamespace(data=data)

    class Plain:
        def __init__(self, text):
            self.text = text

    class At:
        def __init__(self, qq="bot"):
            self.qq = qq

    class Reply:
        pass

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

        def derive(self, chain):
            return MessageChain(chain)

    class Config(dict):
        async def save_config_async(self, values):
            self.update(values)

    class Query(dict):
        def get(self, key, default=None, type=None):
            value = super().get(key, default)
            return type(value) if type else value

    web_request = SimpleNamespace(query=Query(), json=AsyncMock(return_value={}))
    module("astrbot")
    module("astrbot.api", AstrBotConfig=Config, logger=logging.getLogger("test-plugin"))
    module(
        "astrbot.api.event",
        AstrMessageEvent=object,
        MessageChain=MessageChain,
        filter=SimpleNamespace(
            event_message_type=decorator,
            EventMessageType=SimpleNamespace(ALL=object()),
            on_llm_request=decorator,
            llm_tool=decorator,
            on_agent_begin=decorator,
            on_agent_done=decorator,
            after_message_sent=decorator,
        ),
    )
    module("astrbot.api.message_components", Image=Image, Plain=Plain, At=At, Reply=Reply)
    module("astrbot.api.provider", ProviderRequest=object, LLMResponse=object)
    module("astrbot.api.star", Context=object, Star=Star, register=decorator)
    module(
        "astrbot.api.web",
        request=web_request,
        json_response=lambda data: data,
        error_response=lambda message, status_code=400: {"error": message, "code": status_code},
    )
    module("astrbot.core")
    module("astrbot.core.platform")
    module(
        "astrbot.core.platform.astr_message_event",
        streaming_response_event=ContextVar("test_streaming_response_event", default=None),
    )
    module("astrbot.core.utils")
    module("astrbot.core.utils.astrbot_path", get_astrbot_plugin_data_path=lambda: str(tmp_path))
    root = Path(__file__).parents[1]
    module("longhub_test_plugin", __path__=[str(root)])
    spec = importlib.util.spec_from_file_location("longhub_test_plugin.main", root / "main.py")
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded)
    spec.loader.exec_module(loaded)
    loaded.test_request = web_request
    loaded.test_config_class = Config
    return loaded


class Event:
    def __init__(self, origin="aiocqhttp:GroupMessage:100"):
        self.unified_msg_origin = origin
        self.message_str = "确实"
        self.messages = []
        self.self_id = "bot"
        self.extra = {}
        self.sent = []
        self.stopped = False

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def get_messages(self):
        return self.messages

    def get_self_id(self):
        return self.self_id

    def set_extra(self, key, value):
        self.extra[key] = value

    def is_stopped(self):
        return self.stopped

    async def send(self, message):
        await asyncio.sleep(0)
        self.sent.append(message)

    async def send_streaming(self, stream, *args, **kwargs):
        async for message in stream:
            await self.send(message)


@pytest.fixture
def plugin(plugin_module, db):
    context = SimpleNamespace(
        register_web_api=lambda *args: None,
        get_all_providers=lambda: [],
        get_provider_by_id=lambda ident: None,
    )
    plugin = plugin_module.LongHubPlugin(context, plugin_module.test_config_class())
    plugin.service = SimpleNamespace(
        db=db,
        settings=Settings(),
        client=SimpleNamespace(image_bytes=AsyncMock(return_value=b"GIF89a-test")),
    )
    return plugin


async def test_one_image_per_turn_even_for_parallel_tools(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    results = await asyncio.gather(
        plugin.send_longhub_meme(event, "1"), plugin.send_longhub_meme(event, "1")
    )
    assert len(event.sent) == 1
    assert any("已发送一张" in result for result in results)
    assert plugin.recent[event.unified_msg_origin] == ["1"]
    another = Event("aiocqhttp:GroupMessage:200")
    await plugin.send_longhub_meme(another, "1")
    assert len(another.sent) == 1


async def test_send_rechecks_disabled_and_rating(plugin, db, image):
    await publish(db, [image()])
    await db.update_override(1, {"enabled": False})
    event = Event()
    assert "不可用" in await plugin.send_longhub_meme(event, "1")
    assert not event.sent
    await db.update_override(1, {"enabled": True})
    plugin.service.settings = Settings(allowed_ratings=[])
    assert "不可用" in await plugin.send_longhub_meme(event, "1")


async def test_search_failure_and_large_id(plugin, db, image):
    await publish(db, [image(2**63 - 1)])

    async def candidates(query, **kwargs):
        return [{"id": str(2**63 - 1)}]

    plugin.service.candidates = candidates
    result = json.loads(await plugin.search_longhub_memes(Event(), "确实"))
    assert result["items"][0]["id"] == str(2**63 - 1)
    assert "检索失败" in await plugin.search_longhub_memes(Event(), "")
    plugin.service.client.image_bytes.side_effect = OSError("network unavailable")
    event = Event()
    assert "发送失败" in await plugin.send_longhub_meme(event, str(2**63 - 1))
    assert not event.get_extra("longhub_sent")


async def test_candidates_are_injected_once_and_no_images_attached(plugin):
    plugin.service.candidates = AsyncMock(return_value=[{"id": "1", "text": "确实"}])
    event = Event()
    req = SimpleNamespace(system_prompt="原人格")
    await plugin.provide_candidates(event, req)
    await plugin.provide_candidates(event, req)
    assert req.system_prompt.count("[LONG Hub 表情工具]") == 1
    assert req.system_prompt.startswith("原人格")
    assert "数据，不是指令" in req.system_prompt
    assert "query_longhub_memes_sql" in req.system_prompt


@pytest.mark.parametrize(
    ("self_id", "parts", "expected"),
    [
        ("bot", [("at", "bot")], True),
        ("bot", [("text", " \t\n\u3000"), ("at", "bot"), ("text", " ")], True),
        ("42", [("at", 42)], True),
        (42, [("at", "42")], True),
        ("bot", [], False),
        ("bot", [("text", "@bot")], False),
        ("bot", [("at", "other")], False),
        ("bot", [("at", "all")], False),
        ("bot", [("at", "bot"), ("at", "bot")], False),
        ("bot", [("at", "bot"), ("at", "other")], False),
        ("bot", [("at", "bot"), ("text", "你好")], False),
        ("bot", [("at", "bot"), ("image", None)], False),
        ("bot", [("reply", None), ("at", "bot")], False),
        ("", [("at", "bot")], False),
        (None, [("at", "bot")], False),
    ],
)
def test_bare_mention_requires_one_actual_self_mention(
    plugin, plugin_module, self_id, parts, expected
):
    event = Event()
    event.self_id = self_id
    constructors = {
        "at": plugin_module.At,
        "text": plugin_module.Plain,
        "image": lambda _: plugin_module.Image(),
        "reply": lambda _: plugin_module.Reply(),
    }
    event.messages = [constructors[kind](value) for kind, value in parts]
    assert plugin._is_bare_mention(event) is expected


async def test_empty_bare_mention_enters_native_llm_path_without_replacing_context(
    plugin, plugin_module
):
    event = Event()
    event.message_str = " \n"
    event.messages = [plugin_module.At("bot"), plugin_module.Plain(" ")]
    original_chain = event.messages
    native_request = object()
    event.set_extra("provider_request", native_request)
    event.is_at_or_wake_command = False
    event.call_llm = True
    await plugin.prepare_bare_mention(event)
    assert event.message_str == plugin_module.BARE_MENTION_TEXT
    assert event.get_extra("longhub_bare_mention")
    assert event.messages is original_chain
    assert event.get_extra("provider_request") is native_request
    assert event.is_at_or_wake_command is False and event.call_llm is True
    assert not event.sent


@pytest.mark.parametrize("unavailable", ["stopped", "uninitialized", "ordinary"])
async def test_bare_mention_preparation_respects_cancel_and_other_messages(
    plugin, plugin_module, unavailable
):
    event = Event()
    event.message_str = ""
    event.messages = [plugin_module.At("bot")]
    if unavailable == "stopped":
        event.stopped = True
    elif unavailable == "uninitialized":
        plugin.service = None
    else:
        event.messages.append(plugin_module.Plain("有问题"))
    await plugin.prepare_bare_mention(event)
    assert event.message_str == "" and not event.get_extra("longhub_bare_mention")


async def test_bare_mention_guides_context_selection_without_searching_adapter_text(
    plugin, plugin_module
):
    event = Event()
    event.message_str = "[At:bot]"
    event.messages = [plugin_module.At("bot")]
    plugin.service.candidates = AsyncMock()
    history = [{"role": "user", "content": "刚才还在聊原神，今天很开心"}]
    req = SimpleNamespace(system_prompt="原人格", prompt="[At:bot]", contexts=history)
    await plugin.provide_candidates(event, req)
    await plugin.provide_candidates(event, req)
    assert req.contexts is history and req.prompt == "[At:bot]"
    assert req.system_prompt.startswith("原人格")
    assert req.system_prompt.count("[本轮用户仅 @bot]") == 1
    assert plugin_module.IMAGE_ONLY_MARKER in req.system_prompt
    plugin.service.candidates.assert_not_awaited()
    assert not event.sent


async def test_bare_mention_model_can_select_then_send_one_image_only(
    plugin, plugin_module, db, image
):
    await publish(db, [image(1, tag_names=["sleeping"]), image(2, tag_names=["happy"])])
    event = Event()
    event.message_str = ""
    event.messages = [plugin_module.At("bot")]
    await plugin.prepare_bare_mention(event)
    req = SimpleNamespace(
        system_prompt="",
        prompt=event.message_str,
        contexts=[{"role": "user", "content": "好开心，终于成功了"}],
    )
    await plugin.provide_candidates(event, req)
    # Simulate model-selected tool calls, without requesting a live LLM or chat send.
    result = json.loads(
        await plugin.query_longhub_memes_sql(
            event, "SELECT image_id FROM meme_tags WHERE tag IN ('happy','开心')"
        )
    )
    ident = result["rows"][0][0]
    assert ident == "2"
    await plugin.send_longhub_meme(event, ident, position="after")
    response = llm_response(text=plugin_module.IMAGE_ONLY_MARKER)
    await plugin.complete_meme_order(event, None, response)
    await event.send(response.result_chain)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["image"]
    assert plugin.recent[event.unified_msg_origin] == ["2"]


async def test_sql_tool_returns_sendable_ids_and_uses_rating_policy(plugin, db, image):
    await publish(db, [image(2**63 - 1), image(2, rating="violent")])
    plugin.service.settings = Settings(allowed_ratings=["none"])
    event = Event()
    result = json.loads(
        await plugin.query_longhub_memes_sql(event, "SELECT id,text FROM memes WHERE text='确实'")
    )
    assert result["rows"] == [[str(2**63 - 1), "确实"]]
    assert not event.sent
    assert "表情已发送" in await plugin.send_longhub_meme(event, result["rows"][0][0])
    assert len(event.sent) == 1
    assert "SQL 检索失败" in await plugin.query_longhub_memes_sql(event, "DELETE FROM memes")
    assert "SQL 检索失败" in await plugin.query_longhub_memes_sql(event, "SELECT id FROM memes", 0)
    plugin.service = None
    assert "尚未初始化" in await plugin.query_longhub_memes_sql(event, "SELECT 1")


async def test_web_edit_and_validation(plugin, plugin_module, db, image):
    await publish(db, [image()])
    plugin_module.test_request.json.return_value = {"manual_tags": ["手工"], "enabled": False}
    edited = await plugin.api_edit("1")
    assert edited["manual_tags"] == ["手工"] and not edited["enabled"]
    plugin_module.test_request.json.return_value = {"rating": "violent"}
    response = await plugin._guard(plugin.api_edit)(ident="1")
    assert response["code"] == 400
    assert (await db.get_image(1))["rating"] == "none"


async def test_settings_do_not_expose_secrets(plugin):
    plugin.service.settings = Settings(app_key="app-private", webhook_secret="hook-private")
    response = await plugin.api_settings()
    assert "app-private" not in json.dumps(response)
    assert "hook-private" not in json.dumps(response)


async def test_successful_send_not_reported_failed_when_tag_queue_fails(
    plugin, db, image, monkeypatch
):
    await publish(db, [image()])
    plugin.service.settings = Settings(vision_provider_id="vision")
    monkeypatch.setattr(db, "enqueue_tags", AsyncMock(side_effect=OSError("queue unavailable")))
    event = Event()
    assert (await plugin.send_longhub_meme(event, "1")).startswith("表情已发送。")
    assert len(event.sent) == 1 and event.get_extra("longhub_sent")


async def test_model_sdk_errors_do_not_expose_keys_or_image_payload(plugin):
    plugin.context.get_provider_by_id = lambda ident: object()
    plugin.context.llm_generate = AsyncMock(side_effect=RuntimeError("sk-private-key base64-image"))
    with pytest.raises(RuntimeError) as raised:
        await plugin._generate(
            provider_id="vision",
            prompt="test",
            system_prompt="test",
            image_url="data:image/jpeg;base64,test",
        )
    assert "sk-private-key" not in str(raised.value)
    assert "base64-image" not in str(raised.value)


def llm_response(text="回答正文", chain=None, role="assistant"):
    return SimpleNamespace(role=role, completion_text=text, result_chain=chain)


def delivered_parts(event):
    return [getattr(comp, "text", "image") for message in event.sent for comp in message.chain]


async def test_explicit_before_sends_image_before_text(plugin, plugin_module, db, image):
    await publish(db, [image()])
    event = Event()
    assert (await plugin.send_longhub_meme(event, "1", position="before")).startswith(
        "表情已发送。"
    )
    await event.send(plugin_module.MessageChain([plugin_module.Plain("回答正文")]))
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["image", "回答正文"]


async def test_after_waits_for_final_response_and_all_text_segments(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()
    result = await plugin.send_longhub_meme(event, "1", position="after")
    assert "已安排" in result and not event.sent
    plugin.service.client.image_bytes.assert_not_awaited()
    assert not event.get_extra("longhub_sent")
    assert event.unified_msg_origin not in plugin.recent
    await event.send(plugin_module.MessageChain([plugin_module.Plain("中间说明")]))
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["中间说明"]
    await plugin.complete_meme_order(event, None, llm_response())
    await event.send(plugin_module.MessageChain([plugin_module.Plain("第一段")]))
    await event.send(plugin_module.MessageChain([plugin_module.Plain("第二段")]))
    await plugin.send_meme_after_reply(event)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["中间说明", "第一段", "第二段", "image"]
    assert plugin.recent[event.unified_msg_origin] == ["1"]
    plugin.service.client.image_bytes.assert_awaited_once()


async def test_after_stream_waits_for_adapter_buffer_flush(plugin, plugin_module, db, image):
    await publish(db, [image()])
    event = Event()
    context = object()

    async def buffered_adapter(stream):
        messages = [message async for message in stream]
        # Agent is already done, but the adapter has not delivered its buffered text yet.
        assert event.get_extra("longhub_agent_done") and not event.sent
        plugin.service.client.image_bytes.assert_not_awaited()
        for message in messages:
            await event.send(message)

    event.send_streaming = buffered_adapter
    await plugin.prepare_meme_order(event, context)
    wrapped = event.send_streaming
    await plugin.prepare_meme_order(event, context)
    assert event.send_streaming is wrapped

    async def fetch(url):
        assert plugin_module.streaming_response_event.get() is None
        return b"GIF89a-test"

    plugin.service.client.image_bytes.side_effect = fetch

    async def stream():
        await plugin.send_longhub_meme(event, "1", position="after")
        yield plugin_module.MessageChain([plugin_module.Plain("第一段")])
        await plugin.complete_meme_order(event, context, llm_response())
        await plugin.send_meme_after_reply(event)
        yield plugin_module.MessageChain([plugin_module.Plain("第二段")])

    token = plugin_module.streaming_response_event.set(event)
    try:
        await event.send_streaming(stream())
        assert plugin_module.streaming_response_event.get() is event
    finally:
        plugin_module.streaming_response_event.reset(token)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["第一段", "第二段", "image"]


@pytest.mark.parametrize("text", ["", " \n "])
async def test_after_empty_nonstreaming_reply_uses_final_image_chain(
    plugin, plugin_module, db, image, text
):
    await publish(db, [image()])
    event = Event()
    await plugin.send_longhub_meme(event, "1", position="after")
    response = llm_response(text=text)
    await plugin.complete_meme_order(event, None, response)
    assert not event.sent and response.result_chain
    # Simulate the framework flushing buffered intermediate text before the final image.
    await event.send(plugin_module.MessageChain([plugin_module.Plain("缓冲中的文字")]))
    await event.send(response.result_chain)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["缓冲中的文字", "image"]
    plugin.service.client.image_bytes.assert_awaited_once()


@pytest.mark.parametrize("position", ["before", "after"])
async def test_empty_stream_can_reply_with_one_image_only(plugin, db, image, position):
    await publish(db, [image()])
    event = Event()
    context = object()
    await plugin.prepare_meme_order(event, context)

    async def stream():
        await plugin.send_longhub_meme(event, "1", position=position)
        await plugin.complete_meme_order(event, context, llm_response(text=""))
        if False:
            yield

    await event.send_streaming(stream())
    assert delivered_parts(event) == ["image"]
    assert len(event.sent) == 1


@pytest.mark.parametrize("position", ["before", "after"])
async def test_empty_nonstreaming_reply_can_send_one_image_only(plugin, db, image, position):
    await publish(db, [image()])
    event = Event()
    await plugin.send_longhub_meme(event, "1", position=position)
    response = llm_response(text="")
    await plugin.complete_meme_order(event, None, response)
    if response.result_chain:
        await event.send(response.result_chain)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["image"]
    assert len(event.sent) == 1
    assert not response.completion_text
    plugin.service.client.image_bytes.assert_awaited_once()


async def test_queued_after_reserves_turn_across_both_positions(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    results = await asyncio.gather(
        plugin.send_longhub_meme(event, "1", position="after"),
        plugin.send_longhub_meme(event, "1", position="before"),
    )
    assert all("安排" in result for result in results) and not event.sent
    await plugin.complete_meme_order(event, None, llm_response())
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["image"]


async def test_invalid_position_does_not_reserve_or_send(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    result = await plugin.send_longhub_meme(event, "1", position="middle")
    assert "before 或 after" in result
    assert event.get_extra("longhub_pending_after") is None and not event.sent
    plugin.service.client.image_bytes.assert_not_awaited()


async def test_after_revalidates_disabled_item_at_delivery(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    await plugin.send_longhub_meme(event, "1", position="after")
    await db.update_override(1, {"enabled": False})
    await plugin.complete_meme_order(event, None, llm_response())
    await plugin.send_meme_after_reply(event)
    assert not event.sent and event.get_extra("longhub_pending_after") is None
    plugin.service.client.image_bytes.assert_not_awaited()


async def test_after_delivery_failure_is_not_retried_by_duplicate_hooks(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    await plugin.send_longhub_meme(event, "1", position="after")
    plugin.service.client.image_bytes.side_effect = OSError("R2 unavailable")
    await plugin.complete_meme_order(event, None, llm_response())
    await plugin.send_meme_after_reply(event)
    await plugin.send_meme_after_reply(event)
    assert not event.sent and not event.get_extra("longhub_sent")
    plugin.service.client.image_bytes.assert_awaited_once()


async def test_cancelled_stream_drops_queued_image(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    await plugin.prepare_meme_order(event, object())

    async def stream():
        await plugin.send_longhub_meme(event, "1", position="after")
        raise asyncio.CancelledError()
        yield

    with pytest.raises(asyncio.CancelledError):
        await event.send_streaming(stream())
    assert event.get_extra("longhub_pending_after") is None and not event.sent


@pytest.mark.parametrize("flag", ["agent_user_aborted", "agent_stop_requested", "stopped"])
async def test_cancelled_reply_does_not_send_deferred_image(plugin, db, image, flag):
    await publish(db, [image()])
    event = Event()
    await plugin.send_longhub_meme(event, "1", position="after")
    if flag == "stopped":
        event.stopped = True
    else:
        event.set_extra(flag, True)
    await plugin.complete_meme_order(event, None, llm_response())
    await plugin.send_meme_after_reply(event)
    assert not event.sent and event.get_extra("longhub_pending_after") is None


async def test_error_reply_drops_deferred_image(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    await plugin.send_longhub_meme(event, "1", position="after")
    await plugin.complete_meme_order(event, None, llm_response(role="err"))
    await plugin.send_meme_after_reply(event)
    assert not event.sent and event.get_extra("longhub_pending_after") is None


async def test_nested_agent_completion_does_not_release_root_reply_image(plugin, db, image):
    await publish(db, [image()])
    event = Event()
    root, child = object(), object()
    await plugin.prepare_meme_order(event, root)
    await plugin.prepare_meme_order(event, child)
    await plugin.send_longhub_meme(event, "1", position="after")
    await plugin.complete_meme_order(event, child, llm_response())
    await plugin.send_meme_after_reply(event)
    assert not event.sent and not event.get_extra("longhub_agent_done")
    await plugin.complete_meme_order(event, root, llm_response())
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["image"]


@pytest.mark.parametrize("mode", ["before", "after", "empty_after"])
async def test_cancellation_during_image_fetch_prevents_delivery(plugin, db, image, mode):
    await publish(db, [image()])
    event = Event()

    async def fetch(url):
        event.set_extra("agent_stop_requested", True)
        return b"GIF89a-test"

    plugin.service.client.image_bytes.side_effect = fetch
    if mode == "before":
        result = await plugin.send_longhub_meme(event, "1", position="before")
        assert "已取消" in result
    else:
        await plugin.send_longhub_meme(event, "1", position="after")
        response = llm_response(text="" if mode == "empty_after" else "回答正文")
        await plugin.complete_meme_order(event, None, response)
        await plugin.send_meme_after_reply(event)
        assert response.result_chain is None
    assert not event.sent and not event.get_extra("longhub_sent")


async def test_after_reply_already_delivered_by_messaging_tool_uses_image_only_result(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()
    await plugin.send_longhub_meme(event, "1", position="after")
    await event.send(plugin_module.MessageChain([plugin_module.Plain("回答正文")]))
    event.set_extra("_send_message_to_user_current_session_plain_texts", ["回答正文"])
    response = llm_response()
    await plugin.complete_meme_order(event, None, response)
    await event.send(response.result_chain)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["回答正文", "image"]


async def test_after_empty_explicit_result_chain_takes_precedence_over_completion_text(
    plugin, plugin_module, db, image
):
    await publish(db, [image()])
    event = Event()
    await plugin.send_longhub_meme(event, "1", position="after")
    response = llm_response(text="框架不会发送的文字", chain=plugin_module.MessageChain())
    await plugin.complete_meme_order(event, None, response)
    await event.send(response.result_chain)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["image"]


@pytest.mark.parametrize("position", ["before", "after"])
@pytest.mark.parametrize("use_chain", [False, True])
async def test_image_only_marker_hides_all_nonstreaming_text(
    plugin, plugin_module, db, image, position, use_chain
):
    await publish(db, [image()])
    event = Event()
    await plugin.send_longhub_meme(event, "1", position=position)
    marker_text = " \n" + plugin_module.IMAGE_ONLY_MARKER + "\n额外确认语"
    response = llm_response(text=marker_text)
    if use_chain:
        response = llm_response(
            text="", chain=plugin_module.MessageChain([plugin_module.Plain(marker_text)])
        )
    await plugin.complete_meme_order(event, None, response)
    assert response.completion_text == ""
    if response.result_chain:
        await event.send(response.result_chain)
    await plugin.send_meme_after_reply(event)
    assert delivered_parts(event) == ["image"] and len(event.sent) == 1


@pytest.mark.parametrize("position", ["before", "after"])
async def test_image_only_marker_is_hidden_at_every_stream_chunk_boundary(
    plugin, plugin_module, db, image, position
):
    await publish(db, [image()])
    marker = plugin_module.IMAGE_ONLY_MARKER
    for split in range(len(marker) + 1):
        event = Event()
        context = object()
        await plugin.prepare_meme_order(event, context)

        async def stream():
            await plugin.send_longhub_meme(event, "1", position=position)
            yield plugin_module.MessageChain([plugin_module.Plain(" \n" + marker[:split])])
            yield plugin_module.MessageChain([plugin_module.Plain(marker[split:] + "\n")])
            yield plugin_module.MessageChain([plugin_module.Plain("不应显示的确认语")])
            await plugin.complete_meme_order(event, context, llm_response(text=marker))

        await event.send_streaming(stream())
        await plugin.send_meme_after_reply(event)
        assert delivered_parts(event) == ["image"] and len(event.sent) == 1, split


async def test_caption_stream_preserves_nonmarker_text(plugin, plugin_module, db, image):
    await publish(db, [image()])
    event = Event()
    context = object()
    await plugin.prepare_meme_order(event, context)

    async def stream():
        await plugin.send_longhub_meme(event, "1", position="before")
        yield plugin_module.MessageChain([plugin_module.Plain("[")])
        yield plugin_module.MessageChain([plugin_module.Plain("开心] 正常配文")])
        await plugin.complete_meme_order(event, context, llm_response(text="[开心] 正常配文"))

    await event.send_streaming(stream())
    assert delivered_parts(event) == ["image", "[开心] 正常配文"]


async def test_marker_is_not_a_control_token_without_a_meme_reply(plugin, plugin_module):
    event = Event()
    await plugin.prepare_meme_order(event, object())

    async def stream():
        yield plugin_module.MessageChain([plugin_module.Plain(plugin_module.IMAGE_ONLY_MARKER)])

    await event.send_streaming(stream())
    assert delivered_parts(event) == [plugin_module.IMAGE_ONLY_MARKER]


@pytest.mark.parametrize("position", ["before", "after"])
async def test_final_marker_response_drops_unflushed_partial_prefix(
    plugin, plugin_module, db, image, position
):
    await publish(db, [image()])
    event = Event()
    context = object()
    await plugin.prepare_meme_order(event, context)

    async def stream():
        await plugin.send_longhub_meme(event, "1", position=position)
        yield plugin_module.MessageChain([plugin_module.Plain(plugin_module.IMAGE_ONLY_MARKER[:8])])
        await plugin.complete_meme_order(
            event, context, llm_response(text=plugin_module.IMAGE_ONLY_MARKER)
        )

    await event.send_streaming(stream())
    assert delivered_parts(event) == ["image"]


async def test_image_only_mode_drops_later_reasoning_text(plugin, plugin_module, db, image):
    await publish(db, [image()])
    event = Event()
    context = object()
    await plugin.prepare_meme_order(event, context)

    async def stream():
        await plugin.send_longhub_meme(event, "1", position="before")
        yield plugin_module.MessageChain([plugin_module.Plain(plugin_module.IMAGE_ONLY_MARKER)])
        reasoning = plugin_module.MessageChain([plugin_module.Plain("不应显示的文字")])
        reasoning.type = "reasoning"
        yield reasoning
        await plugin.complete_meme_order(
            event, context, llm_response(text=plugin_module.IMAGE_ONLY_MARKER)
        )

    await event.send_streaming(stream())
    assert delivered_parts(event) == ["image"]
