from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

RATINGS = ("none", "moderate", "violent")
PROMPT_VERSION = 1
MAX_ID = 2**63 - 1


def image_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("图片 ID 必须是正整数")
    if isinstance(value, str) and not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise ValueError("图片 ID 无效")
    number = int(value)
    if not 1 <= number <= MAX_ID:
        raise ValueError("图片 ID 超出 int64 范围")
    return number


def tags(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("标签须为数组，最多 100 项")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 80:
            raise ValueError("每个标签须为 1–80 个字符")
        item = item.strip()
        if item not in result:
            result.append(item)
    return result


def normalize_image(data: dict) -> dict | None:
    if not isinstance(data, dict):
        raise ValueError("图片元数据须为对象")
    ident = image_id(data.get("id"))
    if "currentVersionId" not in data:
        raise ValueError("缺少 currentVersionId，不能据此删除本地条目")
    if data.get("deletion") is not None or data.get("currentVersionId") is None:
        return None
    if data.get("rating") not in RATINGS:
        raise ValueError("未知的图片分级")
    for key in ("text", "imageUrl", "imageKey"):
        if not isinstance(data.get(key), str):
            raise ValueError(f"缺少有效的 {key}")
    source_tags = data.get("tags", [])
    if not isinstance(source_tags, list):
        raise ValueError("上游标签格式错误")
    if any(not isinstance(t, dict) or not isinstance(t.get("name"), str) for t in source_tags):
        raise ValueError("上游标签缺少名称")
    return {
        "id": ident,
        "text": data["text"],
        "rating": data["rating"],
        "image_url": data["imageUrl"],
        "image_key": data["imageKey"],
        "hub_tags": list(dict.fromkeys(t["name"] for t in source_tags)),
        "raw": data,
    }


def tokenize(text: str) -> str:
    """CJK overlapping bigrams and Latin words, usable by stock SQLite FTS5."""
    result = []
    for part in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", part) and len(part) > 1:
            result.extend(part[i : i + 2] for i in range(len(part) - 1))
        else:
            result.append(part)
    return " ".join(dict.fromkeys(result))


@dataclass(frozen=True)
class Settings:
    api_base: str = "https://api.longhub.top"
    app_key: str = ""
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8791
    webhook_secret: str = ""
    webhook_ready: bool = False
    sync_interval_hours: int = 24
    vision_provider_id: str = ""
    tagging_rpm: int = 6
    tagging_concurrency: int = 1
    auto_tagging: bool = True
    allowed_ratings: list[str] = field(default_factory=lambda: list(RATINGS))
    candidate_limit: int = 5
    max_image_mb: int = 20
    image_hosts: list[str] = field(default_factory=lambda: ["r2.longhub.top"])
    webhook_log_days: int = 30
    webhook_log_limit: int = 10000

    @classmethod
    def from_dict(cls, values: dict) -> Settings:
        defaults = asdict(cls())
        defaults.update({k: v for k, v in values.items() if k in defaults})
        for key, bounds in {
            "webhook_port": (1, 65535),
            "sync_interval_hours": (1, 720),
            "tagging_rpm": (1, 600),
            "tagging_concurrency": (1, 8),
            "candidate_limit": (1, 10),
            "max_image_mb": (1, 100),
            "webhook_log_days": (1, 365),
            "webhook_log_limit": (100, 100000),
        }.items():
            value = defaults[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not bounds[0] <= value <= bounds[1]
            ):
                raise ValueError(f"{key} 必须是 {bounds[0]}–{bounds[1]} 的整数")
        for key in ("webhook_ready", "auto_tagging"):
            if not isinstance(defaults[key], bool):
                raise ValueError(f"{key} 必须是布尔值")
        for key in (
            "api_base",
            "app_key",
            "webhook_host",
            "webhook_secret",
            "vision_provider_id",
        ):
            if not isinstance(defaults[key], str):
                raise ValueError(f"{key} 必须是字符串")
            defaults[key] = defaults[key].strip()
        base = urlsplit(defaults["api_base"])
        if (
            base.scheme not in ("https", "http")
            or not base.hostname
            or base.query
            or base.fragment
            or base.username
        ):
            raise ValueError("API 地址必须为有效 HTTP(S) 基础地址")
        defaults["api_base"] = defaults["api_base"].rstrip("/")
        ratings = defaults["allowed_ratings"]
        if not isinstance(ratings, list) or any(x not in RATINGS for x in ratings):
            raise ValueError("分级只能包含 none、moderate、violent")
        hosts = defaults["image_hosts"]
        if (
            not isinstance(hosts, list)
            or not hosts
            or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", x) for x in hosts)
        ):
            raise ValueError("图片域名白名单须为非空域名数组")
        defaults["image_hosts"] = [x.lower() for x in hosts]
        return cls(**defaults)

    @property
    def ready(self) -> bool:
        return bool(self.app_key and self.webhook_secret and self.webhook_ready)

    def public(self) -> dict:
        data = asdict(self)
        for key in ("app_key", "webhook_secret"):
            data[key + "_configured"] = bool(data.pop(key))
        return data

    def redact(self, value: object) -> str:
        result = str(value)
        for secret in (self.app_key, self.webhook_secret):
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result[:2000]
