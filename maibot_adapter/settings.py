"""Plugin-owned settings, separate from MaiBot's enable/version controls."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from maibot_sdk import Field, PluginConfigBase

from ..backend.models import Settings


class PluginSection(PluginConfigBase):
    enabled: bool = Field(default=False, description="启用 LONG Hub 表情目录")
    config_version: Literal["1.0.0"] = "1.0.0"


class PluginConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)


class SettingsFile:
    def __init__(self, root: Path):
        self.path = root / "settings.json"

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        return Settings.from_dict(json.loads(self.path.read_text(encoding="utf-8-sig")))

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(asdict(settings), output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)
