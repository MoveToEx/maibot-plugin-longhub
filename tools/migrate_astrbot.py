"""Copy an AstrBot 0.5.x catalog into a NEW MaiBot plugin data directory.

Run from the plugin directory: uv run python -m tools.migrate_astrbot --help
Only metadata is copied. The source remains unchanged; images and FAISS caches
are never copied. Stop both plugins before switching the webhook subscription.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from backend.models import Settings


def migrate(source: Path, destination: Path, config: Path | None = None):
    source, destination = source.resolve(), destination.resolve()
    database = source / "catalog.sqlite3"
    if not database.is_file():
        raise ValueError("来源目录中没有 catalog.sqlite3")
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError("目标必须是尚不存在、且位于来源目录之外的目录，避免覆盖现有数据")
    values = json.loads(config.read_text(encoding="utf-8-sig")) if config else {}
    settings = Settings.from_dict(values)
    # Host provider IDs cannot be translated across frameworks. Opt in after setup.
    values = asdict(settings) | {
        "vision_provider_id": "",
        "embedding_provider_id": "",
        "webhook_ready": False,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.import-{uuid.uuid4().hex}")
    staging.mkdir(mode=0o700)
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as original:
            if original.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("请先用 AstrBot 0.5.x 打开并升级来源数据库（需要 schema v2）")
            with closing(sqlite3.connect(staging / "catalog.sqlite3")) as copied:
                original.backup(copied)
                if copied.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("来源数据库完整性检查未通过")
        os.chmod(staging / "catalog.sqlite3", 0o600)
        with (staging / "settings.json").open("x", encoding="utf-8") as output:
            json.dump(values, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.chmod(staging / "settings.json", 0o600)
        # os.rename refuses to replace an existing directory on Windows. Check on
        # other hosts too; an existing destination is never part of this import.
        if destination.exists():
            raise ValueError("导入期间目标目录被创建，已停止")
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--astrbot-data",
        type=Path,
        required=True,
        help="包含 catalog.sqlite3 的 AstrBot 插件数据目录",
    )
    parser.add_argument(
        "--data-dir", type=Path, required=True, help="MaiBot 插件数据目录（必须尚不存在）"
    )
    parser.add_argument(
        "--config", type=Path, help="可选：AstrBot 插件的配置 JSON；密钥保存在本地，不输出"
    )
    args = parser.parse_args()
    print(migrate(args.astrbot_data, args.data_dir, args.config))
    print(
        "已复制目录、标注和日志；请在 MaiBot 中重新选择模型、核对 webhook 并启用订阅。向量索引会重新生成。"
    )


if __name__ == "__main__":
    main()
