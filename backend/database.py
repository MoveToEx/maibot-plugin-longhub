from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

import aiosqlite

from .models import PROMPT_VERSION, RATINGS, normalize_image, tokenize
from .sql_query import query_catalog, validate_query


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    """One serialized connection; no transaction spans external I/O."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.query_lock = asyncio.Lock()
        self.catalog_revision = 0
        self.conn: aiosqlite.Connection | None = None

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS generations(
                id TEXT PRIMARY KEY, created REAL NOT NULL, export_id TEXT);
            CREATE TABLE IF NOT EXISTS images(
                generation TEXT NOT NULL, id INTEGER NOT NULL, text TEXT NOT NULL,
                rating TEXT NOT NULL, image_url TEXT NOT NULL, image_key TEXT NOT NULL,
                hub_tags TEXT NOT NULL, raw TEXT NOT NULL, PRIMARY KEY(generation,id));
            CREATE TABLE IF NOT EXISTS overrides(
                image_id INTEGER PRIMARY KEY, manual_tags TEXT NOT NULL DEFAULT '[]',
                excluded_tags TEXT NOT NULL DEFAULT '[]', description TEXT,
                enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS annotations(
                image_id INTEGER PRIMARY KEY, image_key TEXT NOT NULL, source_text TEXT NOT NULL,
                provider_id TEXT NOT NULL, prompt_version INTEGER NOT NULL,
                description TEXT NOT NULL, tags TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS notifications(
                id INTEGER PRIMARY KEY AUTOINCREMENT, image_id INTEGER NOT NULL,
                event TEXT NOT NULL, received REAL NOT NULL, processed INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                error TEXT);
            CREATE INDEX IF NOT EXISTS notifications_pending
                ON notifications(processed,next_attempt,image_id);
            CREATE TABLE IF NOT EXISTS webhook_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, received REAL NOT NULL,
                receipt_id INTEGER, event TEXT, image_id TEXT, signature_valid INTEGER NOT NULL,
                http_status INTEGER NOT NULL, body TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS tag_jobs(
                image_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                error TEXT, lease TEXT, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sent_memes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL UNIQUE,
                session TEXT NOT NULL, conversation TEXT NOT NULL, sent_at REAL NOT NULL,
                image_id TEXT NOT NULL, details TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS sent_memes_scope
                ON sent_memes(session,conversation,id DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
                generation UNINDEXED, image_id UNINDEXED, text, manual_tags, tags, description,
                tokenize='unicode61');
            PRAGMA user_version=2;
        """)
        await self.conn.execute(
            "UPDATE tag_jobs SET status='queued', lease=NULL WHERE status='running'"
        )
        active = await self._meta("active_generation")
        # An interrupted export is never published or reused.
        await self.conn.execute("DELETE FROM images WHERE generation != ?", (active or "",))
        await self.conn.execute("DELETE FROM search_index WHERE generation != ?", (active or "",))
        await self.conn.execute("DELETE FROM generations WHERE id != ?", (active or "",))
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def _one(self, sql, args=()):
        async with self.conn.execute(sql, args) as cursor:
            return await cursor.fetchone()

    @asynccontextmanager
    async def _transaction(self):
        try:
            yield
            await self.conn.commit()
        except BaseException:
            await self.conn.rollback()
            raise

    async def _all(self, sql, args=()):
        async with self.conn.execute(sql, args) as cursor:
            return await cursor.fetchall()

    async def _meta(self, key):
        row = await self._one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(row[0]) if row else None

    async def _set_meta(self, key, value):
        await self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dump(value)))

    async def get_meta(self, key, default=None):
        async with self.lock:
            value = await self._meta(key)
            return default if value is None else value

    async def set_meta(self, key, value):
        async with self.lock:
            await self._set_meta(key, value)
            await self.conn.commit()

    async def record_sent_meme(self, session, conversation, receipt_id, details):
        async with self.lock, self._transaction():
            await self.conn.execute(
                "INSERT OR IGNORE INTO sent_memes(receipt_id,session,conversation,sent_at,image_id,details) VALUES(?,?,?,?,?,?)",
                (
                    receipt_id,
                    session,
                    conversation,
                    time.time(),
                    details["image_id"],
                    dump(details),
                ),
            )
            # Keep bounded metadata only: 20 receipts per conversation, 10,000 overall.
            await self.conn.execute(
                """DELETE FROM sent_memes WHERE session=? AND conversation=? AND id NOT IN
                (SELECT id FROM sent_memes WHERE session=? AND conversation=? ORDER BY id DESC LIMIT 20)""",
                (session, conversation, session, conversation),
            )
            await self.conn.execute(
                "DELETE FROM sent_memes WHERE id NOT IN (SELECT id FROM sent_memes ORDER BY id DESC LIMIT 10000)"
            )

    async def recent_sent_memes(self, session, conversation, limit=5):
        async with self.lock:
            rows = await self._all(
                "SELECT sent_at,details FROM sent_memes WHERE session=? AND conversation=? ORDER BY id DESC LIMIT ?",
                (session, conversation, limit),
            )
            return [
                {"sent_at": row["sent_at"], **json.loads(row["details"])} for row in reversed(rows)
            ]

    async def clear_sent_memes(self, session, conversation):
        async with self.lock, self._transaction():
            await self.conn.execute(
                "DELETE FROM sent_memes WHERE session=? AND conversation=?", (session, conversation)
            )

    async def new_generation(self) -> str:
        generation = uuid.uuid4().hex
        async with self.lock:
            await self.conn.execute(
                "INSERT INTO generations VALUES(?,?,NULL)", (generation, time.time())
            )
            await self.conn.commit()
        return generation

    async def _view(self, row) -> dict:
        item = dict(row)
        ident = item["id"]
        override = await self._one("SELECT * FROM overrides WHERE image_id=?", (ident,))
        annotation = await self._one("SELECT * FROM annotations WHERE image_id=?", (ident,))
        item["id"] = str(ident)
        item["hub_tags"] = json.loads(item["hub_tags"])
        item["manual_tags"] = json.loads(override["manual_tags"]) if override else []
        item["excluded_tags"] = json.loads(override["excluded_tags"]) if override else []
        item["enabled"] = bool(override["enabled"]) if override else True
        item["manual_description"] = override["description"] if override else None
        item["ai_tags"] = json.loads(annotation["tags"]) if annotation else []
        item["ai_description"] = annotation["description"] if annotation else ""
        item["description"] = (
            item["manual_description"]
            if item["manual_description"] is not None
            else item["ai_description"]
        )
        item["effective_tags"] = [
            x
            for x in dict.fromkeys(item["hub_tags"] + item["ai_tags"] + item["manual_tags"])
            if x not in item["excluded_tags"]
        ]
        item["annotation_fresh"] = bool(
            annotation
            and annotation["image_key"] == item["image_key"]
            and annotation["source_text"] == item["text"]
            and annotation["prompt_version"] == PROMPT_VERSION
        )
        item["annotation_provider"] = annotation["provider_id"] if annotation else None
        job = await self._one(
            "SELECT status,attempts,error FROM tag_jobs WHERE image_id=?", (ident,)
        )
        item["tag_job"] = dict(job) if job else None
        item["raw"] = json.loads(item["raw"])

        # Convert upstream IDs recursively before crossing the JavaScript boundary.
        def safe(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, int) and abs(value) > 2**53 - 1:
                return str(value)
            if isinstance(value, dict):
                return {k: safe(v) for k, v in value.items()}
            if isinstance(value, list):
                return [safe(v) for v in value]
            return value

        item["raw"] = safe(item["raw"])
        return item

    async def _index(self, generation, ident):
        await self.conn.execute(
            "DELETE FROM search_index WHERE generation=? AND image_id=?", (generation, str(ident))
        )
        row = await self._one(
            "SELECT * FROM images WHERE generation=? AND id=?", (generation, ident)
        )
        if row:
            item = await self._view(row)
            await self.conn.execute(
                "INSERT INTO search_index VALUES(?,?,?,?,?,?)",
                (
                    generation,
                    str(ident),
                    tokenize(item["text"]),
                    tokenize(
                        " ".join(x for x in item["manual_tags"] if x not in item["excluded_tags"])
                    ),
                    tokenize(" ".join(item["effective_tags"])),
                    tokenize(item["description"]),
                ),
            )

    async def _put(self, generation, raw, ident=None):
        item = normalize_image(raw) if raw is not None else None
        ident = item["id"] if item else ident
        if item is None:
            if ident is not None:
                await self.conn.execute(
                    "DELETE FROM images WHERE generation=? AND id=?", (generation, ident)
                )
                await self._index(generation, ident)
            return
        await self.conn.execute(
            "INSERT OR REPLACE INTO images VALUES(?,?,?,?,?,?,?,?)",
            (
                generation,
                item["id"],
                item["text"],
                item["rating"],
                item["image_url"],
                item["image_key"],
                dump(item["hub_tags"]),
                dump(raw),
            ),
        )
        await self._index(generation, item["id"])

    async def stage_batch(self, generation, records):
        async with self.lock:
            try:
                for record in records:
                    await self._put(generation, record)
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def publish(self, generation, summary, receipts):
        async with self.lock:
            try:
                await self._set_meta("active_generation", generation)
                await self._set_meta("last_sync", {**summary, "completed_at": time.time()})
                await self.conn.execute(
                    "UPDATE generations SET export_id=? WHERE id=?",
                    (summary["export_id"], generation),
                )
                await self.conn.executemany(
                    "UPDATE notifications SET processed=1,error=NULL WHERE id=?",
                    [(x,) for x in receipts],
                )
                await self.conn.commit()
                self.catalog_revision += 1
            except BaseException:
                await self.conn.rollback()
                raise
            # Separate cleanup: a crash here still leaves a valid active generation.
            await self.conn.execute("DELETE FROM images WHERE generation != ?", (generation,))
            await self.conn.execute("DELETE FROM search_index WHERE generation != ?", (generation,))
            await self.conn.execute("DELETE FROM generations WHERE id != ?", (generation,))
            await self.conn.commit()

    async def discard(self, generation):
        async with self.lock:
            if await self._meta("active_generation") == generation:
                return
            for table in ("images", "search_index"):
                await self.conn.execute(f"DELETE FROM {table} WHERE generation=?", (generation,))
            await self.conn.execute("DELETE FROM generations WHERE id=?", (generation,))
            await self.conn.commit()

    async def refresh(self, generation, ident, raw, receipts=()):
        async with self.lock:
            try:
                await self._put(generation, raw, ident)
                await self.conn.executemany(
                    "UPDATE notifications SET processed=1,error=NULL WHERE id=?",
                    [(x,) for x in receipts],
                )
                await self.conn.commit()
                self.catalog_revision += 1
            except BaseException:
                await self.conn.rollback()
                raise

    async def get_image(self, ident):
        async with self.lock:
            generation = await self._meta("active_generation")
            row = await self._one(
                "SELECT * FROM images WHERE generation=? AND id=?", (generation, ident)
            )
            return await self._view(row) if row else None

    async def update_override(self, ident, values):
        async with self.lock:
            try:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO overrides(image_id) VALUES(?)", (ident,)
                )
                for key in ("manual_tags", "excluded_tags", "description", "enabled"):
                    if key in values:
                        value = values[key]
                        if key.endswith("tags"):
                            value = dump(value)
                        await self.conn.execute(
                            f"UPDATE overrides SET {key}=? WHERE image_id=?", (value, ident)
                        )
                for row in await self._all("SELECT generation FROM images WHERE id=?", (ident,)):
                    await self._index(row[0], ident)
                await self.conn.commit()
                self.catalog_revision += 1
            except BaseException:
                await self.conn.rollback()
                raise

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
        async with self.lock:
            generation = await self._meta("active_generation")
            terms = tokenize(query[:1000]).split()
            args = []
            joins = "LEFT JOIN overrides o ON o.image_id=i.id LEFT JOIN annotations a ON a.image_id=i.id"
            rank = "0.0"
            if query.strip() and not terms:
                return {"items": [], "total": 0, "page": page}
            if terms:
                joins += " JOIN search_index ON search_index.generation=i.generation AND search_index.image_id=CAST(i.id AS TEXT)"
                rank = "bm25(search_index,0,0,6,8,4,2)"
            where = ["i.generation=?"]
            args.append(generation)
            if terms:
                where.append("search_index MATCH ?")
                args.append(" OR ".join('"' + t.replace('"', '""') + '"' for t in terms[:80]))
            if rating:
                where.append("i.rating=?")
                args.append(rating)
            if ratings is not None:
                if not ratings:
                    return {"items": [], "total": 0, "page": page}
                where.append("i.rating IN (" + ",".join("?" for _ in ratings) + ")")
                args.extend(ratings)
            if annotation == "missing":
                where.append(
                    "(a.image_id IS NULL OR a.image_key!=i.image_key OR a.source_text!=i.text OR a.prompt_version!=?)"
                )
                args.append(PROMPT_VERSION)
            if enabled in ("true", "false"):
                where.append("COALESCE(o.enabled,1)=?")
                args.append(int(enabled == "true"))
            clause = " AND ".join(where)
            total = (
                await self._one(f"SELECT count(*) FROM images i {joins} WHERE {clause}", args)
            )[0]
            # Penalize recent sends without excluding them from an explicit search.
            recent_ids = [int(x) for x in recent][-20:]
            penalty = "0.0"
            if recent_ids:
                penalty = (
                    "CASE WHEN i.id IN ("
                    + ",".join("?" for _ in recent_ids)
                    + ") THEN 1 ELSE 0 END"
                )
            rows = await self._all(
                f"SELECT i.* FROM images i {joins} WHERE {clause} ORDER BY {penalty},{rank},i.id DESC LIMIT ? OFFSET ?",
                [*args, *recent_ids, limit, (page - 1) * limit],
            )
            return {"items": [await self._view(row) for row in rows], "total": total, "page": page}

    async def search(self, query, limit, ratings=RATINGS, recent=()):
        # Filter in SQL before LIMIT, so excluded ratings cannot hide valid candidates.
        result = await self.list_images(
            query=query, limit=limit, enabled="true", recent=recent, ratings=ratings
        )
        return result["items"]

    async def semantic_catalog(self, ratings=RATINGS, ids=None, *, include_disabled=False):
        """One consistent bulk read; no per-image annotation/override queries."""
        if not ratings or ids == []:
            return []
        async with self.lock:
            generation = await self._meta("active_generation")
            where = "i.generation=? AND i.rating IN (" + ",".join("?" for _ in ratings) + ")"
            args = [generation, *ratings]
            if not include_disabled:
                where += " AND COALESCE(o.enabled,1)=1"
            if ids is not None:
                where += " AND i.id IN (" + ",".join("?" for _ in ids) + ")"
                args.extend(int(ident) for ident in ids)
            rows = await self._all(
                f"""SELECT i.id,i.text,i.rating,i.image_key,i.hub_tags,
                COALESCE(o.enabled,1) AS enabled,
                COALESCE(o.manual_tags,'[]') AS manual_tags,
                COALESCE(o.excluded_tags,'[]') AS excluded_tags,o.description AS manual_description,
                a.image_key AS ai_image_key,a.source_text AS ai_source_text,
                a.prompt_version,a.description AS ai_description,a.tags AS ai_tags
                FROM images i LEFT JOIN overrides o ON o.image_id=i.id
                LEFT JOIN annotations a ON a.image_id=i.id WHERE {where} ORDER BY i.id""",
                args,
            )
            return [dict(row) for row in rows]

    async def query_sql(self, sql, limit=10, ratings=RATINGS):
        validate_query(sql, limit)
        # An independent read snapshot keeps model queries off the live writer and
        # event loop. Serialize copies to bound memory when tools run in parallel.
        async with self.query_lock:
            stop = Event()
            try:
                return await asyncio.to_thread(
                    query_catalog, self.path, sql, limit, tuple(ratings), stop
                )
            finally:
                stop.set()

    async def missing_annotation_ids(self):
        async with self.lock:
            generation = await self._meta("active_generation")
            rows = await self._all(
                """SELECT i.id FROM images i LEFT JOIN annotations a ON a.image_id=i.id
                WHERE i.generation=? AND (a.image_id IS NULL OR a.image_key!=i.image_key
                OR a.source_text!=i.text OR a.prompt_version!=?) ORDER BY i.id""",
                (generation, PROMPT_VERSION),
            )
            return [row[0] for row in rows]

    async def record_webhook(self, *, body, valid, status, event=None, ident=None, error=None):
        async with self.lock:
            try:
                receipt = None
                now = time.time()
                if status == 204:
                    cursor = await self.conn.execute(
                        "INSERT INTO notifications(image_id,event,received) VALUES(?,?,?)",
                        (ident, event, now),
                    )
                    receipt = cursor.lastrowid
                    await cursor.close()
                await self.conn.execute(
                    "INSERT INTO webhook_logs(received,receipt_id,event,image_id,signature_valid,http_status,body,error) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        now,
                        receipt,
                        event,
                        str(ident) if ident else None,
                        int(valid),
                        status,
                        body[:8192],
                        error,
                    ),
                )
                await self.conn.commit()
                return receipt
            except BaseException:
                await self.conn.rollback()
                raise

    async def pending(self, *, all_pending=False):
        async with self.lock:
            due = "" if all_pending else " AND next_attempt<=?"
            args = () if all_pending else (time.time(),)
            rows = await self._all(
                f"SELECT * FROM notifications WHERE processed=0{due} ORDER BY id", args
            )
            if not all_pending and rows:
                rows = [r for r in rows if r["image_id"] == rows[0]["image_id"]]
            return [dict(r) for r in rows]

    async def fail_receipts(self, receipts, error):
        async with self.lock:
            await self.conn.executemany(
                "UPDATE notifications SET attempts=attempts+1,error=?,next_attempt=? WHERE id=?",
                [
                    (error, time.time() + min(300, 2 ** min(r["attempts"] + 1, 8)), r["id"])
                    for r in receipts
                ],
            )
            await self.conn.commit()

    async def retry_notifications(self):
        async with self.lock:
            await self.conn.execute("UPDATE notifications SET next_attempt=0 WHERE processed=0")
            await self.conn.commit()

    async def logs(self, page=1, limit=30, event="", failed=False):
        async with self.lock:
            where, args = [], []
            if event:
                where.append("l.event=?")
                args.append(event)
            if failed:
                where.append("(l.http_status!=204 OR n.error IS NOT NULL)")
            clause = " WHERE " + " AND ".join(where) if where else ""
            source = "FROM webhook_logs l LEFT JOIN notifications n ON n.id=l.receipt_id"
            total = (await self._one(f"SELECT count(*) {source}{clause}", args))[0]
            rows = await self._all(
                f"SELECT l.*,n.processed,n.attempts,n.error AS processing_error {source}{clause} ORDER BY l.id DESC LIMIT ? OFFSET ?",
                [*args, limit, (page - 1) * limit],
            )
            return {"items": [dict(r) for r in rows], "total": total, "page": page}

    async def prune_logs(self, days, limit):
        async with self.lock:
            await self.conn.execute(
                "DELETE FROM webhook_logs WHERE received<? OR id NOT IN (SELECT id FROM webhook_logs ORDER BY id DESC LIMIT ?)",
                (time.time() - days * 86400, limit),
            )
            # Unprocessed notifications are durable work, independent of log retention.
            await self.conn.execute(
                "DELETE FROM notifications WHERE processed=1 AND id NOT IN (SELECT receipt_id FROM webhook_logs WHERE receipt_id IS NOT NULL)"
            )
            await self.conn.commit()

    async def enqueue_tags(self, ids, *, force=False, provider_id=""):
        queued = 0
        async with self.lock:
            generation = await self._meta("active_generation")
            for ident in ids:
                row = await self._one(
                    "SELECT * FROM images WHERE generation=? AND id=?", (generation, ident)
                )
                if not row:
                    continue
                view = await self._view(row)
                if (
                    not force
                    and view["annotation_fresh"]
                    and view["annotation_provider"] == provider_id
                ):
                    continue
                job = await self._one("SELECT status FROM tag_jobs WHERE image_id=?", (ident,))
                if job and (job[0] in ("queued", "running") or (job[0] == "failed" and not force)):
                    continue
                await self.conn.execute(
                    "INSERT OR REPLACE INTO tag_jobs(image_id,status,updated) VALUES(?,'queued',?)",
                    (ident, time.time()),
                )
                queued += 1
            await self.conn.commit()
        return queued

    async def claim_tag(self):
        async with self.lock:
            if await self._meta("tagging_paused"):
                return None
            row = await self._one(
                "SELECT * FROM tag_jobs WHERE status='queued' AND next_attempt<=? ORDER BY updated,image_id LIMIT 1",
                (time.time(),),
            )
            if not row:
                return None
            lease = uuid.uuid4().hex
            await self.conn.execute(
                "UPDATE tag_jobs SET status='running',lease=? WHERE image_id=?",
                (lease, row["image_id"]),
            )
            await self.conn.commit()
            return {**dict(row), "lease": lease}

    async def finish_tag(self, job, image, result, provider_id):
        async with self.lock, self._transaction():
            current = await self._one(
                "SELECT * FROM tag_jobs WHERE image_id=? AND lease=?",
                (job["image_id"], job["lease"]),
            )
            if not current:
                return
            generation = await self._meta("active_generation")
            row = await self._one(
                "SELECT * FROM images WHERE generation=? AND id=?", (generation, job["image_id"])
            )
            if not row:
                await self.conn.execute("DELETE FROM tag_jobs WHERE image_id=?", (job["image_id"],))
            elif (
                not image or row["text"] != image["text"] or row["image_key"] != image["image_key"]
            ):
                await self.conn.execute(
                    "UPDATE tag_jobs SET status='queued',lease=NULL WHERE image_id=?",
                    (job["image_id"],),
                )
            else:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO annotations VALUES(?,?,?,?,?,?,?,?)",
                    (
                        job["image_id"],
                        image["image_key"],
                        image["text"],
                        provider_id,
                        PROMPT_VERSION,
                        result["description"],
                        dump(result["tags"]),
                        time.time(),
                    ),
                )
                await self.conn.execute(
                    "UPDATE tag_jobs SET status='done',error=NULL,lease=NULL,updated=? WHERE image_id=?",
                    (time.time(), job["image_id"]),
                )
                for gen in await self._all(
                    "SELECT generation FROM images WHERE id=?", (job["image_id"],)
                ):
                    await self._index(gen[0], job["image_id"])
        self.catalog_revision += 1

    async def fail_tag(self, job, error):
        async with self.lock:
            attempts = job["attempts"] + 1
            await self.conn.execute(
                "UPDATE tag_jobs SET status=?,attempts=?,error=?,lease=NULL,next_attempt=?,updated=? WHERE image_id=? AND lease=?",
                (
                    "failed" if attempts >= 3 else "queued",
                    attempts,
                    error,
                    time.time() + min(300, 10 * 2**attempts),
                    time.time(),
                    job["image_id"],
                    job["lease"],
                ),
            )
            await self.conn.commit()

    async def stats(self):
        async with self.lock:
            generation = await self._meta("active_generation")
            counts = dict(
                await self._all(
                    "SELECT rating,count(*) FROM images WHERE generation=? GROUP BY rating",
                    (generation,),
                )
            )
            pending = (await self._one("SELECT count(*) FROM notifications WHERE processed=0"))[0]
            jobs = dict(await self._all("SELECT status,count(*) FROM tag_jobs GROUP BY status"))
            queue = await self._all(
                """SELECT j.image_id,i.text,j.status,j.attempts,j.next_attempt
                FROM tag_jobs j JOIN images i ON i.id=j.image_id AND i.generation=?
                WHERE j.status IN ('running','queued')
                ORDER BY CASE j.status WHEN 'running' THEN 0 ELSE 1 END,
                    CASE WHEN j.next_attempt>? THEN 1 ELSE 0 END,j.updated,j.image_id LIMIT 5""",
                (generation, time.time()),
            )
            last_hook = await self._one(
                "SELECT max(received) FROM webhook_logs WHERE http_status=204"
            )
            return {
                "generation": generation,
                "total": sum(counts.values()),
                "ratings": counts,
                "pending_notifications": pending,
                "tag_jobs": jobs,
                "tag_queue": [{**dict(row), "image_id": str(row["image_id"])} for row in queue],
                "tagging_auto": bool(await self._meta("tagging_auto")),
                "tagging_paused": bool(await self._meta("tagging_paused")),
                "last_sync": await self._meta("last_sync"),
                "last_webhook": last_hook[0],
            }
