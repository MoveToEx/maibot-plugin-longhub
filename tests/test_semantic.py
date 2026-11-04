import copy
import math
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.embeddings import Embeddings
from backend.errors import SemanticError
from backend.models import Settings
from backend.semantic import SemanticIndex
from backend.vector_store import SQLiteVectorStore
from tests.conftest import publish


class Provider:
    def __init__(self):
        self.model = "test-embedding"
        self.dimension = 3
        self.batch_calls = []
        self.query_calls = []
        self.on_batch = self.on_query = None

    def vector(self, text):
        if "开心" in text or "happy" in text:
            return [3.0, 0.0, 0.0]
        if "难过" in text or "sad" in text:
            return [0.0, 2.0, 0.0]
        return [0.0, 0.0, 4.0]

    async def embed(self, text):
        self.query_calls.append(text)
        if self.on_query:
            await self.on_query()
        return self.vector(text)

    async def embed_many(self, texts):
        self.batch_calls.append(list(texts))
        if self.on_batch:
            await self.on_batch()
        return [self.vector(text) for text in texts]


class Factory:
    def __init__(self):
        self.data = {}
        self.fail_load = self.fail_insert = False

    def __call__(self, directory, dimension):
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

            async def search_many(self, vectors, count):
                return [await self.search(vector, count) for vector in vectors]

            async def search(self, vector, count):
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
        await engine.search(["开心", "happy"])
    assert not provider.query_calls and len(provider.batch_calls) == 1
    await annotate(db, 3, "惊讶")
    assert not (await engine.status())["available"]  # New annotation still needs a vector.
    await engine.run_once()
    assert (await engine.status())["available"]
    result = await engine.search(["开心", "happy"], recent=["1"])
    assert result[0]["id"] == "1" and result[0]["similarity"] == 1
    assert result[0]["recently_sent"] and "image_url" not in result[0]
    assert len(provider.batch_calls) == 3 and provider.batch_calls[-1] == ["开心", "happy"]
    assert not provider.query_calls
    await engine.close()


async def test_keyword_union_keeps_highest_score_then_limits_globally(db, image, tmp_path):
    engine, provider, _ = await setup(db, image, tmp_path)
    await engine.run_once()

    def hit(ident, score):
        return {
            "image_id": ident,
            "fingerprint": engine.entries[ident],
            "similarity": score,
        }

    engine.store.search_many = AsyncMock(
        return_value=[
            [hit("3", 0.95), hit("1", 0.91003)],
            [hit("2", 0.91004), hit("1", 0.80)],
        ]
    )
    result = await engine.search(["开心", "难过"], limit=3, recent=["2"])
    assert [item["id"] for item in result] == ["3", "2", "1"]
    assert [item["similarity"] for item in result] == [0.95, 0.91, 0.91]
    assert result[1]["recently_sent"] and not result[0]["recently_sent"]
    assert provider.batch_calls[-1] == ["开心", "难过"] and not provider.query_calls
    assert engine.store.search_many.await_args.args[0] == [[1.0, 0, 0], [0, 1.0, 0]]
    limited = await engine.search(["开心", "难过"], limit=2)
    assert [item["id"] for item in limited] == ["3", "2"]
    await engine.close()


async def test_keyword_whitespace_and_duplicates_do_not_repeat_embedding(db, image, tmp_path):
    engine, provider, _ = await setup(db, image, tmp_path)
    await engine.run_once()
    result = await engine.search([" 开心 ", "难过", "开心"])
    assert provider.batch_calls[-1] == ["开心", "难过"]
    # Exact score ties are deterministic and independent of keyword order.
    assert [item["id"] for item in result] == ["1", "2", "3"]
    reverse = await engine.search(["难过", "开心"])
    assert reverse == result
    await engine.close()


@pytest.mark.parametrize(
    "keywords",
    [
        "想找一张表达开心心情的龙图",
        None,
        {},
        [],
        ["开心"],
        ["1", "2", "3", "4", "5", "6", "7"],
        ["开心", ""],
        ["开心", "   "],
        ["开心", 1],
        ["开心", True],
        ["开心", "长" * 41],
        ["开心", " 开心 "],
    ],
)
async def test_invalid_keyword_arrays_are_rejected_before_embedding(db, image, tmp_path, keywords):
    engine, provider, _ = await setup(db, image, tmp_path)
    await engine.run_once()
    before = len(provider.batch_calls)
    with pytest.raises(SemanticError):
        await engine.search(keywords)
    assert len(provider.batch_calls) == before and not provider.query_calls
    await engine.close()


async def test_keyword_union_discards_invalid_and_stale_scores(db, image, tmp_path):
    engine, _, _ = await setup(db, image, tmp_path)
    await engine.run_once()
    engine.store.search_many = AsyncMock(
        return_value=[
            [
                {"image_id": "1", "fingerprint": engine.entries["1"], "similarity": math.nan},
                {"image_id": "2", "fingerprint": "obsolete", "similarity": 1.0},
            ],
            [
                {"image_id": "1", "fingerprint": engine.entries["1"], "similarity": True},
                {"image_id": "3", "fingerprint": engine.entries["3"], "similarity": math.inf},
            ],
        ]
    )
    assert await engine.search(["开心", "难过"]) == []
    await engine.close()


async def test_sqlite_multi_query_returns_each_keywords_candidates_in_one_scan(tmp_path):
    store = SQLiteVectorStore(tmp_path / "vectors", 3)
    await store.initialize()
    try:
        documents = [{"id": str(i), "fingerprint": str(i) * 64} for i in range(1, 5)]
        await store.insert(documents, [[1, 0, 0], [0, 1, 0], [0.8, 0.6, 0], [0.6, 0.8, 0]])
        store._decode = Mock(wraps=store._decode)
        groups = await store.search_many([[1, 0, 0], [0, 1, 0]], 2)
        assert [[hit["image_id"] for hit in group] for group in groups] == [["1", "3"], ["2", "4"]]
        assert store._decode.call_count == 4
        assert groups[0][0]["similarity"] == groups[1][0]["similarity"] == 1.0
        assert groups[0][1]["similarity"] == pytest.approx(0.8)
        assert (await store.search("开心", [1, 0, 0], 1))[0]["image_id"] == "1"
        with pytest.raises(SemanticError, match="维度"):
            await store.search_many([[1, 0, 0], [1, 0]], 2)
    finally:
        await store.close()


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
    assert all(item["id"] != "1" for item in await engine.search(["开心", "happy"]))
    original_calls = len(provider.batch_calls)
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

    provider.on_batch = disable
    with pytest.raises(SemanticError, match="更新"):
        await engine.search(["开心", "happy"])
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
    provider.embed_many = AsyncMock(return_value=[[1, 0, 0]])
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
    assert (await engine.search(["开心", "happy"]))[0]["id"] == "1"
    calls = len(provider.batch_calls)
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


async def test_provider_exception_bodies_are_not_exposed():
    provider = Provider()
    provider.embed = AsyncMock(side_effect=RuntimeError("sk-private base64-payload"))
    with pytest.raises(SemanticError) as raised:
        await Embeddings(provider, 30).embed("概念")
    assert "sk-private" not in str(raised.value) and "base64" not in str(raised.value)
