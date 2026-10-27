from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .client import HubClient, HubError
from .database import Database
from .models import Settings

logger = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, db: Database, client: HubClient, settings: Settings):
        self.db, self.client, self.settings = db, client, settings
        self.write_lock = asyncio.Lock()
        self.status = {"status": "idle"}

    async def _version_metadata(self):
        try:
            return {
                "api_base": self.settings.api_base,
                "catalog_timestamp": await self.client.catalog_version(),
            }
        except HubError as exc:
            if exc.status in (401, 403, 429):
                raise
            error = self.settings.redact(exc)
        except Exception as exc:
            error = self.settings.redact(exc)
        logger.warning("LONG Hub 目录版本检查失败，将执行全量同步：%s", error)
        # Unknown is distinct from a successful response containing timestamp=null.
        return {"api_base": self.settings.api_base}

    async def sync(self, *, force=False):
        generation = None
        job_id = self.status.get("id") if self.status.get("status") == "queued" else None
        self.status = {
            "id": job_id or uuid.uuid4().hex,
            "status": "running",
            "imported": 0,
            "phase": "checking_version",
            "started_at": time.time(),
        }
        try:
            # Receiver writes only to the durable journal and never takes this lock.
            async with self.write_lock:
                version = await self._version_metadata()
                previous = await self.db.get_meta("last_sync", {})
                if (
                    not force
                    and "catalog_timestamp" in version
                    and "catalog_timestamp" in previous
                    and previous["catalog_timestamp"] == version["catalog_timestamp"]
                    and previous.get("api_base") == version["api_base"]
                    and await self.db.get_meta("active_generation")
                ):
                    self.status.update(
                        status="done",
                        skipped=True,
                        reason="unchanged",
                        completed_at=time.time(),
                        **version,
                    )
                    await self.db.set_meta("sync_error", None)
                    logger.info(
                        "LONG Hub 目录版本未变化，跳过全量同步（timestamp=%s）",
                        version["catalog_timestamp"],
                    )
                    return
                self.status["phase"] = "downloading"
                generation = await self.db.new_generation()

                async def consume(batch):
                    await self.db.stage_batch(generation, batch)
                    self.status["imported"] += len(batch)

                summary = {**await self.client.export(consume), **version}
                # Only the pre-export hint belongs to this snapshot. A later
                # version could include changes outside its repeatable-read view.
                # This finite receipt set is the handoff boundary. Later arrivals remain pending.
                self.status["phase"] = "replaying_webhooks"
                receipts = await self.db.pending(all_pending=True)
                grouped = {}
                for receipt in receipts:
                    grouped.setdefault(receipt["image_id"], []).append(receipt)
                self.status["refresh_total"] = len(grouped)
                self.status["refreshed"] = 0
                for ident in grouped:
                    current = await self.client.get_image(ident)
                    # Do not acknowledge staging writes until the atomic generation switch.
                    await self.db.refresh(generation, ident, current)
                    self.status["refreshed"] += 1
                await self.db.publish(generation, summary, [r["id"] for r in receipts])
                self.status.update(status="done", completed_at=time.time(), **summary)
                await self.db.set_meta("sync_error", None)
        except asyncio.CancelledError:
            self.status["status"] = "cancelled"
            raise
        except Exception as exc:
            error = self.settings.redact(exc)
            self.status.update(
                status="failed",
                error=error,
                retry_after=exc.retry_after if isinstance(exc, HubError) else 0,
                auth_error=isinstance(exc, HubError) and exc.status in (401, 403),
            )
            await self.db.set_meta("sync_error", error)
            raise
        finally:
            if generation:
                await self.db.discard(generation)

    async def consume_once(self) -> bool:
        async with self.write_lock:
            generation = await self.db.get_meta("active_generation")
            if not generation:
                return False
            receipts = await self.db.pending()
            if not receipts:
                return False
            ident = receipts[0]["image_id"]
            try:
                current = await self.client.get_image(ident)
                await self.db.refresh(generation, ident, current, [r["id"] for r in receipts])
            except Exception as exc:
                await self.db.fail_receipts(receipts, self.settings.redact(exc))
            return True
