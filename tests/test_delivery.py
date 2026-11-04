"""Native send confirmation without model-visible correlation markers."""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from runtime.delivery import Deliveries, Receipt

ITEM = {
    "id": "2446",
    "text": "我看透你了",
    "description": "怀疑的龙脸",
    "effective_tags": ["怀疑"],
}


def pending(deliveries, *, session="chat-a", turn="turn-a", status="attempted"):
    deliveries.enter(session, turn)
    receipt = deliveries.reserve(session, ITEM)
    segment = receipt.segment(b"GIF89a", base64.b64encode(b"GIF89a").decode())
    receipt.status = status
    return receipt, {
        "session_id": session,
        "message_id": "native-message-a",
        "raw_message": [segment],
    }


async def test_confirmed_send_keeps_private_receipt_and_semantic_history_separate():
    deliveries, db = Deliveries(), SimpleNamespace(record_sent_meme=AsyncMock())
    receipt, message = pending(deliveries)
    label = message["raw_message"][0]["data"]
    assert "我看透你了" in label and "怀疑的龙脸" in label
    assert not any(value in label for value in (receipt.id, "2446", "回执", "image_id", "tags"))
    await deliveries.confirm(message, True, db)
    assert receipt.status == "confirmed" and receipt.message_id == "native-message-a"
    assert deliveries.current("chat-a").replied
    db.record_sent_meme.assert_awaited_once_with("chat-a", "maibot", receipt.id, receipt.details)


async def test_duplicate_native_callback_cannot_confirm_a_later_send_of_the_same_image():
    deliveries, db = Deliveries(), SimpleNamespace(record_sent_meme=AsyncMock())
    first, message = pending(deliveries)
    await deliveries.confirm(message, True, db)
    second, next_message = pending(deliveries, turn="turn-b")
    await deliveries.confirm(message, True, db)
    assert second.status == "attempted" and not deliveries.current("chat-a").replied
    assert db.record_sent_meme.await_count == 1
    next_message["message_id"] = "native-message-b"
    await deliveries.confirm(next_message, True, db)
    assert second.status == "confirmed" and db.record_sent_meme.await_count == 2
    assert first.id != second.id


@pytest.mark.parametrize(
    "mode", ["missing-id", "wrong-session", "wrong-hash", "wrong-label", "drop", "prepared"]
)
async def test_unrelated_or_unidentified_callbacks_are_not_send_facts(mode):
    deliveries, db = Deliveries(), SimpleNamespace(record_sent_meme=AsyncMock())
    receipt, message = pending(deliveries, status="prepared" if mode == "prepared" else "attached")
    if mode == "missing-id":
        message.pop("message_id")
    elif mode == "wrong-session":
        message["session_id"] = "other-chat"
    elif mode == "wrong-hash":
        message["raw_message"][0]["hash"] = "other"
    elif mode == "wrong-label":
        message["raw_message"][0]["data"] = "another image"
    elif mode == "drop":
        message["raw_message"] = []
    await deliveries.confirm(message, True, db)
    assert receipt.status != "confirmed" and not deliveries.current("chat-a").replied
    db.record_sent_meme.assert_not_awaited()


async def test_failed_callback_is_deduplicated_and_does_not_block_a_new_turn():
    deliveries, db = Deliveries(), SimpleNamespace(record_sent_meme=AsyncMock())
    first, message = pending(deliveries)
    await deliveries.confirm(message, False, db)
    assert first.status == "failed"
    second, next_message = pending(deliveries, turn="turn-b")
    await deliveries.confirm(message, True, db)
    assert second.status == "attempted"
    db.record_sent_meme.assert_not_awaited()
    next_message["message_id"] = "native-message-b"
    await deliveries.confirm(next_message, True, db)
    assert second.status == "confirmed"


def test_only_one_pending_send_per_session_even_after_a_new_human_message():
    deliveries = Deliveries()
    pending(deliveries)
    deliveries.enter("chat-a", "turn-b")
    with pytest.raises(ValueError, match="等待发送确认"):
        deliveries.reserve("chat-a", ITEM)
    pending(deliveries, session="chat-b")


async def test_ambiguous_pending_receipts_never_claim_a_send():
    deliveries, db = Deliveries(), SimpleNamespace(record_sent_meme=AsyncMock())
    first, message = pending(deliveries)
    duplicate = Receipt("chat-a", "another-turn", "2446", first.details, status="attached")
    duplicate.segment(b"GIF89a", base64.b64encode(b"GIF89a").decode())
    deliveries.receipts[duplicate.id] = duplicate
    await deliveries.confirm(message, True, db)
    assert first.status == "attempted" and duplicate.status == "attached"
    db.record_sent_meme.assert_not_awaited()
