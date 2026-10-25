import pytest
import pytest_asyncio
from aiohttp import web

from backend.database import Database


def picture(ident=1, *, text="确实", rating="none", tag_names=None):
    return {
        "id": ident,
        "text": text,
        "rating": rating,
        "imageUrl": f"https://r2.longhub.top/{ident}.gif",
        "imageKey": f"{ident}.gif",
        "createdAt": "2026-10-25T00:00:00Z",
        "currentVersionId": ident,
        "version": 1,
        "userIdentifier": {"id": 1, "username": "test"},
        "tags": [
            {"id": i + 1, "name": name} for i, name in enumerate(tag_names or ["赞同", "approving"])
        ],
        "deletion": None,
    }


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path / "catalog.sqlite3")
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def image():
    return picture


@pytest_asyncio.fixture
async def serve():
    runners = []

    async def start(app):
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    yield start
    for runner in runners:
        await runner.cleanup()


async def publish(db, items):
    generation = await db.new_generation()
    await db.stage_batch(generation, items)
    await db.publish(generation, {"export_id": generation, "record_count": len(items)}, [])
    return generation
