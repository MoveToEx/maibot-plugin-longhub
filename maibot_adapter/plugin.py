from __future__ import annotations

import asyncio
import base64
import json
import time

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin, ReplyExtension, Tool
from maibot_sdk.types import HookMode, HookOrder

from ..backend.models import image_id
from ..backend.service import Service
from ..backend.vector_store import error_details, safe_error
from .delivery import Deliveries, PreparedReply, check_frame
from .panel import Panel
from .policy import SEARCH_POLICY, inject, turn_key
from .providers import Models
from .settings import PluginConfig, SettingsFile
from .vector_store import SQLiteVectorStore

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
            vector_store_factory=SQLiteVectorStore,
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
        turn = self.deliveries.enter(session, turn_key(items))
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

    @Tool(
        "search_longhub_memes",
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
        description="有意义的聊天上下文与期望表情明确相关时，按概念语义寻找龙图候选。仅标注覆盖率 >50% 且索引完整时开放。不自动发送，相似度不是概率。核对候选后决定发送。",
        parameters={
            "concept": {
                "type": "string",
                "required": True,
                "minLength": 1,
                "maxLength": 1000,
                "description": "想表达的概念、情绪或场景，1–1000 字",
            },
            "limit": LIMIT,
        },
    )
    async def search_semantic(self, concept, limit=5, stream_id="", **kwargs):
        del kwargs
        self.log_tool("search_longhub_memes_semantic", concept=concept, limit=limit)
        async with self.lifecycle_lock:
            service = self.require_service()
            self._limit(limit, 10)
            turn = self.deliveries.current(stream_id)
            try:
                items = await service.semantic.search(
                    concept, limit, recent=await self._recent(stream_id)
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
        description="为当前回复附加已检索到的一张龙图，自主选择文字前/后，或只发图。每轮最多一张，不重复已发送图。",
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
                    "extra_prompt": "本轮将由 LONG Hub 附加一张龙图，正文不重复图片已有表达，不写发送确认。"
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
