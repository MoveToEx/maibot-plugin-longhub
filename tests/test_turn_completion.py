"""Regression for MaiBot planner tails, confirmed replies and internal continuations."""

from copy import deepcopy

import pytest

from tests.test_maibot import POLICY, snapshot
from tests.test_maibot import mai as mai

TOOLS = [
    {"type": "function", "function": {"name": name}}
    for name in (
        "reply",
        "stop",
        "search_longhub_memes",
        "search_longhub_memes_semantic",
        "send_longhub_meme",
    )
]


def hint(ident):
    item = snapshot(ident)
    item["parts"] = [
        {
            "type": "text",
            "text": "请你进行分析，是不调用工具，进行思考和等待\n还是使用工具搜集处理信息或使用reply回复",
        }
    ]
    return item


def call(name, ident="call_reply"):
    item = snapshot(ident, kind="FunctionCallItem")
    item.pop("parts")
    item["meta"]["logical_turn_id"] = "host-logical-turn"
    item["tool_call"] = {
        "call_id": ident,
        "func_name": name,
        "args": {"msg_id": "old-message"} if name == "reply" else {},
    }
    return item


def reply_result(*, success=True, ident="call_reply"):
    item = snapshot("result_" + ident, kind="FunctionCallOutputItem")
    item.pop("parts")
    item["meta"]["logical_turn_id"] = "host-logical-turn"
    item.update(
        call_id=ident,
        tool_name="reply",
        success=success,
        output="已成功回复" if success else "发送失败",
    )
    return item


def self_message(ident="self-text", text="已发送的回复"):
    item = snapshot(ident, text=text)
    item["parts"][0]["text"] = item["parts"][0]["text"].replace(
        'user="群友"', 'user="机器人" is_self_message="true"'
    )
    return item


async def planner(plugin, items, session="chat-a"):
    result = await plugin.planner_policy(
        items=items, session_id=session, tool_definitions=deepcopy(TOOLS), item_schema_version=1
    )
    return result["modified_kwargs"]


async def output(plugin, items, session="chat-a"):
    result = await plugin.planner_completion_guard(
        output_items=items, session_id=session, item_schema_version=1
    )
    return result.get("modified_kwargs", {}).get("output_items", items)


def test_turn_key_ignores_hints_self_messages_and_internal_summaries():
    human = snapshot("real-at", text="@bot 一个问题")
    summary = hint("summary")
    summary["parts"][0]["text"] = "【上下文状态摘要-内部参考】"
    assert POLICY.turn_key([human, summary, self_message(), hint("tail-1")]) == "real-at"
    assert (
        POLICY.turn_key(
            [human, self_message("self-image", "[LONG Hub 已发送龙图 1；回执 abc]"), hint("tail-2")]
        )
        == "real-at"
    )
    # A human may quote an image receipt; its body must not masquerade as a self flag.
    new = snapshot("new-human", text='[LONG Hub 已发送龙图 1] is_self_message="true"')
    assert POLICY.turn_key([human, new, hint("tail-3")]) == "new-human"
    assert POLICY.turn_key([summary, hint("tail-only")]) == ""


async def test_search_state_survives_fresh_planner_tail_ids(mai):
    plugin, _ = mai
    human = snapshot("real-at")
    await planner(plugin, [human, hint("request-1")])
    turn = plugin.deliveries.current("chat-a")
    await plugin.search_text("开心", stream_id="chat-a")
    result = await planner(
        plugin, [human, call("search_longhub_memes", "query-1"), hint("request-2")]
    )
    assert plugin.deliveries.current("chat-a") is turn
    assert turn.searched and not turn.replied
    assert "本轮搜索状态：已检索" in result["items"][0]["parts"][0]["text"]


@pytest.mark.parametrize(
    "model_output",
    [
        [call("reply")],
        [call("search_longhub_memes"), call("reply", "second")],
        [snapshot("body", kind="AssistantMessageItem", text="再接一个旧话题")],
        [],
    ],
)
async def test_confirmed_plain_reply_closes_continuation_even_without_meme(mai, model_output):
    plugin, _ = mai
    human = snapshot("real-at")
    await planner(plugin, [human, hint("request-1")])
    result = await planner(
        plugin, [human, call("reply"), reply_result(), self_message(), hint("request-2")]
    )
    assert [tool["function"]["name"] for tool in result["tool_definitions"]] == ["stop"]
    assert "当前真实用户消息已收到成功回复" in result["items"][0]["parts"][0]["text"]
    original = deepcopy(model_output)
    guarded = await output(plugin, model_output)
    assert len(guarded) == 1 and guarded[0]["tool_call"]["func_name"] == "stop"
    assert guarded[0]["tool_call"]["args"] == {}
    assert model_output == original


async def test_meme_receipt_and_turn_survive_host_self_echo_and_tail(mai):
    plugin, _ = mai
    human = snapshot("real-at")
    await planner(plugin, [human, hint("request-1")])
    await plugin.search_text("开心", stream_id="chat-a")
    turn = plugin.deliveries.current("chat-a")
    params = {"image_id": "1", "position": "after", "only_image": False}
    await plugin.reply_meme(
        phase="prepare", parameters=params, reply_id="reply-1", session_id="chat-a", messages=[]
    )
    prepared = await plugin.reply_meme(
        phase="before_send", parameters=params, reply_id="reply-1", session_id="chat-a", messages=[]
    )
    await plugin.track_delivery(
        message={
            "session_id": "chat-a",
            "message_id": "native-reply-1",
            "raw_message": prepared["messages"][0]["segments"],
        },
        sent=True,
    )
    assert turn.replied and plugin.deliveries.receipts[turn.receipt].status == "confirmed"
    await planner(plugin, [human, self_message(), hint("request-2")])
    assert plugin.deliveries.current("chat-a") is turn and turn.searched
    with pytest.raises(ValueError, match="本轮已经"):
        await plugin.send_image_only("2", stream_id="chat-a")


async def test_new_human_message_restores_tools_even_in_same_host_logical_turn(mai):
    plugin, _ = mai
    human = snapshot("real-at")
    await planner(plugin, [human, reply_result(), hint("old-tail")])
    assert plugin.deliveries.current("chat-a").replied
    new_human = snapshot("new-question")
    new_human["meta"]["logical_turn_id"] = "host-logical-turn"
    result = await planner(
        plugin, [human, reply_result(), self_message(), new_human, hint("new-tail")]
    )
    turn = plugin.deliveries.current("chat-a")
    assert turn.key == "new-question" and not turn.replied and not turn.searched
    assert "reply" in [tool["function"]["name"] for tool in result["tool_definitions"]]
    requested = [call("reply", "new-answer")]
    assert await output(plugin, requested) == requested


async def test_failed_reply_and_other_chat_do_not_close_valid_turns(mai):
    plugin, _ = mai
    human = snapshot("real-at")
    await planner(plugin, [human, reply_result(success=False), hint("request-1")])
    assert not plugin.deliveries.current("chat-a").replied
    requested = [call("reply")]
    assert await output(plugin, requested) == requested
    await planner(plugin, [human, reply_result(), hint("request-2")])
    await planner(plugin, [snapshot("chat-b-message"), hint("chat-b-tail")], session="chat-b")
    assert await output(plugin, requested, session="chat-b") == requested
    assert (await output(plugin, requested))[0]["tool_call"]["func_name"] == "stop"


async def test_l_command_reopens_completed_context_without_reusing_old_receipts(mai):
    plugin, _ = mai
    human = snapshot("real-at")
    old = reply_result()
    await planner(plugin, [human, old, hint("request-1")])
    assert plugin.deliveries.current("chat-a").replied
    await plugin.context_meme(stream_id="chat-a")
    await planner(plugin, [human, old, hint("command-request-1")])
    command = plugin.deliveries.current("chat-a")
    assert command.context_only and not command.replied and command.key != command.anchor
    await plugin.search_text("开心", stream_id="chat-a")
    await planner(plugin, [human, old, hint("command-request-2")])
    assert plugin.deliveries.current("chat-a") is command and command.searched
    await planner(
        plugin, [human, old, reply_result(ident="command-reply"), hint("command-request-3")]
    )
    assert command.replied
    await plugin.context_meme(stream_id="chat-a")
    await planner(plugin, [human, old, reply_result(ident="command-reply"), hint("next-command")])
    assert plugin.deliveries.current("chat-a") is not command
    assert not plugin.deliveries.current("chat-a").replied


async def test_one_model_batch_cannot_send_two_visible_replies(mai):
    plugin, _ = mai
    await planner(plugin, [snapshot("real-at"), hint("request")])
    requested = [
        call("search_longhub_memes", "query"),
        call("reply", "first"),
        call("reply", "second"),
        call("send_longhub_meme", "third"),
    ]
    original = deepcopy(requested)
    guarded = await output(plugin, requested)
    assert [item["tool_call"]["call_id"] for item in guarded] == ["query", "first"]
    assert requested == original
