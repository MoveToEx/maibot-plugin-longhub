"""Static WebUI API dispatcher; no unauthenticated management HTTP listener."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from collections import OrderedDict
from urllib.parse import parse_qsl, urlsplit

from PIL import Image, ImageOps

from ..backend.models import RATINGS, Settings, image_id, tags


def preview_image(data):
    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError("图片像素超过预览限制")
        source.seek(0)
        image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail((640, 640))
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        output = io.BytesIO()
        background.save(output, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


class Panel:
    def __init__(self, plugin):
        self.plugin = plugin
        self.previews = OrderedDict()

    async def handle(self, method, path, payload="{}"):
        if method not in {"GET", "POST"} or not isinstance(path, str) or len(path) > 10000:
            raise ValueError("管理请求无效")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
            raise ValueError("只接受插件内部路径")
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        query = dict(pairs)
        if len(pairs) != len(query) or len(pairs) > 12:
            raise ValueError("查询参数重复或过多")
        if not isinstance(payload, str) or len(payload.encode()) > 65536:
            raise ValueError("管理请求超过大小限制")
        body = json.loads(payload)
        if not isinstance(body, dict):
            raise ValueError("请求体须为 JSON 对象")
        preview = re.fullmatch(r"/memes/([1-9][0-9]{0,18})/preview", parsed.path)
        if method == "GET" and preview:
            return await self._preview(image_id(preview[1]))
        async with self.plugin.lifecycle_lock:
            return await self._dispatch(method, parsed.path, query, body)

    async def _preview(self, ident):
        async with self.plugin.lifecycle_lock:
            service = self.plugin.require_service()
            item = await service.db.get_image(ident)
            if not item:
                raise ValueError("目录中没有这张图片")
            key = (item["id"], item["image_url"])
            if key in self.previews:
                self.previews.move_to_end(key)
                return {"data_url": self.previews[key]}
        # Do not block settings, chat tools or another preview on network I/O.
        data = await asyncio.wait_for(service.client.image_bytes(item["image_url"]), 6)
        preview = await asyncio.to_thread(preview_image, data)
        async with self.plugin.lifecycle_lock:
            if self.plugin.service is not service:
                raise ValueError("设置已变化，请刷新预览")
            self.previews[key] = preview
            while (
                sum(map(len, self.previews.values())) > 8 * 1024 * 1024 or len(self.previews) > 100
            ):
                self.previews.popitem(last=False)
        return {"data_url": preview}

    async def _dispatch(self, method, path, query, body):
        service = self.plugin.require_service()
        if method == "GET":
            if path == "/status":
                return await service.status()
            if path == "/settings":
                models = await self.plugin.ctx.llm.get_available_models()
                return {
                    "settings": service.settings.public(),
                    "providers": [{"id": value, "name": value} for value in models],
                    "embedding_providers": [{"id": "embedding", "name": "MaiBot Embedding 任务"}],
                }
            if path in {"/memes", "/webhook/logs"}:
                page, limit = int(query.get("page", "1")), int(query.get("limit", "30"))
                if not 1 <= page <= 1000000 or not 1 <= limit <= 100:
                    raise ValueError("分页参数无效")
                if path == "/webhook/logs":
                    return await service.db.logs(
                        page, limit, query.get("event", ""), query.get("failed") == "true"
                    )
                rating = query.get("rating", "")
                if rating and rating not in RATINGS:
                    raise ValueError("分级无效")
                return await service.db.list_images(
                    page=page,
                    limit=limit,
                    query=query.get("q", ""),
                    rating=rating,
                    annotation=query.get("annotation", ""),
                    enabled=query.get("enabled", ""),
                )
            match = re.fullmatch(r"/memes/([1-9][0-9]{0,18})", path)
            if match:
                item = await service.db.get_image(image_id(match[1]))
                if not item:
                    raise ValueError("目录中没有这张图片")
                return item
        elif path == "/settings/save":
            values = service.settings.__dict__ | body
            if set(body) - set(service.settings.__dict__):
                raise ValueError("存在未知的设置项")
            settings = Settings.from_dict(values)
            if settings.embedding_provider_id not in {"", "embedding"}:
                raise ValueError("请选择 MaiBot Embedding 任务")
            if settings.vision_provider_id:
                models = await self.plugin.ctx.llm.get_available_models()
                if (
                    settings.vision_provider_id not in models
                    and settings.vision_provider_id != "vlm"
                ):
                    raise ValueError("所选 MaiBot 模型不存在")
            await self.plugin.reconfigure(settings)
            self.previews.clear()
            return {"saved": True, "settings": settings.public()}
        elif path == "/sync":
            force = body.get("force", False)
            if not isinstance(force, bool):
                raise ValueError("force 须为布尔值")
            return service.start_sync(force=force)
        elif path == "/tagging/enqueue":
            ids = body.get("ids")
            if ids is not None:
                if not isinstance(ids, list) or len(ids) > 1000:
                    raise ValueError("每次最多选择 1000 张图片")
                ids = list(dict.fromkeys(image_id(value) for value in ids))
            force = body.get("force", False)
            if not isinstance(force, bool):
                raise ValueError("force 须为布尔值")
            return await service.bulk_tags(ids, force=force)
        elif path == "/tagging/auto":
            if not isinstance(body.get("enabled"), bool):
                raise ValueError("enabled 须为布尔值")
            return await service.set_auto_tagging(body["enabled"])
        elif path in {"/tagging/pause", "/semantic/pause"}:
            if not isinstance(body.get("paused"), bool):
                raise ValueError("paused 须为布尔值")
            if path == "/semantic/pause":
                return await service.semantic.set_paused(body["paused"])
            await service.db.set_meta("tagging_paused", body["paused"])
            return {"paused": body["paused"]}
        elif path == "/semantic/rebuild":
            if not service.settings.embedding_provider_id:
                raise ValueError("请先选择 MaiBot Embedding 任务")
            return await service.semantic.rebuild()
        elif path == "/webhook/retry":
            await service.db.retry_notifications()
            return {"queued": True}
        else:
            match = re.fullmatch(r"/memes/([1-9][0-9]{0,18})/edit", path)
            if match:
                ident = image_id(match[1])
                if not await service.db.get_image(ident):
                    raise ValueError("目录中没有这张图片")
                if set(body) - {"manual_tags", "excluded_tags", "description", "enabled"}:
                    raise ValueError("存在未知的编辑字段")
                for key in ("manual_tags", "excluded_tags"):
                    if key in body:
                        body[key] = tags(body[key])
                description = body.get("description")
                if description is not None and (
                    not isinstance(description, str) or len(description) > 1000
                ):
                    raise ValueError("人工描述须为不超过 1000 字的字符串或 null")
                if "enabled" in body and not isinstance(body["enabled"], bool):
                    raise ValueError("enabled 须为布尔值")
                await service.db.update_override(ident, body)
                return await service.db.get_image(ident)
        raise ValueError("不支持的管理操作")
