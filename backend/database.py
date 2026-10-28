from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

from sqlalchemy import (
    Text,
    and_,
    case,
    cast,
    delete,
    func,
    insert,
    or_,
    select,
    type_coerce,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from .models import PROMPT_VERSION, RATINGS, normalize_image, tokenize
from .orm import (
    Annotation,
    Base,
    CatalogImage,
    Generation,
    Meta,
    Notification,
    Override,
    SearchEntry,
    SentMeme,
    TagJob,
    WebhookLog,
    configure_sqlite,
    entity_dict,
    image_view,
    json_dump,
)
from .sql_query import query_catalog, validate_query


class Database:
    """Short-lived ORM transactions; no session spans external I/O."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.query_lock = asyncio.Lock()
        self.catalog_revision = 0
        self.engine = None
        self.sessions: async_sessionmaker[AsyncSession] | None = None

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(self.path.resolve())),
            json_serializer=json_dump,
            poolclass=AsyncAdaptedQueuePool,
            pool_size=1,
            max_overflow=0,
        )
        configure_sqlite(self.engine.sync_engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        try:
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.exec_driver_sql("PRAGMA user_version=2")
            async with self.transaction() as session:
                await session.execute(
                    update(TagJob)
                    .where(TagJob.status == "running")
                    .values(status="queued", lease=None)
                )
                active = await self._meta(session, "active_generation")
                await self._remove_other_generations(session, active or "")
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = self.sessions = None

    @asynccontextmanager
    async def transaction(self):
        if self.sessions is None:
            raise RuntimeError("数据库尚未打开")
        async with self.lock, self.sessions.begin() as session:
            yield session

    @staticmethod
    async def _meta(session, key):
        row = await session.get(Meta, key)
        return row.value if row else None

    @staticmethod
    async def _set_meta(session, key, value):
        await session.merge(Meta(key=key, value=value))

    async def get_meta(self, key, default=None):
        async with self.transaction() as session:
            value = await self._meta(session, key)
            return default if value is None else value

    async def set_meta(self, key, value):
        async with self.transaction() as session:
            await self._set_meta(session, key, value)

    async def record_sent_meme(self, session, conversation, receipt_id, details):
        async with self.transaction() as db_session:
            await db_session.execute(
                sqlite_insert(SentMeme)
                .values(
                    receipt_id=receipt_id,
                    session=session,
                    conversation=conversation,
                    sent_at=time.time(),
                    image_id=details["image_id"],
                    details=details,
                )
                .on_conflict_do_nothing(index_elements=[SentMeme.receipt_id])
            )
            scope = and_(SentMeme.session == session, SentMeme.conversation == conversation)
            keep = select(SentMeme.id).where(scope).order_by(SentMeme.id.desc()).limit(20)
            await db_session.execute(delete(SentMeme).where(scope, SentMeme.id.not_in(keep)))
            keep = select(SentMeme.id).order_by(SentMeme.id.desc()).limit(10000)
            await db_session.execute(delete(SentMeme).where(SentMeme.id.not_in(keep)))

    async def recent_sent_memes(self, session, conversation, limit=5):
        async with self.transaction() as db_session:
            rows = (
                await db_session.scalars(
                    select(SentMeme)
                    .where(SentMeme.session == session, SentMeme.conversation == conversation)
                    .order_by(SentMeme.id.desc())
                    .limit(limit)
                )
            ).all()
            return [{"sent_at": row.sent_at, **row.details} for row in reversed(rows)]

    async def clear_sent_memes(self, session, conversation):
        async with self.transaction() as db_session:
            await db_session.execute(
                delete(SentMeme).where(
                    SentMeme.session == session, SentMeme.conversation == conversation
                )
            )

    async def new_generation(self) -> str:
        generation = uuid.uuid4().hex
        async with self.transaction() as session:
            session.add(Generation(id=generation))
        return generation

    @staticmethod
    def _image_query():
        return (
            select(CatalogImage, Override, Annotation, TagJob)
            .outerjoin(Override, Override.image_id == CatalogImage.id)
            .outerjoin(Annotation, Annotation.image_id == CatalogImage.id)
            .outerjoin(TagJob, TagJob.image_id == CatalogImage.id)
        )

    @staticmethod
    def _missing_annotation():
        return or_(
            Annotation.image_id.is_(None),
            Annotation.image_key != CatalogImage.image_key,
            Annotation.source_text != CatalogImage.text,
            Annotation.prompt_version != PROMPT_VERSION,
        )

    async def _index(self, session, generation, ident):
        await session.execute(
            delete(SearchEntry)
            .where(SearchEntry.generation == generation, SearchEntry.image_id == str(ident))
            .execution_options(synchronize_session=False)
        )
        row = (
            await session.execute(
                self._image_query().where(
                    CatalogImage.generation == generation, CatalogImage.id == ident
                )
            )
        ).first()
        if row:
            item = image_view(*row)
            await session.execute(
                insert(SearchEntry),
                [
                    {
                        "generation": generation,
                        "image_id": str(ident),
                        "text": tokenize(item["text"]),
                        "manual_tags": tokenize(
                            " ".join(
                                tag
                                for tag in item["manual_tags"]
                                if tag not in item["excluded_tags"]
                            )
                        ),
                        "tags": tokenize(" ".join(item["effective_tags"])),
                        "description": tokenize(item["description"]),
                    }
                ],
            )

    async def _put(self, session, generation, raw, ident=None):
        item = normalize_image(raw) if raw is not None else None
        ident = item["id"] if item else ident
        if item is None:
            if ident is not None:
                await session.execute(
                    delete(CatalogImage).where(
                        CatalogImage.generation == generation, CatalogImage.id == ident
                    )
                )
                await self._index(session, generation, ident)
            return
        await session.merge(CatalogImage(generation=generation, **item))
        await self._index(session, generation, ident)

    async def stage_batch(self, generation, records):
        async with self.transaction() as session:
            for record in records:
                await self._put(session, generation, record)

    @staticmethod
    async def _acknowledge(session, receipts):
        if receipts:
            await session.execute(
                update(Notification)
                .where(Notification.id.in_(receipts))
                .values(processed=True, error=None)
            )

    @staticmethod
    async def _remove_other_generations(session, generation):
        await session.execute(delete(CatalogImage).where(CatalogImage.generation != generation))
        await session.execute(
            delete(SearchEntry)
            .where(SearchEntry.generation != generation)
            .execution_options(synchronize_session=False)
        )
        await session.execute(delete(Generation).where(Generation.id != generation))

    async def publish(self, generation, summary, receipts):
        async with self.transaction() as session:
            await self._set_meta(session, "active_generation", generation)
            await self._set_meta(session, "last_sync", {**summary, "completed_at": time.time()})
            await session.execute(
                update(Generation)
                .where(Generation.id == generation)
                .values(export_id=summary["export_id"])
            )
            await self._acknowledge(session, receipts)
        self.catalog_revision += 1
        # A failure during cleanup still leaves a committed, valid active generation.
        async with self.transaction() as session:
            await self._remove_other_generations(session, generation)

    async def discard(self, generation):
        async with self.transaction() as session:
            if await self._meta(session, "active_generation") == generation:
                return
            await session.execute(delete(CatalogImage).where(CatalogImage.generation == generation))
            await session.execute(
                delete(SearchEntry)
                .where(SearchEntry.generation == generation)
                .execution_options(synchronize_session=False)
            )
            await session.execute(delete(Generation).where(Generation.id == generation))

    async def refresh(self, generation, ident, raw, receipts=()):
        async with self.transaction() as session:
            await self._put(session, generation, raw, ident)
            await self._acknowledge(session, receipts)
        self.catalog_revision += 1

    async def get_image(self, ident):
        async with self.transaction() as session:
            generation = await self._meta(session, "active_generation")
            row = (
                await session.execute(
                    self._image_query().where(
                        CatalogImage.generation == generation, CatalogImage.id == ident
                    )
                )
            ).first()
            return image_view(*row) if row else None

    async def update_override(self, ident, values):
        async with self.transaction() as session:
            override = await session.get(Override, ident)
            if override is None:
                override = Override(image_id=ident)
                session.add(override)
            for key in ("manual_tags", "excluded_tags", "description", "enabled"):
                if key in values:
                    setattr(override, key, values[key])
            generations = (
                await session.scalars(
                    select(CatalogImage.generation).where(CatalogImage.id == ident)
                )
            ).all()
            for generation in generations:
                await self._index(session, generation, ident)
        self.catalog_revision += 1

    async def list_images(
        self,
        *,
        query="",
        page=1,
        limit=30,
        rating="",
        annotation="",
        enabled="",
        recent=(),
        ratings=None,
    ):
        terms = tokenize(query[:1000]).split()
        if query.strip() and not terms or ratings is not None and not ratings:
            return {"items": [], "total": 0, "page": page}
        async with self.transaction() as session:
            generation = await self._meta(session, "active_generation")
            statement = self._image_query().where(CatalogImage.generation == generation)
            rank = None
            if terms:
                fts = SearchEntry.__table__.table_valued()
                statement = statement.join(
                    SearchEntry,
                    and_(
                        SearchEntry.generation == CatalogImage.generation,
                        SearchEntry.image_id == cast(CatalogImage.id, Text),
                    ),
                ).where(
                    type_coerce(fts, Text).match(
                        " OR ".join('"' + term.replace('"', '""') + '"' for term in terms[:80])
                    )
                )
                rank = func.bm25(fts, 0, 0, 6, 8, 4, 2)
            if rating:
                statement = statement.where(CatalogImage.rating == rating)
            if ratings is not None:
                statement = statement.where(CatalogImage.rating.in_(ratings))
            if annotation == "missing":
                statement = statement.where(self._missing_annotation())
            if enabled in ("true", "false"):
                statement = statement.where(
                    func.coalesce(Override.enabled, True) == (enabled == "true")
                )
            total = await session.scalar(
                statement.with_only_columns(func.count(), maintain_column_froms=True)
            )
            recent_ids = [int(ident) for ident in recent][-20:]
            order = []
            if recent_ids:
                order.append(case((CatalogImage.id.in_(recent_ids), 1), else_=0))
            if rank is not None:
                order.append(rank)
            order.append(CatalogImage.id.desc())
            rows = (
                await session.execute(
                    statement.order_by(*order).limit(limit).offset((page - 1) * limit)
                )
            ).all()
            return {"items": [image_view(*row) for row in rows], "total": total, "page": page}

    async def search(self, query, limit, ratings=RATINGS, recent=()):
        result = await self.list_images(
            query=query, limit=limit, enabled="true", recent=recent, ratings=ratings
        )
        return result["items"]

    async def semantic_catalog(self, ratings=RATINGS, ids=None, *, include_disabled=False):
        if not ratings or ids == []:
            return []
        async with self.transaction() as session:
            generation = await self._meta(session, "active_generation")
            statement = (
                select(CatalogImage, Override, Annotation)
                .outerjoin(Override, Override.image_id == CatalogImage.id)
                .outerjoin(Annotation, Annotation.image_id == CatalogImage.id)
                .where(CatalogImage.generation == generation, CatalogImage.rating.in_(ratings))
                .order_by(CatalogImage.id)
            )
            if not include_disabled:
                statement = statement.where(func.coalesce(Override.enabled, True).is_(True))
            if ids is not None:
                statement = statement.where(CatalogImage.id.in_(int(ident) for ident in ids))
            result = []
            for image, override, annotation in (await session.execute(statement)).all():
                result.append(
                    {
                        "id": image.id,
                        "text": image.text,
                        "rating": image.rating,
                        "image_key": image.image_key,
                        "hub_tags": image.hub_tags,
                        "enabled": override.enabled if override else True,
                        "manual_tags": override.manual_tags if override else [],
                        "excluded_tags": override.excluded_tags if override else [],
                        "manual_description": override.description if override else None,
                        "ai_image_key": annotation.image_key if annotation else None,
                        "ai_source_text": annotation.source_text if annotation else None,
                        "prompt_version": annotation.prompt_version if annotation else None,
                        "ai_description": annotation.description if annotation else None,
                        "ai_tags": annotation.tags if annotation else [],
                    }
                )
            return result

    async def query_sql(self, sql, limit=10, ratings=RATINGS):
        validate_query(sql, limit)
        async with self.query_lock:
            stop = Event()
            try:
                return await asyncio.to_thread(
                    query_catalog, self.path, sql, limit, tuple(ratings), stop
                )
            finally:
                stop.set()

    async def missing_annotation_ids(self):
        async with self.transaction() as session:
            generation = await self._meta(session, "active_generation")
            return list(
                await session.scalars(
                    select(CatalogImage.id)
                    .outerjoin(Annotation, Annotation.image_id == CatalogImage.id)
                    .where(CatalogImage.generation == generation, self._missing_annotation())
                    .order_by(CatalogImage.id)
                )
            )

    async def record_webhook(self, *, body, valid, status, event=None, ident=None, error=None):
        async with self.transaction() as session:
            receipt = None
            now = time.time()
            if status == 204:
                notification = Notification(image_id=ident, event=event, received=now)
                session.add(notification)
                await session.flush()
                receipt = notification.id
            session.add(
                WebhookLog(
                    received=now,
                    receipt_id=receipt,
                    event=event,
                    image_id=str(ident) if ident else None,
                    signature_valid=valid,
                    http_status=status,
                    body=body[:8192],
                    error=error,
                )
            )
            return receipt

    async def pending(self, *, all_pending=False):
        async with self.transaction() as session:
            statement = (
                select(Notification)
                .where(Notification.processed.is_(False))
                .order_by(Notification.id)
            )
            if not all_pending:
                statement = statement.where(Notification.next_attempt <= time.time())
            rows = (await session.scalars(statement)).all()
            if not all_pending and rows:
                rows = [row for row in rows if row.image_id == rows[0].image_id]
            return [entity_dict(row) for row in rows]

    async def fail_receipts(self, receipts, error):
        async with self.transaction() as session:
            for receipt in receipts:
                await session.execute(
                    update(Notification)
                    .where(Notification.id == receipt["id"])
                    .values(
                        attempts=Notification.attempts + 1,
                        error=error,
                        next_attempt=time.time() + min(300, 2 ** min(receipt["attempts"] + 1, 8)),
                    )
                )

    async def retry_notifications(self):
        async with self.transaction() as session:
            await session.execute(
                update(Notification).where(Notification.processed.is_(False)).values(next_attempt=0)
            )

    async def logs(self, page=1, limit=30, event="", failed=False):
        async with self.transaction() as session:
            statement = select(
                WebhookLog,
                Notification.processed,
                Notification.attempts,
                Notification.error.label("processing_error"),
            ).outerjoin(Notification, Notification.id == WebhookLog.receipt_id)
            if event:
                statement = statement.where(WebhookLog.event == event)
            if failed:
                statement = statement.where(
                    or_(WebhookLog.http_status != 204, Notification.error.is_not(None))
                )
            total = await session.scalar(
                statement.with_only_columns(func.count(), maintain_column_froms=True)
            )
            rows = (
                await session.execute(
                    statement.order_by(WebhookLog.id.desc()).limit(limit).offset((page - 1) * limit)
                )
            ).all()
            return {
                "items": [
                    {
                        **entity_dict(row[0]),
                        "processed": row.processed,
                        "attempts": row.attempts,
                        "processing_error": row.processing_error,
                    }
                    for row in rows
                ],
                "total": total,
                "page": page,
            }

    async def prune_logs(self, days, limit):
        async with self.transaction() as session:
            keep = select(WebhookLog.id).order_by(WebhookLog.id.desc()).limit(limit)
            await session.execute(
                delete(WebhookLog).where(
                    or_(
                        WebhookLog.received < time.time() - days * 86400, WebhookLog.id.not_in(keep)
                    )
                )
            )
            retained = select(WebhookLog.receipt_id).where(WebhookLog.receipt_id.is_not(None))
            await session.execute(
                delete(Notification).where(
                    Notification.processed.is_(True), Notification.id.not_in(retained)
                )
            )

    async def enqueue_tags(self, ids, *, force=False, provider_id=""):
        queued = 0
        async with self.transaction() as session:
            generation = await self._meta(session, "active_generation")
            for ident in ids:
                row = (
                    await session.execute(
                        self._image_query().where(
                            CatalogImage.generation == generation, CatalogImage.id == ident
                        )
                    )
                ).first()
                if not row:
                    continue
                view = image_view(*row)
                if (
                    not force
                    and view["annotation_fresh"]
                    and view["annotation_provider"] == provider_id
                ):
                    continue
                job = row[3]
                if job and (
                    job.status in ("queued", "running") or job.status == "failed" and not force
                ):
                    continue
                await session.merge(
                    TagJob(
                        image_id=ident,
                        status="queued",
                        updated=time.time(),
                        attempts=0,
                        next_attempt=0,
                        error=None,
                        lease=None,
                    )
                )
                queued += 1
        return queued

    async def claim_tag(self):
        async with self.transaction() as session:
            if await self._meta(session, "tagging_paused"):
                return None
            job = await session.scalar(
                select(TagJob)
                .where(TagJob.status == "queued", TagJob.next_attempt <= time.time())
                .order_by(TagJob.updated, TagJob.image_id)
                .limit(1)
            )
            if job is None:
                return None
            result = entity_dict(job)
            job.status, job.lease = "running", uuid.uuid4().hex
            return {**result, "lease": job.lease}

    async def finish_tag(self, job, image, result, provider_id):
        async with self.transaction() as session:
            current = await session.scalar(
                select(TagJob).where(
                    TagJob.image_id == job["image_id"], TagJob.lease == job["lease"]
                )
            )
            if current is None:
                return
            generation = await self._meta(session, "active_generation")
            row = await session.get(CatalogImage, (generation, job["image_id"]))
            if row is None:
                await session.delete(current)
            elif not image or row.text != image["text"] or row.image_key != image["image_key"]:
                current.status, current.lease = "queued", None
            else:
                await session.merge(
                    Annotation(
                        image_id=job["image_id"],
                        image_key=image["image_key"],
                        source_text=image["text"],
                        provider_id=provider_id,
                        prompt_version=PROMPT_VERSION,
                        description=result["description"],
                        tags=result["tags"],
                        updated=time.time(),
                    )
                )
                current.status, current.error, current.lease, current.updated = (
                    "done",
                    None,
                    None,
                    time.time(),
                )
                generations = (
                    await session.scalars(
                        select(CatalogImage.generation).where(CatalogImage.id == job["image_id"])
                    )
                ).all()
                for current_generation in generations:
                    await self._index(session, current_generation, job["image_id"])
        self.catalog_revision += 1

    async def fail_tag(self, job, error):
        async with self.transaction() as session:
            attempts = job["attempts"] + 1
            await session.execute(
                update(TagJob)
                .where(TagJob.image_id == job["image_id"], TagJob.lease == job["lease"])
                .values(
                    status="failed" if attempts >= 3 else "queued",
                    attempts=attempts,
                    error=error,
                    lease=None,
                    next_attempt=time.time() + min(300, 10 * 2**attempts),
                    updated=time.time(),
                )
            )

    async def stats(self):
        async with self.transaction() as session:
            generation = await self._meta(session, "active_generation")
            counts = dict(
                (
                    await session.execute(
                        select(CatalogImage.rating, func.count())
                        .where(CatalogImage.generation == generation)
                        .group_by(CatalogImage.rating)
                    )
                ).all()
            )
            pending = await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.processed.is_(False))
            )
            jobs = dict(
                (
                    await session.execute(
                        select(TagJob.status, func.count()).group_by(TagJob.status)
                    )
                ).all()
            )
            queue = (
                (
                    await session.execute(
                        select(
                            TagJob.image_id,
                            CatalogImage.text,
                            TagJob.status,
                            TagJob.attempts,
                            TagJob.next_attempt,
                        )
                        .join(
                            CatalogImage,
                            and_(
                                CatalogImage.id == TagJob.image_id,
                                CatalogImage.generation == generation,
                            ),
                        )
                        .where(TagJob.status.in_(["running", "queued"]))
                        .order_by(
                            case((TagJob.status == "running", 0), else_=1),
                            case((TagJob.next_attempt > time.time(), 1), else_=0),
                            TagJob.updated,
                            TagJob.image_id,
                        )
                        .limit(5)
                    )
                )
                .mappings()
                .all()
            )
            last_hook = await session.scalar(
                select(func.max(WebhookLog.received)).where(WebhookLog.http_status == 204)
            )
            return {
                "generation": generation,
                "total": sum(counts.values()),
                "ratings": counts,
                "pending_notifications": pending,
                "tag_jobs": jobs,
                "tag_queue": [{**row, "image_id": str(row["image_id"])} for row in queue],
                "tagging_auto": bool(await self._meta(session, "tagging_auto")),
                "tagging_paused": bool(await self._meta(session, "tagging_paused")),
                "last_sync": await self._meta(session, "last_sync"),
                "last_webhook": last_hook,
            }
