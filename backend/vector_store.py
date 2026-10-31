"""Bounded cosine search over normalized vectors persisted through SQLAlchemy."""

from __future__ import annotations

import asyncio
import heapq
import math
from array import array
from pathlib import Path

from sqlalchemy import Integer, LargeBinary, String, create_engine, delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import NullPool

from .errors import SemanticError


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profile"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dimension: Mapped[int]


class Vector(Base):
    __tablename__ = "vectors"
    image_id: Mapped[str] = mapped_column(String(19), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)


class SQLiteVectorStore:
    """No image bytes or host-private vector database dependencies are stored here."""

    def __init__(self, root: Path, dimension: int):
        self.root, self.dimension = root, dimension
        self.engine = None

    def _decode(self, data):
        if len(data) != self.dimension * 4:
            raise SemanticError("向量缓存维度不匹配，需要重建")
        values = array("f")
        values.frombytes(data)
        if not all(math.isfinite(value) for value in values):
            raise SemanticError("向量缓存包含非有限数，需要重建")
        norm = math.fsum(value * value for value in values)
        if not 0.999 <= norm <= 1.001:
            raise SemanticError("向量缓存未归一化，需要重建")
        return values

    async def initialize(self):
        return await asyncio.to_thread(self._initialize)

    def _initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            "sqlite+pysqlite:///" + str(self.root / "vectors.sqlite3"),
            poolclass=NullPool,
            connect_args={"timeout": 10},
        )
        Base.metadata.create_all(self.engine)
        entries = {}
        with Session(self.engine) as session, session.begin():
            profile = session.get(Profile, 1)
            if profile is None:
                session.add(Profile(id=1, dimension=self.dimension))
            elif profile.dimension != self.dimension:
                raise SemanticError("向量缓存模型维度发生变化，需要重建")
            for row in session.scalars(select(Vector).execution_options(yield_per=128)):
                self._decode(row.data)
                if not row.image_id.isdecimal() or len(row.fingerprint) != 64:
                    raise SemanticError("向量缓存元数据无效，需要重建")
                entries[row.image_id] = row.fingerprint
        return entries

    async def insert(self, documents, vectors):
        rows = []
        for doc, values in zip(documents, vectors, strict=True):
            data = array("f", values).tobytes()
            self._decode(data)
            rows.append({"image_id": doc["id"], "fingerprint": doc["fingerprint"], "data": data})
        await asyncio.to_thread(self._insert, rows)

    def _insert(self, rows):
        with Session(self.engine) as session, session.begin():
            for row in rows:
                statement = insert(Vector).values(**row)
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[Vector.image_id],
                        set_={
                            "fingerprint": statement.excluded.fingerprint,
                            "data": statement.excluded.data,
                        },
                    )
                )

    async def delete(self, ident):
        await asyncio.to_thread(self._delete, ident)

    def _delete(self, ident):
        with Session(self.engine) as session, session.begin():
            session.execute(delete(Vector).where(Vector.image_id == ident))

    async def search(self, concept, vector, count):
        del concept
        if len(vector) != self.dimension:
            raise SemanticError("查询向量维度无效")
        return await asyncio.to_thread(self._search, vector, count)

    def _search(self, query, count):
        best = []
        with Session(self.engine) as session:
            for row in session.scalars(select(Vector).execution_options(yield_per=128)):
                values = self._decode(row.data)
                similarity = max(
                    -1.0, min(1.0, math.fsum(a * b for a, b in zip(query, values, strict=True)))
                )
                candidate = (similarity, row.image_id, row.fingerprint)
                if len(best) < count:
                    heapq.heappush(best, candidate)
                elif candidate > best[0]:
                    heapq.heapreplace(best, candidate)
        return [
            {"image_id": ident, "fingerprint": fingerprint, "similarity": similarity}
            for similarity, ident, fingerprint in sorted(best, reverse=True)
        ]

    async def close(self):
        if self.engine is not None:
            await asyncio.to_thread(self.engine.dispose)
            self.engine = None
