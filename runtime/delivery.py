"""Track exact outgoing emoji segments using native post-send receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class Turn:
    key: str
    searched: bool = False
    receipt: str = ""
    context_only: bool = False
    anchor: str = ""
    replied: bool = False
    logical_turn_id: str = ""
    reply_baseline: set[str] = field(default_factory=set)


@dataclass
class Receipt:
    session: str
    turn: str
    image_id: str
    details: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = field(default_factory=time.monotonic)
    digest: str = ""
    label: str = ""
    status: str = "prepared"

    def segment(self, data, encoded):
        self.digest = hashlib.sha256(data).hexdigest()
        self.label = f"[LONG Hub 已发送龙图 {self.image_id}；回执 {self.id}] " + json.dumps(
            self.details, ensure_ascii=False
        )
        return {
            "type": "emoji",
            "data": self.label,
            "hash": self.digest,
            "binary_data_base64": encoded,
        }


@dataclass
class PreparedReply:
    session: str
    turn: str
    parameters: dict
    receipt: str = ""


class Deliveries:
    def __init__(self):
        self.turns = OrderedDict()
        self.receipts = OrderedDict()
        self.reply_ids = OrderedDict()
        self.context_requests = {}

    def enter(self, session, key, completed=()):
        self._prune()
        previous = self.turns.get(session)
        context_requested = self.context_requests.pop(session, 0) > time.monotonic()
        if context_requested or previous is None or (key and previous.anchor != key):
            previous = Turn(
                uuid.uuid4().hex if context_requested else key,
                anchor=key,
                context_only=context_requested,
                reply_baseline=set(completed) if context_requested else set(),
            )
            self.turns[session] = previous
        if set(completed) - previous.reply_baseline:
            previous.replied = True
        self.turns.move_to_end(session)
        return previous

    def current(self, session):
        value = self.turns.get(session)
        if value is None or not value.key:
            raise ValueError("缺少 Planner 当前会话上下文，请在当前对话中调用")
        return value

    def reserve(self, session, item):
        turn = self.current(session)
        if turn.receipt:
            existing = self.receipts.get(turn.receipt)
            if existing is not None and existing.status != "failed":
                raise ValueError("本轮已经准备或发送过一张龙图，请勿重复发送")
        receipt = Receipt(
            session,
            turn.key,
            str(item["id"]),
            {
                "image_id": str(item["id"]),
                "text": item["text"][:150],
                "description": item["description"][:200],
                "tags": item["effective_tags"][:8],
            },
        )
        self.receipts[receipt.id] = receipt
        turn.receipt = receipt.id
        self._prune()
        return receipt

    async def confirm(self, message, sent, db):
        session = message.get("session_id")
        segments = message.get("raw_message", [])
        if not session or not isinstance(segments, list):
            return
        for receipt in list(self.receipts.values()):
            if receipt.session != session or receipt.status in {"confirmed", "failed"}:
                continue
            matching = any(
                segment.get("type") == "emoji"
                and segment.get("hash") == receipt.digest
                and segment.get("data") == receipt.label
                for segment in segments
                if isinstance(segment, dict)
            )
            if not matching:
                continue
            if sent is not True:
                receipt.status = "failed"
                continue
            receipt.status = "confirmed"
            turn = self.turns.get(session)
            if turn is not None and turn.key == receipt.turn:
                turn.replied = True
            # The IM send already happened. Finish recording its fact even when the
            # caller cancels its RPC after receiving a host-side send timeout.
            task = asyncio.create_task(
                db.record_sent_meme(session, "maibot", receipt.id, receipt.details)
            )
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    def _prune(self):
        now = time.monotonic()
        for key, receipt in list(self.receipts.items()):
            if now - receipt.created > 600:
                self.receipts.pop(key, None)
        for mapping in (self.turns, self.reply_ids):
            while len(mapping) > 1024:
                mapping.popitem(last=False)
        while len(self.receipts) > 1024:
            self.receipts.popitem(last=False)
        self.context_requests = {
            key: expiry for key, expiry in self.context_requests.items() if expiry > now
        }


def check_frame(messages):
    # Host IPC is limited to 16 MiB, including the enclosing RPC payload.
    if len(json.dumps(messages, ensure_ascii=False).encode()) > 16 * 1024 * 1024 - 128 * 1024:
        raise ValueError("图片或整组回复超过 MaiBot IPC 大小限制，请选择另一张龙图")
