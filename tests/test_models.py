import json
from dataclasses import asdict
from pathlib import Path

import pytest

from backend.models import Settings, image_id, normalize_image


def test_schema_defaults_match_settings():
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    assert {k: v["default"] for k, v in schema.items()} == asdict(Settings())


def test_secrets_redacted():
    settings = Settings(app_key="my-private-key", webhook_secret="private-secret")
    public = json.dumps(settings.public())
    assert "my-private-key" not in public and "private-secret" not in public
    assert settings.redact("my-private-key private-secret") == "[REDACTED] [REDACTED]"


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "01", "1.0", str(2**63)])
def test_invalid_ids(value):
    with pytest.raises(ValueError):
        image_id(value)


@pytest.mark.parametrize(
    "values",
    [
        {"webhook_port": 0},
        {"tagging_concurrency": True},
        {"allowed_ratings": ["unknown"]},
        {"image_hosts": ["https://evil.test"]},
        {"api_base": "https://user:secret@example.com"},
        {"auto_tagging": "true"},
    ],
)
def test_invalid_settings(values):
    with pytest.raises(ValueError):
        Settings.from_dict(values)


def test_missing_metadata_does_not_count_as_deleted():
    with pytest.raises(ValueError):
        normalize_image({"id": 1})
