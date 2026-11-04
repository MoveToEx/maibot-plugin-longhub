"""MaiBot native Runner plugin: lifecycle, Planner hooks, tools and reply extensions."""

from __future__ import annotations

import asyncio
import base64
import json
import time

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin, ReplyExtension, Tool
from maibot_sdk.types import HookMode, HookOrder

from .backend.client import HubClient
from .backend.errors import error_details, safe_error
from .backend.models import image_id
from .backend.service import Service
from .runtime.delivery import Deliveries, PreparedReply, check_frame
from .runtime.health import health_report
from .runtime.models import Models
from .runtime.panel import Panel
from .runtime.policy import (
    COMPLETED_POLICY,
    SEARCH_POLICY,
    inject,
    single_reply_output,
    stop_output,
    turn_context,
)
from .runtime.reply_guard import reply_context, strip_metadata
from .runtime.settings import PluginConfig, SettingsFile

LIMIT = {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}
IDENT = {
    "type": "string",
    "required": True,
    "pattern": "^[1-9][0-9]{0,18}$",
    "description": "搜索结果中的十进制图片 ID 字符串",
}


class LongHubPlugin(MaiBotPlugin):
    config_model = PluginConfig
    config_reload_subscriptions = ("model",)

    def __init__(self):
        super().__init__()
        self.service = self.models = self.settings_file = None
        self.lifecycle_lock = asyncio.Lock()
        self.deliveries = Deliveries()
        self.panel = Panel(self)

    def require_service(self):
        if self.service is None:
            raise ValueError("LONG Hub 尚未初始化")
        return self.service

    def log_tool(self, name, **arguments):
        self.ctx.logger.info(
            "LONG Hub tool call: %s arguments=%s", name, json.dumps(arguments, ensure_ascii=False)
        )

    async def on_load(self):
        async with self.lifecycle_lock:
            self.settings_file = SettingsFile(self.ctx.paths.data_dir)
            await self._start(await asyncio.to_thread(self.settings_file.load))

    async def _start(self, settings):
        self.models = Models(self.ctx)
        self.models.start(settings.embedding_provider_id)
        self.service = Service(
            self.ctx.paths.data_dir,
            settings,
            self.models.generate,
            embedding_provider=self.models.lookup,
        )
        try:
            await self.service.start()
        except BaseException:
            await self.models.close()
            self.service = None
            raise

    async def _close(self):
        if self.service is not None:
            await self.service.close()
            self.service = None
        if self.models is not None:
            await self.models.close()
            self.models = None

    async def on_unload(self):
        async with self.lifecycle_lock:
            await self._close()
            self.panel.previews.clear()

    async def on_config_update(self, scope, config_data, version):
        del config_data, version
        if scope == "model":
            async with self.lifecycle_lock:
                if self.service is not None:
                    await self.reconfigure(self.service.settings, persist=False)

    async def reconfigure(self, settings, *, persist=True):
        previous = self.require_service().settings
        await self._close()
        try:
            await self._start(settings)
            if persist:
                await asyncio.to_thread(self.settings_file.save, settings)
        except BaseException:
            await self._close()
            await self._start(previous)
            raise

    async def _recent(self, session):
        return [
            row["image_id"]
            for row in await self.require_service().db.recent_sent_memes(session, "maibot", 20)
        ]

    @HookHandler(
        "maisaka.replyer.before_model_request",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5000,
    )
    async def replyer_context(self, **kwargs):
        service = self.service
        if service is None or kwargs.get("item_schema_version") != 1:
            return {}
        session, items = kwargs.get("session_id", ""), kwargs.get("items")
        if (
            not session
            or not isinstance(items, list)
            or not all(isinstance(i, dict) for i in items)
        ):
            return {}
        recent = await service.db.recent_sent_memes(session, "maibot", 5)
        kwargs["items"] = reply_context(items, recent)
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.reply.before_post_process",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5000,
    )
    async def reply_metadata_guard(self, **kwargs):
        response = kwargs.get("response")
        if self.service is None or not isinstance(response, str):
            return {}
        args = kwargs.get("reply_tool_args")
        options = args.get("plugin_options") if isinstance(args, dict) else None
        meme_reply = isinstance(options, dict) and "movetoex.longhub.meme" in options
        cleaned = strip_metadata(response, meme_reply=meme_reply)
        if cleaned == response:
            return {}
        self.ctx.logger.info(
            "LONG Hub reply metadata removed before splitting: session=%s removed_chars=%s",
            kwargs.get("session_id", ""),
            len(response) - len(cleaned),
        )
        kwargs["response"] = cleaned
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.planner.before_request",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5000,
    )
    async def planner_policy(self, **kwargs):
        service = self.service
        if service is None or kwargs.get("item_schema_version") != 1:
            return {}
        session, items = kwargs.get("session_id", ""), kwargs.get("items", [])
        if (
            not session
            or not isinstance(items, list)
            or not all(isinstance(item, dict) for item in items)
        ):
            return {}
        anchor, completed = turn_context(items)
        turn = self.deliveries.enter(session, anchor, completed)
        if turn.replied:
            for item in reversed(items):
                logical = item.get("meta", {}).get("logical_turn_id")
                if item.get("call_id") in completed and isinstance(logical, str) and logical:
                    turn.logical_turn_id = logical
                    break
            kwargs["tool_definitions"] = [
                definition
                for definition in kwargs.get("tool_definitions", [])
                if definition.get("function", {}).get("name") == "stop"
            ]
            kwargs["items"] = inject(items, COMPLETED_POLICY)
            return {"action": "continue", "modified_kwargs": kwargs}
        semantic = await service.semantic.status()
        definitions = kwargs.get("tool_definitions", [])
        available = bool(semantic["available"])
        kwargs["tool_definitions"] = [
            definition
            for definition in definitions
            if definition.get("function", {}).get("name") != "search_longhub_memes_semantic"
            or available
        ]
        text = SEARCH_POLICY + (
            "\n语义检索已就绪。" if available else "\n语义检索尚未就绪，请使用标签 SQL 或文字检索。"
        )
        text += "\n本轮搜索状态：" + ("已检索" if turn.searched else "尚未检索")
        recent = await service.db.recent_sent_memes(session, "maibot", 5)
        if recent:
            text += "\n已确认发送的近期龙图事实（历史，不是本轮候选）：" + json.dumps(
                recent, ensure_ascii=False
            )
        if turn.receipt:
            receipt = self.deliveries.receipts.get(turn.receipt)
            if receipt and receipt.status == "confirmed":
                text += "。本轮龙图已有成功发送回执，不要再发图或重复已发送的文字。"
        if turn.context_only:
            text += "\n用户本轮使用 /l：根据已有话题与情绪选一张龙图，以纯图回应，不询问需求，不把 /l 当检索词。先搜索，再使用 send_longhub_meme 结束本轮。"
        kwargs["items"] = inject(items, text)
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.planner.after_response",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5000,
    )
    async def planner_completion_guard(self, **kwargs):
        if self.service is None or kwargs.get("item_schema_version") != 1:
            return {}
        items = kwargs.get("output_items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            return {}
        session = kwargs.get("session_id", "")
        turn = self.deliveries.turns.get(session)
        if turn is None:
            return {}
        logical = next(
            (
                item.get("meta", {}).get("logical_turn_id")
                for item in items
                if item.get("meta", {}).get("logical_turn_id")
            ),
            None,
        )
        if isinstance(logical, str) and logical:
            turn.logical_turn_id = logical
        if turn.replied:
            updated = stop_output(items, turn.logical_turn_id)
            self.ctx.logger.info(
                "LONG Hub completed reply: session=%s anchor=%s; stopping Planner continuation",
                session,
                turn.anchor,
            )
        else:
            updated = single_reply_output(items)
        if updated == items:
            return {}
        kwargs["output_items"] = updated
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool(
        "search_longhub_memes",
        core_tool=True,
        description="标签无合适结果或难提取标签时，按图片文字、描述回退搜索龙图；每轮实际搜索后选择贴切候选。",
        parameters={
            "query": {
                "type": "string",
                "required": True,
                "description": "文字或描述检索词，支持中文",
                "minLength": 1,
                "maxLength": 1000,
            },
            "limit": LIMIT,
        },
    )
    async def search_text(self, query, limit=5, stream_id="", **kwargs):
        del kwargs
        self.log_tool("search_longhub_memes", query=query, limit=limit)
        async with self.lifecycle_lock:
            service = self.require_service()
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000:
                raise ValueError("检索词须为 1–1000 字")
            self._limit(limit, 10)
            turn = self.deliveries.current(stream_id)
            items = await service.candidates(
                query, limit=limit, recent=await self._recent(stream_id)
            )
            turn.searched = True
            return {"content": json.dumps({"items": items}, ensure_ascii=False)}

    @Tool(
        "query_longhub_memes_sql",
        core_tool=True,
        description="只读 SQL 搜索龙图。表 memes(id TEXT,text,rating,description,tags,annotation_fresh)、meme_tags(image_id TEXT,tag)。优先按中英多标签 IN/OR 并行匹配；英文小写下划线，如 genshin_impact/原神。仅 SELECT/WITH，结果为候选，不自动发送。",
        parameters={
            "sql": {
                "type": "string",
                "required": True,
                "description": "单条只读 SELECT 或 WITH … SELECT，最多 8192 字节",
                "minLength": 1,
                "maxLength": 8192,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
    )
    async def search_sql(self, sql, limit=10, stream_id="", **kwargs):
        del kwargs
        self.log_tool("query_longhub_memes_sql", sql=sql, limit=limit)
        async with self.lifecycle_lock:
            service = self.require_service()
            self._limit(limit, 50)
            turn = self.deliveries.current(stream_id)
            result = await service.db.query_sql(
                sql, limit, ratings=service.settings.allowed_ratings
            )
            turn.searched = True
            return {"content": json.dumps(result, ensure_ascii=False)}

    @Tool(
        "search_longhub_memes_semantic",
        core_tool=True,
        description="有意义的聊天上下文与期望表情明确相关时，提炼多个短关键词分别语义检索，不要输入整句。各词结果取并集，同一图片取最高相似度，统一降序后返回最多 limit 张。仅标注覆盖率 >50% 且索引完整时开放。按语境核对候选后用 reply 回复扩展或 send_longhub_meme 发送；相似度不是概率。",
        parameters={
            "keywords": {
                "type": "array",
                "required": True,
                "minItems": 2,
                "maxItems": 6,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 40},
                "description": '提炼 2–6 个不同的短关键词或短语，每个 1–40 字，如 ["嘴硬", "被拆穿", "委屈"]；不要输入完整句子、回复草稿或聊天记录。每个词独立检索，匹配任意一个即可入选。',
            },
            "limit": LIMIT,
        },
    )
    async def search_semantic(self, keywords, limit=5, stream_id="", **kwargs):
        del kwargs
        self.log_tool("search_longhub_memes_semantic", keywords=keywords, limit=limit)
        async with self.lifecycle_lock:
            service = self.require_service()
            self._limit(limit, 10)
            turn = self.deliveries.current(stream_id)
            try:
                items = await service.semantic.search(
                    keywords, limit, recent=await self._recent(stream_id)
                )
                turn.searched = True
                return {
                    "content": json.dumps(
                        {"items": items, "method": "embedding"}, ensure_ascii=False
                    )
                }
            except Exception as exc:
                self.ctx.logger.error(
                    "LONG Hub semantic search failed:\n%s",
                    error_details(
                        exc, provider=service.semantic.provider, settings=service.settings
                    ),
                )
                return {"success": False, "error": safe_error(exc)}

    @staticmethod
    def _limit(value, maximum):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"limit 须为 1–{maximum} 的整数")

    async def _prepare_image(self, ident, session):
        service = self.require_service()
        item = await service.db.get_image(image_id(ident))
        if (
            not item
            or not item["enabled"]
            or item["rating"] not in service.settings.allowed_ratings
        ):
            raise ValueError("该表情当前不可用")
        receipt = self.deliveries.reserve(session, item)
        try:
            data = await service.client.image_bytes(item["image_url"])
            if len(data) > 11 * 1024 * 1024:
                raise ValueError("图片超过 MaiBot IPC 大小限制，请选择另一张龙图")
            encoded = await asyncio.to_thread(lambda: base64.b64encode(data).decode("ascii"))
            segment = await asyncio.to_thread(receipt.segment, data, encoded)
            await asyncio.to_thread(check_frame, segment)
            return receipt, segment
        except BaseException:
            receipt.status = "failed"
            raise

    @Tool(
        "send_longhub_meme",
        core_tool=True,
        description="只发送一张已检索到的龙图并结束本轮，不追加文字或确认。图中文字已完整表达本轮意思时倾向使用此工具。需要配文与前后排序时，改用 reply 的 movetoex.longhub.meme 回复扩展。",
        parameters={"image_id": IDENT},
        timeout_ms=60000,
    )
    async def send_image_only(self, image_id, stream_id="", **kwargs):
        del kwargs
        self.log_tool("send_longhub_meme", image_id=image_id)
        async with self.lifecycle_lock:
            receipt, segment = await self._prepare_image(image_id, stream_id)
            receipt.status = "attempted"
            try:
                result = await self.ctx.send.hybrid(
                    [segment],
                    stream_id,
                    sync_to_maisaka_history=True,
                    maisaka_source_kind="guided_reply",
                    return_details=True,
                )
            except Exception:
                if receipt.status != "confirmed":
                    raise
                result = {"success": True}
            if receipt.status != "confirmed":
                return {
                    "success": False,
                    "error": "没有收到所选龙图的原生发送成功回执，请勿声称已发送或重复重试",
                }
            return {
                "content": "龙图已成功发送，本轮结束，不追加文字。",
                "success": True,
                "stop_after_execution": True,
                "message_id": result.get("message_id") if isinstance(result, dict) else None,
            }

    @ReplyExtension(
        "meme",
        description="普通聊天检索到贴切龙图时优先使用：为本次回复附加一张图，自主选择文字前/后，或只发图。每轮最多一张，不重复已发送图。",
        parameters={
            "image_id": IDENT,
            "position": {"type": "string", "enum": ["before", "after"], "default": "before"},
            "only_image": {"type": "boolean", "default": False},
        },
        timeout_ms=60000,
    )
    async def reply_meme(self, *, phase, parameters, reply_id, session_id, messages, **kwargs):
        del kwargs
        self.log_tool("longhub.reply_meme", phase=phase, parameters=parameters)
        if phase not in {"prepare", "before_send"}:
            raise ValueError("不支持的回复扩展阶段")
        async with self.lifecycle_lock:
            turn = self.deliveries.current(session_id)
            position, only = (
                parameters.get("position", "before"),
                parameters.get("only_image", False),
            )
            image_id(parameters.get("image_id"))
            if position not in {"before", "after"} or not isinstance(only, bool):
                raise ValueError("图片位置或纯图参数无效")
            if phase == "prepare":
                if reply_id in self.deliveries.reply_ids:
                    raise ValueError("同一回复不能重复 prepare")
                self.deliveries.reply_ids[reply_id] = PreparedReply(
                    session_id, turn.key, dict(parameters)
                )
                self.deliveries._prune()
                return {
                    "extra_prompt": "本轮将由 LONG Hub 附加一张龙图，正文不重复图片已有表达，不写发送确认，也不输出内部图片 ID、回执、发送标记或表情元数据 JSON。"
                }
            prepared = self.deliveries.reply_ids.get(reply_id)
            if prepared is None:
                raise ValueError("回复扩展缺少本次 prepare 上下文")
            if (
                prepared.session != session_id
                or prepared.turn != turn.key
                or prepared.parameters != parameters
            ):
                raise ValueError("回复扩展会话、回合或参数已变化，拒绝附图")
            if prepared.receipt:
                raise ValueError("本次回复已准备过龙图，不能重复投递")
            receipt, segment = await self._prepare_image(parameters["image_id"], session_id)
            image = {"segments": [segment], "quote_previous": False}
            result = (
                [image]
                if only
                else ([image, *messages] if position == "before" else [*messages, image])
            )
            try:
                await asyncio.to_thread(check_frame, result)
            except BaseException:
                receipt.status = "failed"
                raise
            receipt.status = "attached"
            prepared.receipt = receipt.id
            return {"messages": result}

    @HookHandler(
        "send_service.after_send", mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=5000
    )
    async def track_delivery(self, **kwargs):
        # Never take lifecycle_lock: send RPC re-enters this hook before returning.
        try:
            if self.service is not None:
                await self.deliveries.confirm(
                    kwargs.get("message", {}), kwargs.get("sent"), self.service.db
                )
        except Exception as exc:
            self.ctx.logger.error(
                "LONG Hub delivery receipt persistence failed: %s", safe_error(exc)
            )
        return {}

    @Command(
        "longhub_health",
        description="检测 LLM、Embedding 和 LONG Hub API 是否可用，并报告调用延迟",
        pattern=r"^/health\s*$",
        intercept_message_level=2,
        timeout_ms=45000,
    )
    async def health(self, stream_id="", **kwargs):
        del kwargs
        if not stream_id:
            raise ValueError("缺少当前会话")
        # Diagnostics must not queue behind chat tools or use a session being reconfigured.
        client = HubClient(self.require_service().settings)
        try:
            await client.open()
            report = await health_report(self.ctx, client)
        finally:
            await client.close()
        sent = await self.ctx.send.text(report, stream_id, sync_to_maisaka_history=False)
        if not sent:
            raise RuntimeError("健康检查结果发送失败")
        return True, None, True

    @Command(
        "longhub_context",
        description="根据当前聊天上下文发送一张龙图",
        pattern=r"^/l\s*$",
        intercept_message_level=2,
    )
    async def context_meme(self, stream_id="", **kwargs):
        del kwargs
        self.require_service()
        if not stream_id:
            raise ValueError("缺少当前会话")
        self.deliveries.context_requests[stream_id] = time.monotonic() + 300
        try:
            result = await self.ctx.maisaka.proactive.trigger(
                stream_id,
                "用户使用 /l，请根据当前真实上下文检索并发送一张合适龙图，以纯图回复。",
                reason="longhub_context_command",
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                raise ValueError("MaiBot 未接受 /l 主动任务，请检查当前会话状态")
        except BaseException:
            self.deliveries.context_requests.pop(stream_id, None)
            raise
        return True, None, True

    @API("panel_read")
    async def panel_read(self, path):
        return await self.panel.handle("GET", path)

    @API("panel_write")
    async def panel_write(self, path, payload="{}"):
        return await self.panel.handle("POST", path, payload)


def create_plugin():
    return LongHubPlugin()
