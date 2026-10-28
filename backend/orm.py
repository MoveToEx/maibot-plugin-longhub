"""SQLAlchemy mappings compatible with existing catalog.sqlite3 files."""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import JSON, Boolean, Float, Index, Integer, Text, event
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateTable

from .models import PROMPT_VERSION


class Base(DeclarativeBase):
    pass


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)


class Generation(Base):
    __tablename__ = "generations"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    created: Mapped[float] = mapped_column(Float, default=time.time)
    export_id: Mapped[str | None] = mapped_column(Text)


class CatalogImage(Base):
    __tablename__ = "images"
    generation: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    text: Mapped[str] = mapped_column(Text)
    rating: Mapped[str] = mapped_column(Text)
    image_url: Mapped[str] = mapped_column(Text)
    image_key: Mapped[str] = mapped_column(Text)
    hub_tags: Mapped[list[str]] = mapped_column(JSON)
    raw: Mapped[dict] = mapped_column(JSON)


class Override(Base):
    __tablename__ = "overrides"
    image_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    manual_tags: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    excluded_tags: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")


class Annotation(Base):
    __tablename__ = "annotations"
    image_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    image_key: Mapped[str] = mapped_column(Text)
    source_text: Mapped[str] = mapped_column(Text)
    provider_id: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON)
    updated: Mapped[float] = mapped_column(Float, default=time.time)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("notifications_pending", "processed", "next_attempt", "image_id"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(Integer)
    event: Mapped[str] = mapped_column(Text)
    received: Mapped[float] = mapped_column(Float)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)


class WebhookLog(Base):
    __tablename__ = "webhook_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received: Mapped[float] = mapped_column(Float)
    receipt_id: Mapped[int | None] = mapped_column(Integer)
    event: Mapped[str | None] = mapped_column(Text)
    image_id: Mapped[str | None] = mapped_column(Text)
    signature_valid: Mapped[bool] = mapped_column(Boolean)
    http_status: Mapped[int] = mapped_column(Integer)
    body: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class TagJob(Base):
    __tablename__ = "tag_jobs"
    image_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    lease: Mapped[str | None] = mapped_column(Text)
    updated: Mapped[float] = mapped_column(Float, default=time.time)


class SentMeme(Base):
    __tablename__ = "sent_memes"
    __table_args__ = (
        Index("sent_memes_scope", "session", "conversation", "id"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[str] = mapped_column(Text, unique=True)
    session: Mapped[str] = mapped_column(Text)
    conversation: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[float] = mapped_column(Float)
    image_id: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON)


class SearchEntry(Base):
    __tablename__ = "search_index"
    __table_args__ = {"info": {"fts5": True}}
    # These form the ORM identity; SQLite FTS5 itself has no PK constraints.
    generation: Mapped[str] = mapped_column(Text, primary_key=True, info={"unindexed": True})
    image_id: Mapped[str] = mapped_column(Text, primary_key=True, info={"unindexed": True})
    text: Mapped[str] = mapped_column(Text)
    manual_tags: Mapped[str] = mapped_column(Text)
    tags: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)


class PublicBase(DeclarativeBase):
    """Only these two tables may exist in the model's isolated SQL database."""


class PublicMeme(PublicBase):
    __tablename__ = "memes"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    rating: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON)
    annotation_fresh: Mapped[bool] = mapped_column(Boolean)


class PublicMemeTag(PublicBase):
    __tablename__ = "meme_tags"
    __table_args__ = (Index("meme_tags_by_tag", "tag", "image_id"),)
    image_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)


@compiles(CreateTable, "sqlite")
def _create_sqlite_table(element, compiler, **kwargs):
    table = element.element
    if not table.info.get("fts5"):
        return compiler.visit_create_table(element, **kwargs)
    columns = ", ".join(
        compiler.preparer.quote(column.name)
        + (" UNINDEXED" if column.info.get("unindexed") else "")
        for column in table.columns
    )
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {compiler.preparer.format_table(table)} "
        f"USING fts5({columns}, tokenize='unicode61')"
    )


def configure_sqlite(engine, *, readonly=False):
    """Explicit BEGIN also makes ORM SELECTs share one SQLite read snapshot."""

    @event.listens_for(engine, "connect")
    def connect(dbapi_connection, _):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            if readonly:
                cursor.execute("PRAGMA query_only=ON")
            else:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")


def entity_dict(entity):
    return {column.key: getattr(entity, column.key) for column in entity.__table__.columns}


def image_view(image, override=None, annotation=None, job=None):
    item = entity_dict(image)
    item["id"] = str(image.id)
    item["manual_tags"] = override.manual_tags if override else []
    item["excluded_tags"] = override.excluded_tags if override else []
    item["enabled"] = override.enabled if override else True
    item["manual_description"] = override.description if override else None
    item["ai_tags"] = annotation.tags if annotation else []
    item["ai_description"] = annotation.description if annotation else ""
    item["description"] = (
        item["manual_description"]
        if item["manual_description"] is not None
        else item["ai_description"]
    )
    item["effective_tags"] = [
        tag
        for tag in dict.fromkeys(item["hub_tags"] + item["ai_tags"] + item["manual_tags"])
        if tag not in item["excluded_tags"]
    ]
    item["annotation_fresh"] = bool(
        annotation
        and annotation.image_key == image.image_key
        and annotation.source_text == image.text
        and annotation.prompt_version == PROMPT_VERSION
    )
    item["annotation_provider"] = annotation.provider_id if annotation else None
    item["tag_job"] = (
        {key: getattr(job, key) for key in ("status", "attempts", "error")} if job else None
    )

    def safe(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and abs(value) > 2**53 - 1:
            return str(value)
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [safe(item) for item in value]
        return value

    item["raw"] = safe(item["raw"])
    return item


def json_dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
