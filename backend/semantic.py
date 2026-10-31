from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import shutil
import uuid
from pathlib import Path

from .embeddings import Embeddings
from .errors import SemanticError, error_details, safe_error
from .models import PROMPT_VERSION
from .vector_store import SQLiteVectorStore

logger = logging.getLogger(__name__)
DOCUMENT_VERSION = 1


def document(row):
    """Use current visual/manual annotation; upstream text alone is not coverage."""
    fresh = (
        row["ai_image_key"] == row["image_key"]
        and row["ai_source_text"] == row["text"]
        and row["prompt_version"] == PROMPT_VERSION
    )
    excluded = set(row["excluded_tags"])
    manual = [tag for tag in row["manual_tags"] if tag not in excluded]
    visual = [tag for tag in row["ai_tags"] if tag not in excluded] if fresh else []
    description = row["manual_description"]
    if description is None:
        description = row["ai_description"] if fresh else ""
    description = description or ""
    if not (description.strip() or manual or visual):
        return None
    tags = [tag for tag in dict.fromkeys(row["hub_tags"] + visual + manual) if tag not in excluded]
    content = (
        f"画面与使用场景：{description[:1000]}\n图片中的文字：{row['text'][:300]}\n标签："
        + "、".join(tag[:40] for tag in tags[:24])
    )[:2000]
    fingerprint = hashlib.sha256(
        json.dumps(
            [DOCUMENT_VERSION, row["image_key"], content],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return {
        "id": str(row["id"]),
        "fingerprint": fingerprint,
        "content": content,
        "text": row["text"][:150],
        "rating": row["rating"],
        "description": description[:250],
        "tags": tags[:24],
    }


def provider_profile(provider, ident):
    values = ["maibot.llm.embed", ident, provider.model, provider.dimension, DOCUMENT_VERSION]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class SemanticIndex:
    def __init__(
        self, db, root: Path, settings, provider_lookup=None, store_factory=SQLiteVectorStore
    ):
        self.db, self.root, self.settings = db, (root / "semantic").resolve(), settings
        self.provider_lookup, self.store_factory = provider_lookup, store_factory
        self.store = self.embeddings = self.provider = self.profile = self.directory = None
        self.entries = {}
        self.desired = {}
        self.unannotated_ids = []
        self.total = 0
        self.catalog_total = self.catalog_annotated = 0
        self.revision = None
        self.catalog_lock = asyncio.Lock()
        self.index_lock = asyncio.Lock()
        self.error = None
        self.force_reset = False
        self.recover = False
        self.wakeup = asyncio.Event()

    async def refresh(self):
        async with self.catalog_lock:
            if self.revision == self.db.catalog_revision:
                return
            revision = self.db.catalog_revision
            rows = await self.db.semantic_catalog(include_disabled=True)
            self.catalog_total = len(rows)
            self.catalog_annotated = 0
            self.total = 0
            self.desired = {}
            self.unannotated_ids = []
            for row in rows:
                doc = document(row)
                if doc is not None:
                    self.catalog_annotated += 1
                else:
                    self.unannotated_ids.append(row["id"])
                if row["enabled"] and row["rating"] in self.settings.allowed_ratings:
                    self.total += 1
                    if doc is not None:
                        self.desired[doc["id"]] = doc
            self.revision = revision

    def _provider(self):
        if not self.settings.embedding_provider_id:
            return None
        if self.provider_lookup is None:
            raise SemanticError("尚未连接宿主 Embedding 模型")
        return self.provider_lookup(self.settings.embedding_provider_id)

    async def status(self):
        await self.refresh()
        paused = bool(await self.db.get_meta("semantic_paused"))
        configured = bool(self.settings.embedding_provider_id)
        current = False
        error = self.error
        model = ""
        if configured:
            try:
                provider = self._provider()
                model = provider.model
                current = self.provider is provider and self.profile == provider_profile(
                    provider, self.settings.embedding_provider_id
                )
            except Exception as exc:
                error = safe_error(exc)
        matched = (
            sum(
                self.entries.get(ident) == doc["fingerprint"] for ident, doc in self.desired.items()
            )
            if current and not self.force_reset and not self.recover
            else 0
        )
        complete = (
            current and matched == len(self.desired) and set(self.entries) == set(self.desired)
        )
        sufficient = len(self.desired) * 2 > self.total
        available = bool(
            configured
            and sufficient
            and complete
            and self.store
            and not error
            and not self.force_reset
            and not self.recover
        )
        if not configured:
            state = "unconfigured"
        elif error:
            state = "error"
        elif not self.total:
            state = "empty"
        elif available:
            state = "ready"
        elif paused:
            state = "paused"
        elif not complete or self.force_reset or self.recover:
            state = "indexing"
        else:
            state = "insufficient"
        return {
            "configured": configured,
            "paused": paused,
            "provider_id": self.settings.embedding_provider_id,
            "model": model,
            "total": self.total,
            "annotated": len(self.desired),
            "coverage": len(self.desired) / self.total if self.total else 0,
            "indexed": matched,
            "pending": len(self.desired) - matched,
            "catalog": {
                "total": self.catalog_total,
                "unannotated": self.catalog_total - self.catalog_annotated,
                "annotated_unindexed": self.catalog_annotated - matched,
                "indexed": matched,
            },
            "available": available,
            "state": state,
            "error": error,
        }

    def _cache_directory(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{32}", name):
            return None
        path = (self.root / name).resolve()
        return path if path.parent == self.root.resolve() else None

    async def _clean_old_caches(self):
        # Only owned UUID cache directories directly below this plugin's vector root.
        for candidate in self.root.iterdir():
            path = self._cache_directory(candidate.name)
            if (
                path is not None
                and path != self.directory
                and path.is_dir()
                and not candidate.is_symlink()
            ):
                await asyncio.to_thread(shutil.rmtree, path)

    async def ensure_store(self):
        provider = self._provider()
        if provider is None:
            return False
        profile = provider_profile(provider, self.settings.embedding_provider_id)
        if (
            self.provider is provider
            and self.profile == profile
            and self.store
            and not self.force_reset
            and not self.recover
        ):
            return True
        async with self.index_lock:
            if self.store:
                await self.store.close()
            self.store = None
            self.entries = {}
            self.root.mkdir(parents=True, exist_ok=True)
            cache = await self.db.get_meta("semantic_cache", {})
            directory = self._cache_directory(cache.get("directory"))
            reuse = (
                (
                    cache.get("profile") == profile
                    or cache.get("profile")
                    in getattr(provider, "previous_profiles", lambda _: set())(DOCUMENT_VERSION)
                )
                and directory is not None
                and directory.exists()
                and not cache.get("dirty")
                and not self.force_reset
                and not self.recover
            )
            self.embeddings = Embeddings(provider, self.settings.embedding_rpm)
            self.provider, self.profile = provider, profile
            for attempt in range(2):
                self.directory = (
                    directory if reuse and attempt == 0 else self.root / uuid.uuid4().hex
                )
                store = None
                try:
                    store = self.store_factory(self.directory, self.embeddings.dimension)
                    entries = await store.initialize()
                    self.store, self.entries = store, entries
                    await self.db.set_meta("semantic_cache", self._cache_state(False))
                    self.force_reset = self.recover = False
                    self.error = None
                    try:
                        await self._clean_old_caches()
                    except OSError:
                        logger.warning("LONG Hub 旧向量缓存清理失败，将在后续重建时重试")
                    return True
                except BaseException as exc:
                    if store is not None:
                        await store.close()
                    self.store = None
                    if not reuse or attempt or not isinstance(exc, Exception):
                        raise
            return False

    def _cache_state(self, dirty):
        return {"profile": self.profile, "directory": self.directory.name, "dirty": dirty}

    async def _mutate(self, operation):
        await self.db.set_meta("semantic_cache", self._cache_state(True))
        try:
            await operation()
            await self.db.set_meta("semantic_cache", self._cache_state(False))
        except BaseException:
            self.recover = True
            raise

    async def run_once(self):
        await self.refresh()
        if not await self.ensure_store():
            return False
        obsolete = [
            ident
            for ident in self.entries
            if ident not in self.desired
            or self.entries[ident] != self.desired[ident]["fingerprint"]
        ]
        if obsolete:
            async with self.index_lock:

                async def remove():
                    for ident in obsolete:
                        await self.store.delete(ident)
                        self.entries.pop(ident, None)

                await self._mutate(remove)
        if await self.db.get_meta("semantic_paused"):
            return bool(obsolete)
        missing = [
            doc
            for ident, doc in self.desired.items()
            if self.entries.get(ident) != doc["fingerprint"]
        ][:16]
        if not missing:
            self.error = None
            return bool(obsolete)
        store, embeddings, profile = self.store, self.embeddings, self.profile
        # Network requests run outside the index lock; queries needn't wait for backfill I/O.
        vectors = await embeddings.embed_many([doc["content"] for doc in missing])
        await self.refresh()
        async with self.index_lock:
            if (
                store is not self.store
                or self.force_reset
                or self.recover
                or profile
                != provider_profile(self._provider(), self.settings.embedding_provider_id)
            ):
                return True
            valid = [
                (doc, vector)
                for doc, vector in zip(missing, vectors, strict=True)
                if self.desired.get(doc["id"], {}).get("fingerprint") == doc["fingerprint"]
            ]
            if valid:

                async def insert():
                    await store.insert([doc for doc, _ in valid], [vector for _, vector in valid])
                    self.entries.update({doc["id"]: doc["fingerprint"] for doc, _ in valid})

                await self._mutate(insert)
        self.error = None
        return True

    async def run(self):
        failures = 0
        while True:
            try:
                worked = await self.run_once()
                failures = 0
                delay = 0.1 if worked else 2
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = safe_error(exc)
                logger.error(
                    "LONG Hub 语义索引更新失败：%s\n%s",
                    self.error,
                    error_details(exc, provider=self.provider, settings=self.settings),
                )
                failures += 1
                delay = min(60, 2 ** min(failures, 6))
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def rebuild(self):
        async with self.index_lock:
            self.force_reset = True
            cache = await self.db.get_meta("semantic_cache", {})
            if cache:
                await self.db.set_meta("semantic_cache", {**cache, "dirty": True})
        self.wakeup.set()
        return {"queued": True}

    async def set_paused(self, paused):
        await self.db.set_meta("semantic_paused", paused)
        self.wakeup.set()
        return {"paused": paused}

    async def search(self, concept, limit=5, recent=()):
        if not isinstance(concept, str) or not concept.strip() or len(concept) > 1000:
            raise SemanticError("请输入 1–1000 字的自然语言概念或使用场景")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise SemanticError("limit 须为 1–10 的整数")
        if not (await self.status())["available"]:
            raise SemanticError(
                "语义检索尚未就绪：需配置 Embedding、有效标注覆盖率严格超过 50% 且索引完整；可暂用标签或文字检索"
            )
        store, embeddings, profile = self.store, self.embeddings, self.profile
        vector = await embeddings.embed(concept.strip())
        async with self.index_lock:
            if (
                store is not self.store
                or profile
                != provider_profile(self._provider(), self.settings.embedding_provider_id)
                or not (await self.status())["available"]
            ):
                raise SemanticError("目录或 Embedding 提供商已更新，请稍后重试或暂用标签检索")
            results = await store.search(
                concept.strip(), vector, min(len(self.entries), max(20, limit * 4))
            )
        # Re-read candidates after the external request, including enable/rating/freshness.
        ids = [result["image_id"] for result in results]
        current = {
            doc["id"]: doc
            for row in await self.db.semantic_catalog(self.settings.allowed_ratings, ids)
            if (doc := document(row)) is not None
        }
        items = []
        for result in results:
            doc = current.get(result["image_id"])
            score = result["similarity"]
            if (
                doc
                and doc["fingerprint"] == result["fingerprint"]
                and isinstance(score, (int, float))
                and math.isfinite(score)
            ):
                items.append(
                    {key: doc[key] for key in ("id", "text", "rating", "description", "tags")}
                    | {
                        "similarity": round(max(-1.0, min(1.0, score)), 4),
                        "recently_sent": doc["id"] in recent,
                    }
                )
        if not (await self.status())["available"]:
            raise SemanticError("目录正在更新，语义索引暂不可用，请稍后重试或使用标签检索")
        return items[:limit]

    async def close(self):
        async with self.index_lock:
            if self.store:
                await self.store.close()
                self.store = None
