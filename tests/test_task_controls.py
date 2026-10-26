from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import publish
from tests.test_plugin import plugin as plugin
from tests.test_plugin import plugin_module as plugin_module


async def test_queue_preview_is_current_ordered_limited_and_preserves_large_ids(db, image):
    ident = 2**63 - 1
    await publish(db, [image(i) for i in range(1, 8)] + [image(ident)])
    await db.enqueue_tags([ident, 1, 2, 3, 4, 5, 6, 7])
    assert (await db.claim_tag())["image_id"] == ident
    generation = await db.get_meta("active_generation")
    await db.refresh(generation, 1, None)
    await db.conn.execute("UPDATE tag_jobs SET next_attempt=9999999999 WHERE image_id=2")
    await db.conn.commit()
    snapshot = await db.new_generation()
    await db.stage_batch(snapshot, [image(99)])
    queue = (await db.stats())["tag_queue"]
    assert [job["image_id"] for job in queue] == [str(ident), "3", "4", "5", "6"]
    assert queue[0]["status"] == "running"
    assert all(isinstance(job["image_id"], str) for job in queue)


@pytest.mark.parametrize(
    ("method", "key"), [("api_auto_tagging", "enabled"), ("api_pause_semantic", "paused")]
)
async def test_task_control_apis_validate_boolean_and_forward_state(
    plugin, plugin_module, method, key
):
    target = AsyncMock(return_value={key: True})
    plugin.service.set_auto_tagging = target
    plugin.service.semantic = SimpleNamespace(set_paused=target)
    for bad in (None, "true", 1):
        plugin_module.test_request.json.return_value = {key: bad}
        with pytest.raises(ValueError, match="布尔值"):
            await getattr(plugin, method)()
    target.assert_not_awaited()
    plugin_module.test_request.json.return_value = {key: True}
    assert await getattr(plugin, method)() == {key: True}
    target.assert_awaited_once_with(True)
