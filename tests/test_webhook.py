import hashlib
import hmac
import json

import aiohttp

from backend.models import Settings
from backend.webhook import WebhookServer


def sign(body, secret="测试密钥"):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_signed_receipt_persisted_before_ack(serve, db):
    server = WebhookServer(db, Settings(webhook_secret="测试密钥"))
    base = await serve(server.app)
    ident = 2**63 - 1
    body = json.dumps({"event": "restore", "imageId": ident}).encode()
    async with aiohttp.ClientSession() as session:
        for _ in range(2):
            async with session.post(
                base + "/hooks/longhub", data=body, headers={"X-Signature": sign(body)}
            ) as response:
                assert response.status == 204
                assert await response.read() == b""
                assert len(await db.pending(all_pending=True)) >= 1
    pending = await db.pending(all_pending=True)
    assert len(pending) == 2 and all(x["image_id"] == ident for x in pending)
    logs = await db.logs()
    assert logs["total"] == 2 and logs["items"][0]["image_id"] == str(ident)


async def test_invalid_requests_are_logged_not_applied(serve, db):
    server = WebhookServer(db, Settings(webhook_secret="测试密钥"))
    base = await serve(server.app)
    async with aiohttp.ClientSession() as session:
        body = b'{"event":"delete","imageId":1}'
        async with session.post(
            base + "/hooks/longhub", data=body, headers={"X-Signature": "wrong"}
        ) as response:
            assert response.status == 401
        for body in [
            b"bad-json",
            b'{"event":"unknown","imageId":1}',
            b'{"event":"create","imageId":true}',
        ]:
            async with session.post(
                base + "/hooks/longhub", data=body, headers={"X-Signature": sign(body)}
            ) as response:
                assert response.status == 400
        async with session.post(base + "/hooks/longhub", data=b"a" * (256 * 1024 + 1)) as response:
            assert response.status == 413
    assert not await db.pending(all_pending=True)
    assert (await db.logs(failed=True))["total"] == 5


async def test_no_success_ack_when_journal_fails(serve, db, monkeypatch):
    server = WebhookServer(db, Settings(webhook_secret="测试密钥"))
    base = await serve(server.app)

    async def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(db, "record_webhook", fail)
    body = b'{"event":"create","imageId":1}'
    async with aiohttp.ClientSession() as session:
        async with session.post(
            base + "/hooks/longhub", data=body, headers={"X-Signature": sign(body)}
        ) as response:
            assert response.status == 503


async def test_empty_secret_never_authenticates(serve, db):
    server = WebhookServer(db, Settings())
    base = await serve(server.app)
    body = b'{"event":"create","imageId":1}'
    async with aiohttp.ClientSession() as session:
        async with session.post(
            base + "/hooks/longhub", data=body, headers={"X-Signature": sign(body, "")}
        ) as response:
            assert response.status == 401
