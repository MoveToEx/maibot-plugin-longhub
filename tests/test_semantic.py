import copy
import logging
import math
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import Settings
from backend.semantic import SemanticIndex
from backend.vector_store import Embeddings, SemanticError
from tests.conftest import publish
from tests.test_plugin import Event
from tests.test_plugin import plugin as plugin
from tests.test_plugin import plugin_module as plugin_module


class Provider:
    def __init__(self):
        self.model = "test-embedding"
        self.provider_config = {"api_base": "https://embedding.example.test", "key": "sk-private"}
        self.batch_calls = []
        self.query_calls = []
        self.on_batch = self.on_query = None

    def get_dim(self):
        return 3

    def get_model(self):
        return self.model

    def vector(self, text):
        if "开心" in text or "happy" in text:
            return [3.0, 0.0, 0.0]
        if "难过" in text or "sad" in text:
            return [0.0, 2.0, 0.0]
        return [0.0, 0.0, 4.0]

    async def get_embedding(self, text):
        self.query_calls.append(text)
        if self.on_query:
            await self.on_query()
        return self.vector(text)

    async def get_embeddings_batch(self, texts, **kwargs):
        self.batch_calls.append(list(texts))
        if self.on_batch:
            await self.on_batch()
        return [self.vector(text) for text in texts]


class Factory:
    def __init__(self):
        self.data = {}
        self.fail_load = self.fail_insert = False

    def __call__(self, directory, embeddings):
        directory.mkdir(parents=True, exist_ok=True)
        rows = self.data.setdefault(str(directory), {})
        factory = self

        class Store:
            async def initialize(self):
                if factory.fail_load:
                    factory.fail_load = False
                    raise OSError("corrupt cache")
                return {ident: row[0]["fingerprint"] for ident, row in rows.items()}

            async def insert(self, documents, vectors):
                for doc, vector in zip(documents, vectors, strict=True):
                    rows[doc["id"]] = (copy.deepcopy(doc), list(vector))
                if factory.fail_insert:
                    factory.fail_insert = False
                    raise OSError("partial native write")

            async def delete(self, ident):
                rows.pop(ident, None)

            async def search(self, concept, vector, count):
                scored = [
                    {
                        "image_id": ident,
                        "fingerprint": doc["fingerprint"],
                        "similarity": sum(x * y for x, y in zip(vector, value, strict=True)),
                    }
                    for ident, (doc, value) in rows.items()
                ]
                return sorted(scored, key=lambda item: item["similarity"], reverse=True)[:count]

            async def close(self):
                pass

        return Store()


async def annotate(db, ident, description):
    await db.enqueue_tags([ident], force=True, provider_id="vision")
    job = await db.claim_tag()
    assert job["image_id"] == ident
    await db.finish_tag(
        job,
        await db.get_image(ident),
        {"description": description, "tags": [description]},
        "vision",
    )


async def setup(db, image, tmp_path, count=4, annotated=3):
    await publish(db, [image(i) for i in range(1, count + 1)])
    for ident in range(1, annotated + 1):
        await annotate(db, ident, "开心" if ident == 1 else "难过" if ident == 2 else "惊讶")
    provider, factory = Provider(), Factory()
    engine = SemanticIndex(
        db,
        tmp_path,
        Settings(embedding_provider_id="embedding", embedding_rpm=600),
        lambda ident: provider,
        factory,
    )
    return engine, provider, factory


async def test_strict_coverage_gate_and_semantic_best_match(db, image, tmp_path):
    engine, provider, _ = await setup(db, image, tmp_path, annotated=2)
    await engine.run_once()
    status = await engine.status()
    assert status["coverage"] == 0.5 and not status["available"]
    with pytest.raises(SemanticError):
        await engine.search("开心的回应")
    assert not provider.query_calls
    await annotate(db, 3, "惊讶")
    assert not (await engine.status())["available"]  # New annotation still needs a vector.
    await engine.run_once()
    assert (await engine.status())["available"]
    result = await engine.search("开心的回应", recent=["1"])
    assert result[0]["id"] == "1" and result[0]["similarity"] == 1
    assert result[0]["recently_sent"] and "image_url" not in result[0]
    assert len(provider.batch_calls) == 2 and len(provider.query_calls) == 1
    await engine.close()


async def test_stale_visual_and_hub_only_do_not_count_but_manual_annotations_do(
    db, image, tmp_path
):
    engine, _, _ = await setup(db, image, tmp_path, annotated=1)
    await engine.run_once()
    generation = await db.get_meta("active_generation")
    await db.refresh(generation, 1, image(1, text="上游文字改变"))
    assert (await engine.status())["annotated"] == 0
    await db.update_override(2, {"description": "人工描述"})
    await db.update_override(3, {"manual_tags": ["可靠人工标签"]})
    await db.update_override(4, {"manual_tags": ["屏蔽"], "excluded_tags": ["屏蔽"]})
    status = await engine.status()
    assert status["annotated"] == 2 and status["total"] == 4
    assert "1" not in engine.desired and "4" not in engine.desired
    await engine.close()


async def test_index_removes_disabled_deleted_and_changed_items_and_ignores_staging(
    db, image, tmp_path
):
    engine, provider, _ = await setup(db, image, tmp_path)
    await engine.run_once()
    original_calls = len(provider.batch_calls)
    # An identical full replacement must reuse every embedding.
    await publish(db, [image(i) for i in range(1, 5)])
    await engine.run_once()
    assert len(provider.batch_calls) == original_calls
    staging = await db.new_generation()
    await db.stage_batch(staging, [image(99)])
    assert (await engine.status())["total"] == 4
    await db.update_override(1, {"enabled": False})
    assert not (await engine.status())["available"]
    await engine.run_once()
    assert "1" not in engine.entries
    assert all(item["id"] != "1" for item in await engine.search("开心"))
    await db.update_override(2, {"description": "人工改成开心"})
    await engine.run_once()
    assert len(provider.batch_calls) == original_calls + 1
    generation = await db.get_meta("active_generation")
    await db.refresh(generation, 3, None)
    await engine.run_once()
    assert "3" not in engine.entries
    await engine.close()


async def test_rating_limits_apply_to_coverage_and_index_before_search(db, image, tmp_path):
    await publish(db, [image(1), image(2, rating="violent"), image(3)])
    for ident in (1, 2):
        await annotate(db, ident, "开心")
    provider = Provider()
    engine = SemanticIndex(
        db,
        tmp_path,
        Settings(embedding_provider_id="e", embedding_rpm=600, allowed_ratings=["none"]),
        lambda _: provider,
        Factory(),
    )
    await engine.run_once()
    status = await engine.status()
    assert status["total"] == 2 and status["annotated"] == 1 and not status["available"]
    assert set(engine.entries) == {"1"}
    await engine.close()


async def test_resume_without_reembedding_and_model_change_creates_new_cache(db, image, tmp_path):
    engine, provider, factory = await setup(db, image, tmp_path)
    await engine.run_once()
    directory = engine.directory
    await engine.close()
    resumed = SemanticIndex(db, tmp_path, engine.settings, lambda _: provider, factory)
    await resumed.run_once()
    assert resumed.directory == directory and len(provider.batch_calls) == 1
    provider.model = "different-model-same-dimension"
    assert not (await resumed.status())["available"]
    (resumed.root / "personal-notes").mkdir()
    await resumed.run_once()
    assert resumed.directory != directory and len(provider.batch_calls) == 2
    assert not directory.exists() and (resumed.root / "personal-notes").is_dir()
    await resumed.close()


async def test_catalog_chart_counts_cover_all_current_images_and_only_current_vectors(
    db, image, tmp_path
):
    engine, provider, _ = await setup(db, image, tmp_path)
    await publish(db, [image(1), image(2), image(3), image(4, rating="violent")])
    engine.settings = replace(engine.settings, allowed_ratings=["none"])
    await db.update_override(1, {"enabled": False})
    await db.update_override(4, {"description": "人工标注"})
    assert (await engine.status())["catalog"] == {
        "total": 4,
        "unannotated": 0,
        "annotated_unindexed": 4,
        "indexed": 0,
    }
    await engine.run_once()
    status = await engine.status()
    assert status["available"] and status["total"] == 2
    assert status["catalog"] == {
        "total": 4,
        "unannotated": 0,
        "annotated_unindexed": 2,
        "indexed": 2,
    }
    generation = await db.get_meta("active_generation")
    await db.refresh(generation, 3, image(3, text="更新后旧标注已过期"))
    await db.refresh(generation, 4, None)
    assert (await engine.status())["catalog"] == {
        "total": 3,
        "unannotated": 1,
        "annotated_unindexed": 1,
        "indexed": 1,
    }
    provider.model = "new-vector-space"
    assert (await engine.status())["catalog"]["indexed"] == 0
    assert (await engine.status())["catalog"]["annotated_unindexed"] == 2
    await engine.close()


async def test_dirty_or_corrupt_native_cache_is_rebuilt(db, image, tmp_path):
    engine, provider, factory = await setup(db, image, tmp_path)
    await engine.run_once()
    directory = engine.directory
    await engine.close()
    cache = await db.get_meta("semantic_cache")
    await db.set_meta("semantic_cache", {**cache, "dirty": True})
    resumed = SemanticIndex(db, tmp_path, engine.settings, lambda _: provider, factory)
    await resumed.run_once()
    assert resumed.directory != directory and (await resumed.status())["available"]
    directory = resumed.directory
    await resumed.close()
    factory.fail_load = True
    again = SemanticIndex(db, tmp_path, engine.settings, lambda _: provider, factory)
    await again.run_once()
    assert again.directory != directory and (await again.status())["available"]
    await again.close()


async def test_partial_write_is_not_exposed_and_recovers(db, image, tmp_path):
    engine, _, factory = await setup(db, image, tmp_path)
    factory.fail_insert = True
    with pytest.raises(OSError):
        await engine.run_once()
    assert (await db.get_meta("semantic_cache"))["dirty"] and not (await engine.status())[
        "available"
    ]
    await engine.run_once()
    assert (await engine.status())["available"]
    await engine.close()


async def test_annotation_change_during_embedding_discards_stale_result(db, image, tmp_path):
    engine, provider, _ = await setup(db, image, tmp_path)

    async def change():
        await db.update_override(1, {"description": "全新人工描述"})

    provider.on_batch = change
    await engine.run_once()
    assert "1" not in engine.entries and not (await engine.status())["available"]
    provider.on_batch = None
    await engine.run_once()
    assert (await engine.status())["available"]
    await engine.close()


async def test_query_rechecks_catalog_after_embedding_request(db, image, tmp_path):
    engine, provider, _ = await setup(db, image, tmp_path)
    await engine.run_once()

    async def disable():
        await db.update_override(1, {"enabled": False})

    provider.on_query = disable
    with pytest.raises(SemanticError, match="更新"):
        await engine.search("开心")
    await engine.close()


async def test_manual_rebuild_and_provider_batch_failure(db, image, tmp_path):
    engine, provider, _ = await setup(db, image, tmp_path)
    await engine.run_once()
    old = engine.directory
    await engine.rebuild()
    assert (
        not (await engine.status())["available"] and (await db.get_meta("semantic_cache"))["dirty"]
    )
    await engine.run_once()
    assert engine.directory != old and (await engine.status())["available"]
    provider.get_embeddings_batch = AsyncMock(return_value=[[1, 0, 0]])
    await engine.rebuild()
    with pytest.raises(SemanticError, match="数量"):
        await engine.run_once()
    assert not engine.entries
    await engine.close()


async def test_embedding_pause_keeps_search_and_persists_reset_until_resume(db, image, tmp_path):
    engine, provider, factory = await setup(db, image, tmp_path)
    await engine.run_once()
    calls = len(provider.batch_calls)
    await engine.set_paused(True)
    assert (await engine.status())["paused"] and (await engine.status())["available"]
    assert (await engine.search("开心"))[0]["id"] == "1"
    await db.update_override(4, {"description": "新标注"})
    await engine.run_once()
    assert len(provider.batch_calls) == calls and (await engine.status())["pending"] == 1
    await engine.rebuild()
    assert (await engine.status())["indexed"] == 0
    await engine.run_once()
    assert not engine.entries and len(provider.batch_calls) == calls
    await engine.close()
    resumed = SemanticIndex(db, tmp_path, engine.settings, lambda _: provider, factory)
    await resumed.run_once()
    assert (await resumed.status())["paused"] and len(provider.batch_calls) == calls
    await resumed.set_paused(False)
    await resumed.run_once()
    assert (await resumed.status())["available"] and len(resumed.entries) == 4
    await resumed.close()


async def test_reset_during_embedding_discards_old_batch(db, image, tmp_path):
    engine, provider, _ = await setup(db, image, tmp_path)
    provider.on_batch = engine.rebuild
    await engine.run_once()
    assert not engine.entries and not (await engine.status())["available"]
    provider.on_batch = None
    await engine.run_once()
    assert len(engine.entries) == 3 and (await engine.status())["available"]
    await engine.close()


@pytest.mark.parametrize(
    "vector", [[0, 0, 0], [1, 2], [math.nan, 0, 1], [True, 0, 1], [math.inf, 0, 1]]
)
def test_invalid_vectors_rejected(vector):
    with pytest.raises(SemanticError):
        Embeddings(Provider(), 30).normalize(vector)


async def test_cached_vectors_do_not_call_provider_or_leak_across_tasks():
    provider = Provider()
    adapter = Embeddings(provider, 600)
    with adapter.precomputed(["x"], [[1, 0, 0]]):
        assert await adapter.get_embedding("x") == [1, 0, 0]
        assert await adapter.get_embeddings_batch(["x"]) == [[1, 0, 0]]
    assert not provider.query_calls and not provider.batch_calls
    result = await adapter.get_embedding("x")
    assert result == [0, 0, 1] and provider.query_calls == ["x"]


async def test_provider_exception_bodies_are_not_exposed():
    provider = Provider()
    provider.get_embedding = AsyncMock(side_effect=RuntimeError("sk-private base64-payload"))
    with pytest.raises(SemanticError) as raised:
        await Embeddings(provider, 30).embed("概念")
    assert "sk-private" not in str(raised.value) and "base64" not in str(raised.value)


@pytest.mark.parametrize("available", [False, True])
async def test_request_tool_gating_and_preference_are_local(plugin, plugin_module, available):
    class ToolSet:
        def __init__(self):
            self.tools = [
                SimpleNamespace(name=name)
                for name in (plugin_module.SEMANTIC_TOOL, "search_longhub_memes")
            ]

        def remove_tool(self, name):
            self.tools = [tool for tool in self.tools if tool.name != name]

    tools = ToolSet()
    plugin.service.semantic = SimpleNamespace(
        status=AsyncMock(return_value={"available": available})
    )
    plugin.service.candidates = AsyncMock(return_value=[])
    req = SimpleNamespace(system_prompt="原人格", func_tool=tools)
    await plugin.provide_candidates(Event(), req)
    exposed = [tool.name for tool in req.func_tool.tools]
    assert (plugin_module.SEMANTIC_TOOL in exposed) is available
    assert plugin_module.SEMANTIC_TOOL in [tool.name for tool in tools.tools]
    if available:
        plugin.service.candidates.assert_not_awaited()
        assert "不要先拆成精确标签" in req.system_prompt
        assert "只有聊天具有有意义的上下文" in req.system_prompt
    else:
        plugin.service.candidates.assert_awaited_once()
    assert "RANDOM()" not in req.system_prompt and "随机" not in req.system_prompt
    assert "每轮回复结束前，必须先调用一次 LONG Hub 搜索工具" in req.system_prompt


async def test_tool_returns_candidates_without_sending_and_embedding_settings_validation(
    plugin, plugin_module
):
    plugin.service.semantic = SimpleNamespace(
        search=AsyncMock(return_value=[{"id": "1", "similarity": 0.9}])
    )
    event = Event()
    assert '"method": "embedding"' in await plugin.search_longhub_memes_semantic(event, "开心")
    assert not event.sent
    with pytest.raises(ValueError, match="Embedding"):
        plugin._embedding_provider("wrong-type")
    provider = Provider()
    plugin.context.get_provider_by_id = lambda _: provider
    assert plugin._embedding_provider("embedding") is provider


async def test_semantic_tool_logs_original_error_chain_and_redacts_secrets(
    plugin, plugin_module, db, image, tmp_path, caplog
):
    engine, provider, _ = await setup(db, image, tmp_path)
    engine.settings = replace(engine.settings, app_key="hub-private", webhook_secret="hook-private")
    provider.provider_config["key"] = ["sk-private", "rotated-private"]
    provider.provider_config["headers"] = {"Authorization": "Bearer header-private"}
    await engine.run_once()
    plugin.service.semantic = engine
    plugin.service.settings = engine.settings

    class EmbeddingRequestError(RuntimeError):
        status_code = 422

    secrets = ["sk-private", "rotated-private", "hub-private", "hook-private", "header-private"]
    provider.get_embedding = AsyncMock(
        side_effect=EmbeddingRequestError("invalid embedding input dimension; " + " ".join(secrets))
    )
    with caplog.at_level(logging.ERROR, logger=plugin_module.logger.name):
        result = await plugin.search_longhub_memes_semantic(Event(), "尴尬的回应")
    assert "语义检索失败" in result and "invalid embedding input dimension" not in result
    records = [record for record in caplog.records if record.name == plugin_module.logger.name]
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    details = records[0].getMessage()
    assert "语义检索失败" in details and "embedding" in details
    assert "Traceback" in details and "EmbeddingRequestError" in details
    assert "HTTP 422" in details and "invalid embedding input dimension" in details
    assert "[REDACTED]" in details
    assert all(secret not in details and secret not in result for secret in secrets)
    await engine.close()
