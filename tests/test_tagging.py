import base64
import io
import json
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from backend.models import PROMPT_VERSION, Settings
from backend.tagging import TAG_PROMPT, Tagger, model_image, parse_annotation
from tests.conftest import publish


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(buffer, "PNG")
    return buffer.getvalue()


def gif_bytes(frame_count=4):
    colors = ["red", "green", "blue", "yellow", "purple", "orange", "black"]
    frames = [Image.new("RGB", (20, 20), color) for color in colors[:frame_count]]
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buffer.getvalue()


@pytest.mark.parametrize("frame_count", [1, 2, 3, 4, 7])
def test_gif_montage_is_in_memory(frame_count):
    data_url, sampled_frames = model_image(gif_bytes(frame_count))
    assert sampled_frames == min(frame_count, 4)
    with Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))) as result:
        assert result.size == ((1024, 1024) if frame_count > 1 else (20, 20))
        assert result.format == "JPEG"
        assert result.getpixel((5, 5))[0] > 200
        if 1 < frame_count < 4:
            assert all(channel > 240 for channel in result.getpixel((600, 600)))


@pytest.mark.parametrize("frame_count", [1, 2, 3, 4, 7])
async def test_animation_prompt_matches_actual_preview(db, image, frame_count):
    item = image()
    # Determine animation from decoded bytes, even if the filename is misleading.
    item["imageUrl"] = "https://r2.longhub.top/1.png"
    item["imageKey"] = "1.png"
    await publish(db, [item])
    await db.enqueue_tags([1], provider_id="vision")
    client = AsyncMock()
    client.image_bytes.return_value = gif_bytes(frame_count)
    generate = AsyncMock(return_value='{"description":"带龙脸的角色点头","tags":["龙脸","赞同"]}')
    tagger = Tagger(db, client, Settings(vision_provider_id="vision"), generate)

    assert await tagger.run_once()
    call = generate.call_args.kwargs
    assert call["system_prompt"].startswith(TAG_PROMPT)
    assert call["prompt"] == '图片中已确认的文字（JSON 字符串）："确实"'
    if frame_count == 1:
        assert call["system_prompt"] == TAG_PROMPT
    else:
        assert f"{min(frame_count, 4)} 帧" in call["system_prompt"]
        assert "从左到右、从上到下" in call["system_prompt"]
        assert "不足四帧的位置是补白" in call["system_prompt"]
        assert "对整张动图给出一份描述和标签" in call["system_prompt"]
        assert "不要把预览的" in call["system_prompt"]
    assert (await db.get_image(1))["annotation_fresh"]


@pytest.mark.parametrize(
    "output",
    [
        '{"description":"","tags":["标签"]}',
        '{"description":"描述","tags":[]}',
        '{"description":"描述","tags":"错误"}',
        "not JSON",
    ],
)
def test_invalid_annotations_rejected(output):
    with pytest.raises(ValueError):
        parse_annotation(output)


async def test_tagging_runs_without_image_files(db, image, tmp_path):
    await publish(db, [image()])
    await db.update_override(1, {"manual_tags": ["手工"], "description": "人工描述"})
    await db.enqueue_tags([1], provider_id="vision")
    client = AsyncMock()
    client.image_bytes.return_value = png_bytes()
    generate = AsyncMock(
        return_value=json.dumps(
            {"description": "模型描述", "tags": ["开心", "赞同"]}, ensure_ascii=False
        )
    )
    tagger = Tagger(db, client, Settings(vision_provider_id="vision"), generate)
    assert await tagger.run_once()
    item = await db.get_image(1)
    assert item["annotation_fresh"] and item["description"] == "人工描述"
    assert "手工" in item["effective_tags"] and "开心" in item["effective_tags"]
    assert generate.call_args.kwargs["image_url"].startswith("data:image/jpeg;base64,")
    assert generate.call_args.kwargs["system_prompt"] == TAG_PROMPT
    assert not any(
        p.suffix.lower() in [".jpg", ".png", ".gif", ".webp"] for p in tmp_path.rglob("*")
    )


async def test_pausing_and_duplicate_queue(db, image):
    await publish(db, [image()])
    assert await db.enqueue_tags([1], provider_id="vision") == 1
    assert await db.enqueue_tags([1], force=True, provider_id="vision") == 0
    await db.set_meta("tagging_paused", True)
    assert await db.claim_tag() is None
    await db.set_meta("tagging_paused", False)
    assert await db.claim_tag()


async def test_old_prompt_annotations_can_be_refreshed_without_losing_manual_edits(db, image):
    await publish(db, [image(tag_names=["genshin_impact"])])
    await db.update_override(1, {"manual_tags": ["人工"], "description": "人工描述"})
    await db.conn.execute(
        "INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?)",
        (1, "1.gif", "确实", "vision", PROMPT_VERSION - 1, "旧描述", '["旧标签"]', 0),
    )
    await db.conn.commit()
    item = await db.get_image(1)
    assert not item["annotation_fresh"] and "旧标签" in item["effective_tags"]
    assert await db.missing_annotation_ids() == [1]
    assert (await db.query_sql("SELECT annotation_fresh FROM memes"))["rows"] == [[0]]
    assert await db.enqueue_tags([1], provider_id="vision") == 1
    client = AsyncMock()
    client.image_bytes.return_value = png_bytes()
    generate = AsyncMock(return_value='{"description":"带龙脸的角色","tags":["龙脸","开心"]}')
    tagger = Tagger(db, client, Settings(vision_provider_id="vision"), generate)
    assert await tagger.run_once()
    item = await db.get_image(1)
    assert item["annotation_fresh"] and item["description"] == "人工描述"
    assert set(item["effective_tags"]) == {"genshin_impact", "龙脸", "开心", "人工"}
    assert (await db.query_sql("SELECT annotation_fresh FROM memes"))["rows"] == [[1]]


async def test_metadata_change_during_model_call_requeues(db, image):
    await publish(db, [image()])
    await db.enqueue_tags([1], provider_id="vision")
    client = AsyncMock()
    client.image_bytes.return_value = png_bytes()

    async def generate(**kwargs):
        await publish(db, [image(text="新文字")])
        return '{"description":"已过期的输出","tags":["旧标签"]}'

    tagger = Tagger(db, client, Settings(vision_provider_id="vision"), generate)
    await tagger.run_once()
    item = await db.get_image(1)
    assert not item["ai_tags"] and item["tag_job"]["status"] == "queued"


async def test_failure_count_and_manual_retry(db, image):
    await publish(db, [image()])
    await db.enqueue_tags([1], provider_id="vision")
    for _ in range(3):
        job = await db.claim_tag()
        await db.fail_tag(job, "model unavailable")
        async with db.lock:
            await db.conn.execute("UPDATE tag_jobs SET next_attempt=0")
            await db.conn.commit()
    item = await db.get_image(1)
    assert item["tag_job"]["status"] == "failed"
    assert not await db.enqueue_tags([1], provider_id="vision")
    assert await db.enqueue_tags([1], force=True, provider_id="vision") == 1
