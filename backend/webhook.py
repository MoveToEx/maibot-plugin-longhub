from __future__ import annotations

import hashlib
import hmac
import json

from aiohttp import web

from .database import Database
from .models import Settings, image_id


class WebhookServer:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings
        self.runner = None
        self.started = False
        self.error = None
        self.app = web.Application(client_max_size=256 * 1024)
        self.app.router.add_post("/hooks/longhub", self.receive)
        self.app.router.add_get("/health", self.health)

    async def health(self, request):
        return web.json_response({"ok": True})

    async def start(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(
                self.runner, self.settings.webhook_host, self.settings.webhook_port
            ).start()
            self.started = True
        except OSError as exc:
            self.error = self.settings.redact(exc)
            await self.runner.cleanup()

    async def close(self):
        self.started = False
        if self.runner:
            await self.runner.cleanup()

    async def receive(self, request: web.Request):
        try:
            raw = await request.read()
        except web.HTTPRequestEntityTooLarge:
            await self.db.record_webhook(
                body="[oversized]", valid=False, status=413, error="请求体超过 256 KiB"
            )
            return web.json_response({"error": "body too large"}, status=413)
        expected = hmac.new(self.settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
        signature = request.headers.get("X-Signature", "")
        valid = bool(self.settings.webhook_secret) and hmac.compare_digest(
            signature.encode("utf-8"), expected.encode("ascii")
        )
        status, event, ident, error = 204, None, None, None
        if not valid:
            status, error = 401, "invalid signature"
        else:
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict) or payload.get("event") not in (
                    "create",
                    "update",
                    "delete",
                    "restore",
                ):
                    raise ValueError("未知事件")
                event = payload["event"]
                ident = image_id(payload.get("imageId"))
            except (ValueError, TypeError, UnicodeDecodeError):
                status, error = 400, "invalid payload; use the documented template"
        try:
            # Commit both receipt and log before acknowledging delivery.
            await self.db.record_webhook(
                body=self.settings.redact(raw.decode("utf-8", "replace"))
                if valid
                else "[invalid signature body omitted]",
                valid=valid,
                status=status,
                event=event,
                ident=ident,
                error=error,
            )
        except Exception:
            return web.json_response({"error": "journal unavailable"}, status=503)
        return (
            web.Response(status=204)
            if status == 204
            else web.json_response({"error": error}, status=status)
        )
