"""Rate-limited, normalized embeddings from MaiBot model tasks."""

from __future__ import annotations

import asyncio
import math
import time

from .errors import SemanticError, safe_error


class Embeddings:
    def __init__(self, provider, rpm):
        self.provider = provider
        self.dimension = provider.dimension
        if (
            isinstance(self.dimension, bool)
            or not isinstance(self.dimension, int)
            or not 1 <= self.dimension <= 32768
        ):
            raise SemanticError("Embedding 向量维度无效，请在宿主中配置正确的维度")
        self.rpm = rpm
        self.rate_lock = asyncio.Lock()
        self.last_call = float("-inf")

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
                return self.normalize(await self.provider.embed(text))
        except Exception as exc:
            raise SemanticError(safe_error(exc)) from exc

    async def embed_many(self, texts):
        await self._rate()
        try:
            async with asyncio.timeout(90):
                vectors = await self.provider.embed_many(texts)
            if len(vectors) != len(texts):
                raise SemanticError("Embedding 批量返回的向量数量与文本数量不一致")
            return [self.normalize(vector) for vector in vectors]
        except Exception as exc:
            raise SemanticError(safe_error(exc)) from exc
