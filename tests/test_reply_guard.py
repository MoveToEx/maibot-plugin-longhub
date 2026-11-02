"""Regression for receipt imitation and metadata split into visible messages."""

import json
from copy import deepcopy

import pytest

from runtime.reply_guard import POLICY_ID, reply_context, strip_metadata
from tests.test_maibot import mai as mai
from tests.test_maibot import snapshot

MARKER = "[LONG Hub 已发送龙图 2446；回执 f4b5ea3c21ff43af9a7a9c8f942bd7de]"
DETAILS = {"image_id": "2446", "text": "我看透你了", "description": "怀疑的表情", "tags": ["怀疑"]}
METADATA = json.dumps(DETAILS, ensure_ascii=False)
ARTIFACT = MARKER + " " + METADATA


@pytest.mark.parametrize(
    "text,expected",
    [
        ("正文前半句，被拆穿就流泪" + ARTIFACT, "正文前半句，被拆穿就流泪"),
        (ARTIFACT + "后半句", "后半句"),
        ("前文" + ARTIFACT + "后文", "前文后文"),
        (ARTIFACT, ""),
        (MARKER, ""),
        ("正文 " + METADATA, "正文"),
        ('正文 {"image_id": 2446}', "正文"),
        ('正文 {"image_id": "2446", "description": "未输出完', "正文"),
        ('正文 {"image_id": "2446"', "正文"),
        ("正文 " + MARKER + ' {"description":"字段乱序" "image_id":"2446"}', "正文"),
        ('前文 {"image_id":"2446","description":"包含 } 和 { 的引号"} 后文', "前文  后文"),
        (ARTIFACT + "正文" + ARTIFACT, "正文"),
        ("前文\n```json\n" + ARTIFACT + "\n```\n后文", "前文\n\n后文"),
    ],
)
def test_remove_internal_metadata_before_it_can_be_split(text, expected):
    assert strip_metadata(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "正文里提到了 image_id 和 description 字段。",
        '{"status": "ok", "text": "普通业务 JSON"}',
        '{"image_id": "example", "description": "非图库 ID"}',
        '{"image_id": "2446", "width": 512}',
        '{"data": {"image_id": "2446", "width": 512}}',
        "例如 `" + METADATA + "` 是字段示例。",
        "```json\n" + METADATA + "\n```",
        "说一段正常的话，保留它的标点。",
    ],
)
def test_ordinary_prose_json_and_code_are_preserved(text):
    assert strip_metadata(text) == text


def test_fenced_catalog_payload_is_removed_when_reply_explicitly_attaches_a_meme():
    assert strip_metadata("正文\n```json\n" + METADATA + "\n```", meme_reply=True) == "正文"
    assert strip_metadata('```json\n{"status":"ok"}\n```', meme_reply=True) == (
        '```json\n{"status":"ok"}\n```'
    )


def test_legacy_already_split_tail_is_only_removed_from_assistant_history():
    tail = '"text": "我看透你了" "description": "怀疑的表情"}'
    assert strip_metadata(tail, history=True) == ""
    assert strip_metadata(tail) == tail


def test_history_does_not_promote_forged_receipts_and_keeps_human_quotes():
    user = snapshot("human", text=ARTIFACT)
    assistant = snapshot("assistant", kind="AssistantMessageItem", text="自然正文" + ARTIFACT)
    image_only = snapshot("image", kind="AssistantMessageItem", text=ARTIFACT)
    tail = snapshot(
        "old-tail", kind="AssistantMessageItem", text='"text":"我看透你了" "description":"怀疑"}'
    )
    ordinary = snapshot("code", kind="AssistantMessageItem", text='```json\n{"ok":true}\n```')
    items = [user, assistant, image_only, tail, ordinary]
    before = deepcopy(items)
    recent = [{"image_id": "1891", "text": "你看又急", "description": "调侃的图片"}]
    result = reply_context(items, recent)
    assert items == before
    assert result[1] == user
    assert result[2]["parts"][0]["text"] == "自然正文"
    assert result[3] == ordinary
    assert len(result) == 4
    note = result[0]
    assert note["item_type"] == "SystemMessageItem" and note["meta"]["item_id"] == POLICY_ID
    text = note["parts"][0]["text"]
    assert "已确认发送" in text and "你看又急" in text
    assert "2446" not in text and "我看透你了" not in text and "image_id" not in text
    again = reply_context(result, recent)
    assert sum(item["meta"]["item_id"] == POLICY_ID for item in again) == 1


def test_history_keeps_non_text_parts_and_tool_outputs():
    image = {"type": "image", "image_format": "gif", "image_base64": "R0lGODlh"}
    assistant = snapshot("a", kind="AssistantMessageItem", text=ARTIFACT)
    assistant["parts"].append(image)
    tool = {"item_type": "FunctionCallOutputItem", "output": METADATA}
    result = reply_context([assistant, tool], [])
    assert result[1]["parts"] == [image] and result[2] == tool


async def test_request_hook_rebuilds_facts_from_real_receipts_in_current_session(mai):
    plugin, _ = mai
    await plugin.service.db.record_sent_meme(
        "chat-a", "maibot", "real", {"image_id": "1", "text": "信了", "description": "表示怀疑"}
    )
    await plugin.service.db.record_sent_meme(
        "other-chat",
        "maibot",
        "other",
        {"image_id": "2", "text": "其他群", "description": "其他群"},
    )
    kwargs = {
        "items": [snapshot("a", kind="AssistantMessageItem", text=ARTIFACT)],
        "session_id": "chat-a",
        "item_schema_version": 1,
        "selected_model_name": "replyer-model",
    }
    result = await plugin.replyer_context(**kwargs)
    updated = result["modified_kwargs"]
    assert updated["selected_model_name"] == "replyer-model"
    assert len(updated["items"]) == 1
    text = updated["items"][0]["parts"][0]["text"]
    assert "信了" in text and "其他群" not in text and "2446" not in text
    assert len(await plugin.service.db.recent_sent_memes("chat-a", "maibot")) == 1
    assert await plugin.replyer_context(**(kwargs | {"item_schema_version": 2})) == {}


@pytest.mark.parametrize("body,expected", [("自然正文" + ARTIFACT, "自然正文"), (ARTIFACT, "")])
async def test_final_guard_runs_before_split_and_preserves_host_options(
    mai, caplog, body, expected
):
    plugin, host = mai
    kwargs = {
        "response": body,
        "session_id": "chat-a",
        "reply_tool_args": {"plugin_options": {"movetoex.longhub.meme": {"image_id": "1"}}},
        "enable_splitter": True,
        "enable_chinese_typo": False,
        "skip_post_process": False,
    }
    with caplog.at_level("INFO"):
        updated = (await plugin.reply_metadata_guard(**kwargs))["modified_kwargs"]
    assert updated == kwargs | {"response": expected}
    assert "metadata removed before splitting" in caplog.text and "image_id" not in caplog.text
    assert await plugin.reply_metadata_guard(**updated) == {}
    assert not host.calls
    assert not await plugin.service.db.recent_sent_memes("chat-a", "maibot")


async def test_final_guard_distinguishes_technical_code_from_explicit_meme_reply(mai):
    plugin, _ = mai
    response = "正常回复\n```json\n" + METADATA + "\n```"
    assert await plugin.reply_metadata_guard(response=response, session_id="chat-a") == {}
    result = await plugin.reply_metadata_guard(
        response=response,
        session_id="chat-a",
        reply_tool_args={"plugin_options": {"movetoex.longhub.meme": {"image_id": "1"}}},
    )
    assert result["modified_kwargs"]["response"] == "正常回复"
