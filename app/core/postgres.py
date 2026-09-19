"""Shared PostgreSQL helpers for stores that optionally use a shared database.

Both the assessment and the income event stores keep a SQLite backend for the
single-node deployment and switch to PostgreSQL when a database URL is
configured.  They deliberately open a short-lived connection per transaction so
a slow provider call never holds a database transaction, and so several
application replicas can share the same durable state.
"""

from __future__ import annotations

from typing import Any

__all__ = ["normalize_database_url", "postgres_connect", "postgres_row_factory"]


def normalize_database_url(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    for prefix in (
        "postgresql+asyncpg://",
        "postgres+asyncpg://",
        "postgresql+psycopg://",
        "postgres+psycopg://",
    ):
        if normalized.startswith(prefix):
            return "postgresql://" + normalized.removeprefix(prefix)
    return normalized


async def postgres_connect(database_url: str | None) -> Any:
    if not database_url:
        raise RuntimeError("A PostgreSQL database URL is required")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised in deployment images
        raise RuntimeError("PostgreSQL storage requires psycopg[binary]") from exc
    return await psycopg.AsyncConnection.connect(database_url)


def postgres_row_factory(cursor: Any) -> Any:
    try:
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("PostgreSQL storage requires psycopg[binary]") from exc
    return dict_row(cursor)
