"""Use the host's public model and embedding capabilities over SDK RPC."""

from __future__ import annotations

import asyncio
import math

from ..backend.vector_store import SemanticError


def _vector(payload):
    values = payload.get("embedding")
    if not isinstance(values, list) or not 1 <= len(values) <= 32768:
        raise SemanticError("MaiBot Embedding 返回了无效的向量")
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
        for v in values
    ):
        raise SemanticError("MaiBot Embedding 返回了无效的数值")
    return values


class MaiBotEmbedding:
    def __init__(self, ctx, task, model, dimension):
        self.ctx, self.task, self.model, self.dimension = ctx, task, model, dimension
        self.provider_config = {"type": "maibot_sdk", "embedding_dimensions": dimension}

    def get_dim(self):
        return self.dimension

    def get_model(self):
        return self.model

    async def get_embedding(self, text):
        return (await self.get_embeddings_batch([text]))[0]

    async def get_embeddings_batch(self, texts, **kwargs):
        del kwargs
        result = await self.ctx.llm.embed(texts=texts, task_name=self.task, max_concurrent=1)
        if result.get("success") is not True:
            raise SemanticError("MaiBot Embedding 调用失败，请检查宿主模型任务日志")
        rows = result.get("results")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise SemanticError("MaiBot Embedding 返回数量不匹配")
        values = []
        for row in rows:
            if not isinstance(row, dict) or row.get("success") is False:
                raise SemanticError("MaiBot Embedding 批次中有失败的文本，请检查宿主模型日志")
            vector = _vector(row)
            if len(vector) != self.dimension or row.get("model_name") != self.model:
                raise SemanticError("MaiBot Embedding 任务切换了模型或维度，请重新保存设置重建索引")
            values.append(vector)
        return values


class Models:
    def __init__(self, ctx):
        self.ctx, self.embedding, self.error = ctx, None, None
        self.task = ""
        self.probe = None

    def lookup(self, task):
        if task != self.task or self.embedding is None:
            raise SemanticError(self.error or "MaiBot Embedding 正在初始化")
        return self.embedding

    def start(self, task):
        self.task, self.embedding, self.error = task, None, None
        if task:
            self.probe = asyncio.create_task(self._prepare())

    async def _prepare(self):
        delay = 5
        while self.embedding is None:
            try:
                result = await asyncio.wait_for(
                    self.ctx.llm.embed(text="LONG Hub embedding profile", task_name=self.task), 20
                )
                if result.get("success") is not True:
                    raise SemanticError("MaiBot Embedding 任务不可用，请检查宿主模型设置")
                values = _vector(result)
                model = result.get("model_name")
                if not isinstance(model, str) or not model:
                    raise SemanticError("MaiBot Embedding 没有返回实际模型名称")
                self.embedding = MaiBotEmbedding(self.ctx, self.task, model, len(values))
                self.error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = (
                    str(exc)
                    if isinstance(exc, SemanticError)
                    else f"MaiBot Embedding 初始化失败（{type(exc).__name__}）"
                )
                self.ctx.logger.warning("LONG Hub: %s", self.error)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def close(self):
        if self.probe is not None:
            self.probe.cancel()
            await asyncio.gather(self.probe, return_exceptions=True)
            self.probe = None

    async def generate(self, *, provider_id, prompt, system_prompt, image_url):
        result = await self.ctx.llm.generate(
            prompt=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
            model=provider_id,
            max_tokens=900,
            timeout_ms=90000,
        )
        if result.get("success") is not True or not isinstance(result.get("response"), str):
            raise ValueError("MaiBot 视觉模型调用失败，请检查宿主模型任务日志")
        return result["response"]
