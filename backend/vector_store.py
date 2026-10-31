"""Thin adapter over AstrBot's own FAISS database. No separate vector dependency."""

from __future__ import annotations

import asyncio
import json
import math
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar


class SemanticError(ValueError):
    """A safe, user-facing error without provider response bodies or keys."""


def safe_error(exc):
    if isinstance(exc, SemanticError):
        return str(exc)
    status = getattr(exc, "status_code", None)
    detail = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
    return f"语义索引或 Embedding 调用失败（{detail}），请检查宿主嵌入模型配置"


def error_details(exc, *, provider=None, settings=None):
    """Retain diagnostic exception chains without dumping provider configuration or locals."""
    secrets = set()

    def collect(value, sensitive=False):
        if isinstance(value, dict):
            for key, child in value.items():
                name = str(key).lower().replace("-", "_")
                collect(
                    child,
                    sensitive
                    or name
                    in {
                        "key",
                        "keys",
                        "api_key",
                        "api_keys",
                        "apikey",
                        "secret",
                        "token",
                        "access_token",
                        "api_token",
                        "password",
                        "authorization",
                    },
                )
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child, sensitive)
        elif sensitive and isinstance(value, str) and value:
            secrets.add(value)
            scheme, _, credential = value.partition(" ")
            if scheme.lower() in {"bearer", "basic", "key"} and credential:
                secrets.add(credential)

    collect(getattr(provider, "provider_config", {}))
    for name in ("app_key", "webhook_secret"):
        collect(getattr(settings, name, ""), True)
    details = "".join(traceback.format_exception(exc))
    for secret in sorted(secrets, key=len, reverse=True):
        details = details.replace(secret, "[REDACTED]")
    return details if len(details) <= 16000 else details[:16000] + "\n[错误详情已截断]"


class Embeddings:
    def __init__(self, provider, rpm):
        self.provider = provider
        self.dimension = provider.get_dim()
        if (
            isinstance(self.dimension, bool)
            or not isinstance(self.dimension, int)
            or not 1 <= self.dimension <= 32768
        ):
            raise SemanticError("Embedding 向量维度无效，请在宿主中配置正确的维度")
        self.rpm = rpm
        self.rate_lock = asyncio.Lock()
        self.last_call = float("-inf")
        self.cache = ContextVar("longhub_precomputed_embeddings", default=None)

    def get_dim(self):
        return self.dimension

    def normalize(self, vector):
        if not isinstance(vector, (list, tuple)) or len(vector) != self.dimension:
            raise SemanticError("Embedding 返回的向量维度与提供商声明不一致")
        try:
            if any(isinstance(value, bool) for value in vector):
                raise ValueError()
            values = [float(value) for value in vector]
            norm = math.sqrt(sum(value * value for value in values))
            if (
                not all(math.isfinite(value) for value in values)
                or not math.isfinite(norm)
                or norm <= 0
            ):
                raise ValueError()
            return [value / norm for value in values]
        except (ValueError, TypeError, OverflowError):
            raise SemanticError("Embedding 返回了无效或全零向量") from None

    async def _rate(self):
        async with self.rate_lock:
            delay = self.last_call + 60 / self.rpm - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_call = time.monotonic()

    async def embed(self, text):
        await self._rate()
        try:
            async with asyncio.timeout(60):
                return self.normalize(await self.provider.get_embedding(text))
        except Exception as exc:
            raise SemanticError(safe_error(exc)) from exc

    async def embed_many(self, texts):
        await self._rate()
        try:
            async with asyncio.timeout(90):
                vectors = await self.provider.get_embeddings_batch(
                    texts,
                    batch_size=16,
                    tasks_limit=1,
                    max_retries=1,
                )
            if len(vectors) != len(texts):
                raise SemanticError("Embedding 批量返回的向量数量与文本数量不一致")
            return [self.normalize(vector) for vector in vectors]
        except Exception as exc:
            raise SemanticError(safe_error(exc)) from exc

    @contextmanager
    def precomputed(self, texts, vectors):
        token = self.cache.set(dict(zip(texts, vectors, strict=True)))
        try:
            yield
        finally:
            self.cache.reset(token)

    async def get_embedding(self, text):
        cache = self.cache.get()
        return cache[text] if cache is not None and text in cache else await self.embed(text)

    async def get_embeddings_batch(self, texts, **kwargs):
        cache = self.cache.get()
        if cache is not None and all(text in cache for text in texts):
            return [cache[text] for text in texts]
        return await self.embed_many(texts)


class NativeVectorStore:
    def __init__(self, directory, embeddings):
        # Keep this optional import lazy: missing FAISS must not break Hub sync/chat.
        try:
            from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
        except ImportError:
            raise SemanticError("AstrBot 内置 FAISS 向量库不可用，请检查知识库组件依赖") from None
        directory.mkdir(parents=True, exist_ok=True)
        self.embeddings = embeddings
        self.db = FaissVecDB(
            doc_store_path=str(directory / "documents.sqlite3"),
            index_store_path=str(directory / "index.faiss"),
            embedding_provider=embeddings,
        )

    async def initialize(self):
        await self.db.initialize()
        documents = await self.db.document_storage.get_documents({}, offset=None, limit=None)
        import faiss

        ids = set(
            int(value) for value in faiss.vector_to_array(self.db.embedding_storage.index.id_map)
        )
        if ids != {row["id"] for row in documents}:
            raise SemanticError("向量与文档缓存不一致，需要重建")
        entries = {}
        for row in documents:
            meta = (
                json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
            )
            if (
                not isinstance(meta, dict)
                or meta.get("image_id") != row["doc_id"]
                or not isinstance(meta.get("fingerprint"), str)
            ):
                raise SemanticError("向量文档元数据无效，需要重建")
            entries[row["doc_id"]] = meta["fingerprint"]
        return entries

    async def insert(self, documents, vectors):
        texts = [doc["content"] for doc in documents]
        with self.embeddings.precomputed(texts, vectors):
            await self.db.insert_batch(
                contents=texts,
                metadatas=[
                    {"image_id": doc["id"], "fingerprint": doc["fingerprint"]} for doc in documents
                ],
                ids=[doc["id"] for doc in documents],
                batch_size=16,
                tasks_limit=1,
                max_retries=1,
            )

    async def delete(self, ident):
        await self.db.delete(ident)

    async def search(self, concept, vector, count):
        with self.embeddings.precomputed([concept], [vector]):
            results = await self.db.retrieve(concept, k=count)
        rows = []
        for result in results:
            meta = result.data["metadata"]
            meta = json.loads(meta) if isinstance(meta, str) else meta
            rows.append({**meta, "similarity": float(result.similarity)})
        return rows

    async def close(self):
        await self.db.close()
