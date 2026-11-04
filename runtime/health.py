"""Bounded, independent API probes for the /health command."""

from __future__ import annotations

import asyncio
import time

from ..backend.client import HubError

PROBE_TIMEOUT_SECONDS = 30


class HealthError(ValueError):
    """A fixed diagnostic message that never contains provider response bodies."""


async def llm_probe(ctx):
    result = await ctx.llm.generate(
        prompt=[
            {"role": "system", "content": "你在执行连通性检测。请仅回复 OK，不要分析或解释。"},
            {"role": "user", "content": "请回复 OK。"},
        ],
        task_name="replyer",
        max_tokens=64,
        timeout_ms=30000,
    )
    if not isinstance(result, dict) or result.get("success") is not True:
        raise HealthError("补全调用失败，请检查宿主模型日志")
    response = result.get("response")
    if not isinstance(response, str) or not response.strip():
        raise HealthError("补全未返回有效文本")
    # Model output is diagnostic only and is never sent or added to chat history.


async def timed_probe(name, operation, logger):
    started = time.perf_counter()
    detail = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await operation()
    except TimeoutError:
        detail = "请求超时"
    except HubError as exc:
        detail = f"HTTP {exc.status}"
    except HealthError as exc:
        detail = str(exc)
    except Exception as exc:
        # Host/provider exceptions can contain credentials or raw upstream bodies.
        detail = f"调用失败：{type(exc).__name__}"
    elapsed_ms = (time.perf_counter() - started) * 1000
    if detail is not None:
        logger.warning("LONG Hub health check failed: service=%s detail=%s", name, detail)
    logger.info(
        "LONG Hub health check: service=%s available=%s latency_ms=%.0f",
        name,
        detail is None,
        elapsed_ms,
    )
    state = "可用" if detail is None else f"不可用（{detail}）"
    return f"{name}：{state}，{elapsed_ms:.0f} ms"


async def health_report(ctx, client):
    llm, hub = await asyncio.gather(
        timed_probe("LLM API", lambda: llm_probe(ctx), ctx.logger),
        timed_probe("LONG Hub API", client.healthcheck, ctx.logger),
    )
    return f"Bot：在线\n{llm}\n{hub}"
