"""DDL helpers for the writable companion table + union view.

In ``managed_synced`` mode, the Lakebase triple table per graph version is
actually three Postgres objects in the same schema:

- ``g_<dom>_v<n>_sync`` -- read-only synced table managed by Lakeflow (single ``_sync``, distinct from the union view bare name).
- ``g_<dom>_v<n>__app``  -- writable companion table (reasoning + cohort).
- ``g_<dom>_v<n>``       -- union view that readers query (back-compat name).

The synced table mirrors the source Delta view's columns
(``subject``, ``predicate``, ``object``); the companion carries the full
``(subject, predicate, object, datatype, lang)`` shape used by reasoning
output. The union view casts NULL ``datatype`` / ``lang`` for the synced
side so SPARQL / KG-search readers see a uniform schema.
"""

from __future__ import annotations

from typing import Any

from back.core.helpers import safe_identifier


def _safe(name: str) -> str:
    return (safe_identifier(name) or "triples").lower()


def synced_phy(name: str) -> str:
    """Postgres table name for the read-only synced table.

    Uses a ``_sync`` suffix (not ``__sync``) so it does not collide with the union
    view identifier (:func:`view_phy`, the legacy reader-facing name).
    """
    return f"{_safe(name)}_sync"


def companion_phy(name: str) -> str:
    """Postgres table name for the writable companion table."""
    return f"{_safe(name)}__app"


def view_phy(name: str) -> str:
    """Postgres view name readers see (matches the legacy single-table name)."""
    return _safe(name)


def _idx_name(table: str, suffix: str) -> str:
    base = f"g_{table}_{suffix}".lower()
    return base[:63]


def ensure_companion(cur: Any, schema: str, companion: str) -> None:
    """Create the writable companion table + standard B-tree indexes if absent."""
    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {companion} (
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            datatype TEXT,
            lang TEXT,
            PRIMARY KEY (subject, predicate, object)
        )
        """
    )
    for sfx, cols in (
        ("sp", "subject, predicate"),
        ("po", "predicate, object"),
        ("ops", "object, predicate"),
    ):
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {_idx_name(companion, sfx)} "
            f"ON {companion} ({cols})"
        )


def ensure_union_view(
    cur: Any,
    view: str,
    synced: str,
    companion: str,
) -> None:
    """``CREATE OR REPLACE`` the union view that readers query.

    The synced side is NULL-padded for ``datatype`` / ``lang`` so the view
    has a uniform 5-column shape regardless of which side a row came from.
    """
    sql = (
        f"CREATE OR REPLACE VIEW {view} AS "
        f"SELECT subject, predicate, object, "
        f"NULL::TEXT AS datatype, NULL::TEXT AS lang "
        f"FROM {synced} "
        f"UNION ALL "
        f"SELECT subject, predicate, object, datatype, lang FROM {companion}"
    )
    cur.execute(sql)


def truncate_companion(cur: Any, companion: str) -> None:
    """Drop all rows from the companion table (used on full rebuild)."""
    cur.execute(f"TRUNCATE TABLE {companion}")


def drop_companion(cur: Any, companion: str) -> None:
    cur.execute(f"DROP TABLE IF EXISTS {companion}")


def drop_view(cur: Any, view: str) -> None:
    cur.execute(f"DROP VIEW IF EXISTS {view}")
