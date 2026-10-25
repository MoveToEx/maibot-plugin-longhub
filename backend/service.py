from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from .client import HubClient
from .database import Database
from .models import Settings
from .sync import SyncEngine
from .tagging import Tagger
from .webhook import WebhookServer

logger = logging.getLogger(__name__)


class Service:
    def __init__(self, data_dir: Path, settings: Settings, generate):
        self.settings = settings
        self.db = Database(data_dir / "catalog.sqlite3")
        self.client = HubClient(settings)
        self.sync = SyncEngine(self.db, self.client, settings)
        self.webhook = WebhookServer(self.db, settings)
        self.tagger = Tagger(self.db, self.client, settings, generate)
        self.tasks = []
        self.sync_task = None
        self.stopping = False
        self.background_error = None
        self.next_sync = 0.0
        self.sync_failures = 0

    async def start(self):
        try:
            await self.db.open()
            await self.client.open()
            await self.webhook.start()
            self.tasks = [
                asyncio.create_task(self._consumer()),
                asyncio.create_task(self._scheduler()),
            ]
            self.tasks.extend(
                asyncio.create_task(self._tag_worker())
                for _ in range(self.settings.tagging_concurrency)
            )
            if self.settings.ready and self.webhook.started:
                self.start_sync()
        except BaseException:
            await self.close()
            raise

    async def close(self):
        self.stopping = True
        tasks = [*self.tasks, *([self.sync_task] if self.sync_task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.webhook.close()
        await self.client.close()
        await self.db.close()

    def start_sync(self):
        if not self.settings.ready:
            raise ValueError("请填写 Appkey、webhook 密钥，并确认 Hub 上的订阅已启用")
        if not self.webhook.started:
            raise ValueError("Webhook 接收器未成功启动，请检查监听地址和端口")
        if self.sync_task and not self.sync_task.done():
            return self.sync.status
        # Set status before yielding so the caller receives the correct job ID.
        self.sync.status = {"id": uuid.uuid4().hex, "status": "queued"}
        self.sync_task = asyncio.create_task(self._run_sync())
        return self.sync.status

    async def _run_sync(self):
        try:
            await self.sync.sync()
            self.sync_failures = 0
            self.next_sync = time.time() + self.settings.sync_interval_hours * 3600
        except asyncio.CancelledError:
            raise
        except Exception:
            self.sync_failures += 1
            if self.sync.status.get("auth_error"):
                self.next_sync = float("inf")
            else:
                self.next_sync = time.time() + max(
                    min(3600, 60 * 2 ** min(self.sync_failures - 1, 6)),
                    self.sync.status.get("retry_after", 0),
                )

    async def _consumer(self):
        while True:
            try:
                worked = await self.sync.consume_once()
                if not worked:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.background_error = self.settings.redact(exc)
                await asyncio.sleep(5)

    async def _tag_worker(self):
        while True:
            try:
                worked = await self.tagger.run_once()
                if not worked:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.background_error = self.settings.redact(exc)
                await asyncio.sleep(5)

    async def _scheduler(self):
        prune_at = 0
        while True:
            try:
                now = time.time()
                if now >= prune_at:
                    await self.db.prune_logs(
                        self.settings.webhook_log_days, self.settings.webhook_log_limit
                    )
                    prune_at = now + 3600
                if (
                    self.settings.ready
                    and self.webhook.started
                    and now >= self.next_sync
                    and (not self.sync_task or self.sync_task.done())
                ):
                    self.start_sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.background_error = self.settings.redact(exc)
            await asyncio.sleep(5)

    async def status(self):
        return {
            **await self.db.stats(),
            "ready": self.settings.ready,
            "receiver": {
                "started": self.webhook.started,
                "error": self.webhook.error,
                "listen": f"{self.settings.webhook_host}:{self.settings.webhook_port}",
                "path": "/hooks/longhub",
            },
            "sync": self.sync.status,
            "sync_error": await self.db.get_meta("sync_error"),
            "background_error": self.background_error,
            "vision_configured": bool(self.settings.vision_provider_id),
        }

    async def candidates(self, query, *, limit=None, recent=()):
        if not query.strip():
            return []
        result = await self.db.search(
            query, limit or self.settings.candidate_limit, self.settings.allowed_ratings, recent
        )
        if self.settings.auto_tagging and self.settings.vision_provider_id:
            await self.db.enqueue_tags(
                [int(x["id"]) for x in result], provider_id=self.settings.vision_provider_id
            )
        return [
            {
                "id": x["id"],
                "text": x["text"][:150],
                "rating": x["rating"],
                "description": x["description"][:250],
                "tags": x["effective_tags"][:24],
            }
            for x in result
        ]

    async def bulk_tags(self, ids=None, *, force=False):
        if not self.settings.vision_provider_id:
            raise ValueError("请先选择支持原生视觉输入的 AstrBot 提供商")
        if ids is not None:
            count = await self.db.enqueue_tags(
                ids, force=force, provider_id=self.settings.vision_provider_id
            )
        else:
            count = 0
            # Snapshot IDs first: completed jobs must not shift OFFSET pagination.
            missing = await self.db.missing_annotation_ids()
            for offset in range(0, len(missing), 100):
                count += await self.db.enqueue_tags(
                    missing[offset : offset + 100], provider_id=self.settings.vision_provider_id
                )
        return {"queued": count}
