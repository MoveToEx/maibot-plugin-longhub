from __future__ import annotations

import hashlib
import json
import zlib
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import aiohttp

from .models import Settings, normalize_image


class HubError(RuntimeError):
    def __init__(self, status: int, message: str, retry_after: float = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class ExportValidator:
    """Checksums the exact uncompressed image lines, including their LF bytes."""

    def __init__(self):
        self.manifest = None
        self.complete = False
        self.count = 0
        self.last_id = 0
        self.digest = hashlib.sha256()

    def feed(self, line: bytes) -> dict | None:
        if self.complete or not line.endswith(b"\n"):
            raise ValueError("导出包含不完整行或 end 后的多余数据")
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("导出记录须为对象")
        kind = record.get("type")
        if self.manifest is None:
            if (
                kind != "manifest"
                or type(record.get("schemaVersion")) is not int
                or record["schemaVersion"] != 1
                or not isinstance(record.get("exportId"), str)
                or not record["exportId"]
            ):
                raise ValueError("缺少有效的 schemaVersion=1 manifest")
            self.manifest = record
        elif kind == "image":
            item = normalize_image(record.get("data"))
            if not item or item["id"] <= self.last_id:
                raise ValueError("导出图片无效、重复或未按 ID 升序排列")
            self.last_id = item["id"]
            self.count += 1
            self.digest.update(line)
            return record["data"]
        elif kind == "end":
            if (
                record.get("exportId") != self.manifest["exportId"]
                or type(record.get("recordCount")) is not int
                or record["recordCount"] != self.count
                or record.get("sha256") != self.digest.hexdigest()
            ):
                raise ValueError("导出 ID、记录数或 SHA-256 不匹配")
            self.complete = True
        else:
            raise ValueError("未知的导出记录类型")
        return None

    def finish(self) -> dict:
        if not self.complete:
            raise ValueError("导出被中断：缺少 end 记录")
        return {"export_id": self.manifest["exportId"], "record_count": self.count}


class HubClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session: aiohttp.ClientSession | None = None

    async def open(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=330),
            auto_decompress=False,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            headers={"User-Agent": "astrbot-plugin-longhub/0.4.4"},
        )

    async def close(self):
        if self.session:
            await self.session.close()

    @property
    def auth(self):
        return {"Authorization": "Key " + self.settings.app_key} if self.settings.app_key else {}

    async def _check(self, response):
        if response.status < 400:
            return
        # Error messages may contain untrusted upstream text; never expose credentials.
        try:
            retry = min(3600, max(0, float(response.headers.get("Retry-After", "0"))))
        except ValueError:
            retry = 0
        raise HubError(response.status, f"LONG Hub HTTP {response.status}", retry)

    async def export(self, consume: Callable[[list[dict]], Awaitable[None]]) -> dict:
        async with self.session.get(
            self.settings.api_base + "/sync/full",
            headers={**self.auth, "Accept-Encoding": "gzip"},
            allow_redirects=False,
        ) as response:
            await self._check(response)
            if response.status != 200 or response.content_type != "application/x-ndjson":
                raise ValueError("全量导出响应状态或 Content-Type 无效")
            encoding = response.headers.get("Content-Encoding", "").lower()
            if encoding not in ("", "gzip"):
                raise ValueError("不支持的导出压缩格式")
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
            validator = ExportValidator()
            buffer = b""
            batch = []

            async def process(chunk):
                nonlocal buffer, batch
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if len(line) > 8 * 1024 * 1024:
                        raise ValueError("导出单行过大")
                    item = validator.feed(line + b"\n")
                    if item is not None:
                        batch.append(item)
                    if len(batch) >= 100:
                        await consume(batch)
                        batch = []
                if len(buffer) > 8 * 1024 * 1024:
                    raise ValueError("导出单行过大")

            async for chunk in response.content.iter_chunked(65536):
                if decoder:
                    # Bound each decompression allocation even for a malicious compressed line.
                    incoming = chunk
                    while incoming:
                        decoded = decoder.decompress(incoming, 1024 * 1024)
                        incoming = decoder.unconsumed_tail
                        await process(decoded)
                    if decoder.unused_data:
                        raise ValueError("gzip 尾部存在多余数据")
                else:
                    await process(chunk)
            if decoder:
                await process(decoder.flush())
                if not decoder.eof or decoder.unused_data:
                    raise ValueError("gzip 不完整或包含多余数据")
            if buffer:
                raise ValueError("NDJSON 最后一行缺少 LF")
            summary = validator.finish()
            if batch:
                await consume(batch)
            return summary

    async def get_image(self, ident: int) -> dict | None:
        async with self.session.get(
            self.settings.api_base + f"/image/{ident}",
            headers={**self.auth, "Accept-Encoding": "identity"},
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=False,
        ) as response:
            if response.status == 404:
                return None
            await self._check(response)
            if response.status != 200:
                raise HubError(response.status, "图片查询响应状态无效")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("图片元数据响应过大")
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or envelope.get("error") is not None:
                raise ValueError("图片查询响应格式错误")
            data = envelope.get("data")
            normalized = normalize_image(data)
            if data.get("id") != ident:
                raise ValueError("返回图片 ID 与请求不符")
            return data if normalized is not None else None

    def validate_image_url(self, url: str):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.settings.image_hosts
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("图片 URL 不在 HTTPS 域名白名单内")

    async def image_bytes(self, url: str) -> bytes:
        limit = self.settings.max_image_mb * 1024 * 1024
        for _ in range(4):
            self.validate_image_url(url)
            # No Hub credentials, cookies, or model credentials are forwarded to R2.
            async with self.session.get(
                url,
                headers={"Accept-Encoding": "identity"},
                timeout=aiohttp.ClientTimeout(total=45),
                allow_redirects=False,
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                await self._check(response)
                if response.status != 200:
                    raise ValueError("无法读取图片")
                if response.content_length and response.content_length > limit:
                    raise ValueError("图片超过大小限制")
                if (
                    not response.content_type.startswith("image/")
                    and response.content_type != "application/octet-stream"
                ):
                    raise ValueError("远程响应不是图片")
                content = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    content.extend(chunk)
                    if len(content) > limit:
                        raise ValueError("图片超过大小限制")
                if not content:
                    raise ValueError("图片内容为空")
                return bytes(content)
        raise ValueError("图片重定向次数过多")
