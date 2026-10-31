"""Credential-safe model and indexing diagnostics."""

from __future__ import annotations

import traceback


class SemanticError(ValueError):
    """A safe, user-facing error without provider response bodies or keys."""


def safe_error(exc):
    if isinstance(exc, SemanticError):
        return str(exc)
    status = getattr(exc, "status_code", None)
    detail = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
    return f"语义索引或 Embedding 调用失败（{detail}），请检查宿主嵌入模型配置"


def error_details(exc, *, provider=None, settings=None):
    """Retain diagnostic exception chains without dumping provider configuration or locals."""
    secrets = set()

    def collect(value, sensitive=False):
        if isinstance(value, dict):
            for key, child in value.items():
                name = str(key).lower().replace("-", "_")
                collect(
                    child,
                    sensitive
                    or name
                    in {
                        "key",
                        "keys",
                        "api_key",
                        "api_keys",
                        "apikey",
                        "secret",
                        "token",
                        "access_token",
                        "api_token",
                        "password",
                        "authorization",
                    },
                )
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child, sensitive)
        elif sensitive and isinstance(value, str) and value:
            secrets.add(value)
            scheme, _, credential = value.partition(" ")
            if scheme.lower() in {"bearer", "basic", "key"} and credential:
                secrets.add(credential)

    collect(getattr(provider, "provider_config", {}))
    for name in ("app_key", "webhook_secret"):
        collect(getattr(settings, name, ""), True)
    details = "".join(traceback.format_exception(exc))
    for secret in sorted(secrets, key=len, reverse=True):
        details = details.replace(secret, "[REDACTED]")
    return details if len(details) <= 16000 else details[:16000] + "\n[错误详情已截断]"
