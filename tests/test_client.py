import gzip
import hashlib
import json

import pytest
from aiohttp import web

from backend.client import ExportValidator, HubClient, HubError
from backend.models import Settings


def export_body(items):
    lines = [
        json.dumps({"type": "image", "data": x}, ensure_ascii=False).encode() + b"\n" for x in items
    ]
    return (
        b'{"type":"manifest","schemaVersion":1,"exportId":"export-1"}\n'
        + b"".join(lines)
        + json.dumps(
            {
                "type": "end",
                "exportId": "export-1",
                "recordCount": len(lines),
                "sha256": hashlib.sha256(b"".join(lines)).hexdigest(),
            }
        ).encode()
        + b"\n"
    )


def test_export_exact_bytes_and_int64(image):
    raw = export_body([image(2**63 - 1, text="文字\n包含换行")])
    validator = ExportValidator()
    records = [item for line in raw.splitlines(keepends=True) if (item := validator.feed(line))]
    assert records[0]["id"] == 2**63 - 1
    assert validator.finish() == {"export_id": "export-1", "record_count": 1}


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_end",
        "checksum",
        "count",
        "export_id",
        "schema",
        "after_end",
        "lf",
        "duplicate",
        "unordered",
    ],
)
def test_export_rejects_invalid_streams(image, mutation):
    raw = export_body([image(1), image(2)])
    if mutation == "missing_end":
        raw = b"\n".join(raw.splitlines()[:-1]) + b"\n"
    elif mutation == "checksum":
        raw = raw.replace(b'"sha256": "', b'"sha256": "bad')
    elif mutation == "count":
        raw = raw.replace(b'"recordCount": 2', b'"recordCount": 3')
    elif mutation == "export_id":
        raw = raw.replace(b'"exportId": "export-1"', b'"exportId": "other"')
    elif mutation == "schema":
        raw = raw.replace(b'"schemaVersion":1', b'"schemaVersion":2')
    elif mutation == "after_end":
        raw += b"{}\n"
    elif mutation == "lf":
        raw = raw[:-1]
    elif mutation == "duplicate":
        raw = export_body([image(1), image(1)])
    elif mutation == "unordered":
        raw = export_body([image(2), image(1)])
    validator = ExportValidator()
    with pytest.raises((ValueError, KeyError)):
        for line in raw.splitlines(keepends=True):
            validator.feed(line)
        validator.finish()


@pytest.mark.parametrize("compressed", [False, True])
async def test_download_stream_and_auth(serve, image, compressed):
    raw = export_body([image(1), image(2)])
    seen = {}

    async def handler(request):
        seen.update(request.headers)
        body = gzip.compress(raw) if compressed else raw
        response = web.StreamResponse(
            headers={
                "Content-Type": "application/x-ndjson",
                **({"Content-Encoding": "gzip"} if compressed else {}),
            }
        )
        await response.prepare(request)
        for offset in range(0, len(body), 7):
            await response.write(body[offset : offset + 7])
        return response

    app = web.Application()
    app.router.add_get("/sync/full", handler)
    base = await serve(app)
    client = HubClient(Settings(api_base=base, app_key="secret-key"))
    await client.open()
    items = []

    async def consume(batch):
        items.extend(batch)

    try:
        summary = await client.export(consume)
        assert summary["record_count"] == 2
        assert [x["id"] for x in items] == [1, 2]
        assert seen["Authorization"] == "Key secret-key"
    finally:
        await client.close()


@pytest.mark.parametrize("corruption", ["truncated", "crc", "trailing"])
async def test_gzip_integrity(serve, image, corruption):
    raw = gzip.compress(export_body([image()]))
    if corruption == "truncated":
        raw = raw[:-5]
    elif corruption == "crc":
        raw = raw[:-8] + bytes([raw[-8] ^ 1]) + raw[-7:]
    else:
        raw += b"extra"

    async def handler(request):
        return web.Response(
            body=raw, headers={"Content-Type": "application/x-ndjson", "Content-Encoding": "gzip"}
        )

    app = web.Application()
    app.router.add_get("/sync/full", handler)
    client = HubClient(Settings(api_base=await serve(app)))
    await client.open()

    async def consume(batch):
        pass

    try:
        with pytest.raises(Exception):
            await client.export(consume)
    finally:
        await client.close()


async def test_429_and_deletion_semantics(serve, image):
    async def full(request):
        return web.Response(status=429, headers={"Retry-After": "5"})

    async def get(request):
        ident = int(request.match_info["id"])
        if ident == 4:
            return web.Response(status=404)
        if ident == 5:
            return web.Response(status=500)
        item = image(ident)
        if ident == 2:
            item["deletion"] = {"reason": "removed"}
        if ident == 3:
            item["currentVersionId"] = None
        return web.json_response({"error": None, "data": item})

    app = web.Application()
    app.router.add_get("/sync/full", full)
    app.router.add_get("/image/{id}", get)
    client = HubClient(Settings(api_base=await serve(app)))
    await client.open()

    async def consume(batch):
        pass

    try:
        with pytest.raises(HubError) as raised:
            await client.export(consume)
        assert raised.value.retry_after == 5
        assert await client.get_image(1)
        for ident in [2, 3, 4]:
            assert await client.get_image(ident) is None
        with pytest.raises(HubError):
            await client.get_image(5)
    finally:
        await client.close()


async def test_metadata_query_handles_fragmented_json(serve, image):
    async def handler(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        raw = json.dumps({"error": None, "data": image()}).encode()
        for offset in range(0, len(raw), 13):
            await response.write(raw[offset : offset + 13])
            import asyncio

            await asyncio.sleep(0)
        return response

    app = web.Application()
    app.router.add_get("/image/1", handler)
    client = HubClient(Settings(api_base=await serve(app)))
    await client.open()
    try:
        assert (await client.get_image(1))["id"] == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://r2.longhub.top/a",
        "https://evil.test/a",
        "https://r2.longhub.top.evil.test/a",
        "https://user@r2.longhub.top/a",
        "https://r2.longhub.top:8080/a",
    ],
)
def test_image_url_allowlist(url):
    with pytest.raises(ValueError):
        HubClient(Settings()).validate_image_url(url)


@pytest.mark.parametrize("empty", [False, True])
async def test_health_reads_one_metadata_page_and_uses_current_auth(serve, image, empty):
    seen = []

    async def handler(request):
        seen.append((request.path, dict(request.query), dict(request.headers)))
        return web.json_response(
            {
                "error": None,
                "data": {"total": 0 if empty else 1, "images": [] if empty else [image()]},
            }
        )

    app = web.Application()
    app.router.add_get("/image", handler)
    client = HubClient(Settings(api_base=await serve(app), app_key="health-secret"))
    await client.open()
    try:
        await client.healthcheck()
        assert len(seen) == 1 and seen[0][0] == "/image"
        assert seen[0][1] == {"limit": "1", "offset": "0"}
        assert seen[0][2]["Authorization"] == "Key health-secret"
        assert seen[0][2]["Accept-Encoding"] == "identity"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"error": "upstream error", "data": {"total": 1, "images": []}},
        {"error": None, "data": []},
        {"error": None, "data": {"images": []}},
        {"error": None, "data": {"total": True, "images": []}},
        {"error": None, "data": {"total": -1, "images": []}},
        {"error": None, "data": {"total": 0, "images": {}}},
        {"error": None, "data": {"total": 1, "images": ["invalid metadata"]}},
    ],
)
async def test_health_rejects_invalid_image_list_envelopes(serve, payload):
    app = web.Application()

    async def handler(request):
        return web.json_response(payload)

    app.router.add_get("/image", handler)
    client = HubClient(Settings(api_base=await serve(app)))
    await client.open()
    try:
        with pytest.raises(ValueError):
            await client.healthcheck()
    finally:
        await client.close()


@pytest.mark.parametrize("failure", ["http", "redirect", "html", "invalid_json", "oversize"])
async def test_health_rejects_error_pages_redirects_and_oversized_responses(serve, failure):
    followed = []

    async def handler(request):
        if failure == "http":
            return web.Response(status=503, text="private upstream response health-secret")
        if failure == "redirect":
            return web.Response(status=302, headers={"Location": "/other"})
        if failure == "html":
            return web.Response(text="<html>not an API response</html>", content_type="text/html")
        if failure == "invalid_json":
            return web.Response(text="not-json", content_type="application/json")
        return web.Response(body=b" " * (256 * 1024 + 1), content_type="application/json")

    async def other(request):
        followed.append(True)
        return web.json_response({"error": None, "data": {"total": 0, "images": []}})

    app = web.Application()
    app.router.add_get("/image", handler)
    app.router.add_get("/other", other)
    client = HubClient(Settings(api_base=await serve(app)))
    await client.open()
    try:
        with pytest.raises((HubError, ValueError)) as raised:
            await client.healthcheck()
        assert not followed and "health-secret" not in str(raised.value)
        if failure == "http":
            assert raised.value.status == 503
    finally:
        await client.close()
