import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from backend.orm import TagJob
from tests.conftest import publish
from tests.test_maibot import mai as mai


async def test_queue_preview_is_current_ordered_limited_and_preserves_large_ids(db, image):
    ident = 2**63 - 1
    await publish(db, [image(i) for i in range(1, 8)] + [image(ident)])
    ordered = [ident, 1, 2, 3, 4, 5, 6, 7]
    await db.enqueue_tags(ordered)
    # Windows clocks can give several enqueues the same timestamp.
    async with db.transaction() as session:
        for index, image_id in enumerate(ordered):
            await session.execute(
                update(TagJob).where(TagJob.image_id == image_id).values(updated=index)
            )
    assert (await db.claim_tag())["image_id"] == ident
    generation = await db.get_meta("active_generation")
    await db.refresh(generation, 1, None)
    async with db.transaction() as session:
        await session.execute(
            update(TagJob).where(TagJob.image_id == 2).values(next_attempt=9999999999)
        )
    snapshot = await db.new_generation()
    await db.stage_batch(snapshot, [image(99)])
    queue = (await db.stats())["tag_queue"]
    assert [job["image_id"] for job in queue] == [str(ident), "3", "4", "5", "6"]
    assert queue[0]["status"] == "running"
    assert all(isinstance(job["image_id"], str) for job in queue)


@pytest.mark.parametrize(
    ("path", "key"), [("/tagging/auto", "enabled"), ("/semantic/pause", "paused")]
)
async def test_task_control_apis_validate_boolean_and_forward_state(mai, path, key):
    plugin, _ = mai
    target = AsyncMock(return_value={key: True})
    plugin.service.set_auto_tagging = target
    plugin.service.semantic = SimpleNamespace(set_paused=target)
    for bad in (None, "true", 1):
        with pytest.raises(ValueError, match="布尔值"):
            await plugin.panel_write(path, json.dumps({key: bad}))
    target.assert_not_awaited()
    assert await plugin.panel_write(path, json.dumps({key: True})) == {key: True}
    target.assert_awaited_once_with(True)
