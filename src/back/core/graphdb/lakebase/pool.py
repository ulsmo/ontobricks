"""Process-wide psycopg connection pool for the Graph DB Lakebase engine.

Duplicated (with minor tweaks) from ``registry/store/lakebase/store.py`` so
``back/core/graphdb`` stays independent from ``back/objects/registry``.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from back.core.logging import get_logger

logger = get_logger(__name__)

_COLD_START_SQLSTATES = {"57P03"}
_AUTH_FAILURE_SQLSTATES = {"28P01"}
_MAX_COLD_START_ATTEMPTS = 6
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 16.0

_POOL_MAX_SIZE = 4
_POOL_MAX_LIFETIME_S = 45 * 60.0
_POOL_ACQUIRE_TIMEOUT_S = 30.0


class LakebaseGraphPoolError(RuntimeError):
    """Raised when the graph-db pool cannot serve a connection."""


def _require_psycopg():
    try:
        import psycopg  # noqa: F401
        from psycopg.rows import dict_row  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "psycopg is required for the Lakebase graph engine. Install with "
            "``uv sync --extra lakebase`` or ``pip install .[lakebase]``."
        ) from exc
    import psycopg as psy
    from psycopg.rows import dict_row as dr

    return psy, dr


class _LakebaseGraphPool:
    """Tiny thread-safe LIFO pool for Lakebase (graph DB workload)."""

    def __init__(
        self,
        *,
        auth: Any,
        schema: str,
        database: str = "",
        max_size: int = _POOL_MAX_SIZE,
        max_lifetime: float = _POOL_MAX_LIFETIME_S,
    ) -> None:
        self._auth = auth
        self._schema = schema
        self._database = database or ""
        self._max_size = max_size
        self._max_lifetime = max_lifetime
        self._cv = threading.Condition()
        self._idle: List[Tuple[Any, float]] = []
        self._size = 0
        self._closed = False

    @contextmanager
    def connection(self):
        conn, opened_at = self._acquire()
        try:
            yield conn
        except Exception:
            self._discard(conn)
            raise
        else:
            self._release(conn, opened_at)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
            self._size = 0
            self._cv.notify_all()
        for conn, _ in idle:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _is_alive(self, conn: Any, opened_at: float) -> bool:
        if (time.monotonic() - opened_at) >= self._max_lifetime:
            return False
        try:
            return not conn.closed
        except Exception:  # noqa: BLE001
            return False

    def _acquire(self, timeout: float = _POOL_ACQUIRE_TIMEOUT_S) -> Tuple[Any, float]:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                if self._closed:
                    raise LakebaseGraphPoolError("Lakebase graph pool is closed")
                while self._idle:
                    conn, opened_at = self._idle.pop()
                    if self._is_alive(conn, opened_at):
                        return conn, opened_at
                    self._size -= 1
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                if self._size < self._max_size:
                    self._size += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LakebaseGraphPoolError(
                        f"Lakebase graph pool exhausted after {timeout:.1f}s"
                    )
                self._cv.wait(remaining)
        try:
            conn = self._open_one()
        except Exception:
            with self._cv:
                self._size -= 1
                self._cv.notify()
            raise
        return conn, time.monotonic()

    def _release(self, conn: Any, opened_at: float) -> None:
        with self._cv:
            if self._closed or not self._is_alive(conn, opened_at):
                self._size -= 1
                self._cv.notify()
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            self._idle.append((conn, opened_at))
            self._cv.notify()

    def _discard(self, conn: Any) -> None:
        with self._cv:
            self._size -= 1
            self._cv.notify()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _open_one(self) -> Any:
        psycopg, _ = _require_psycopg()
        attempts = 0
        backoff = _INITIAL_BACKOFF_S
        retried_auth = False
        while True:
            try:
                kwargs = self._auth.kwargs(application_name="ontobricks-graphdb")
                if self._database:
                    kwargs["dbname"] = self._database
                conn = psycopg.connect(autocommit=True, **kwargs)
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{self._schema}", public')
                return conn
            except Exception as exc:  # noqa: BLE001
                sqlstate = getattr(exc, "sqlstate", "") or ""
                msg = str(exc).lower()
                cold = (
                    sqlstate in _COLD_START_SQLSTATES
                    or "starting up" in msg
                    or "connection refused" in msg
                )
                auth_failed = (
                    sqlstate in _AUTH_FAILURE_SQLSTATES
                    or "authentication failed" in msg
                )
                if auth_failed and not retried_auth:
                    self._auth.invalidate()
                    retried_auth = True
                    logger.info("Lakebase graph auth failed; rotating token and retrying")
                    continue
                if cold and attempts < _MAX_COLD_START_ATTEMPTS:
                    attempts += 1
                    sleep_for = min(backoff, _MAX_BACKOFF_S)
                    logger.info(
                        "Lakebase graph cold start (sqlstate=%s, attempt=%d/%d); "
                        "sleeping %.1fs",
                        sqlstate or "?",
                        attempts,
                        _MAX_COLD_START_ATTEMPTS,
                        sleep_for,
                    )
                    time.sleep(sleep_for)
                    backoff *= 2
                    continue
                raise LakebaseGraphPoolError(f"Lakebase graph connection failed: {exc}") from exc


_pools_lock = threading.Lock()
_pools: Dict[Tuple[str, str, str, str, str, str, str], _LakebaseGraphPool] = {}


def _safe_attr(obj: Any, name: str) -> str:
    try:
        return str(getattr(obj, name, "") or "")
    except Exception:  # noqa: BLE001
        return ""


def get_lakebase_graph_pool(auth: Any, schema: str, database: str = "") -> _LakebaseGraphPool:
    """Return the shared pool for *auth* + *schema* + optional *database* override."""
    bound_db = _safe_attr(auth, "database")
    effective_db = database or bound_db
    key = (
        _safe_attr(auth, "host"),
        _safe_attr(auth, "port"),
        bound_db,
        effective_db,
        _safe_attr(auth, "user"),
        _safe_attr(auth, "instance_name"),
        schema,
    )
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = _LakebaseGraphPool(auth=auth, schema=schema, database=database)
            _pools[key] = pool
            logger.info(
                "Created Lakebase graph pool for %s/%s (schema=%s)",
                key[0],
                effective_db,
                schema,
            )
        return pool
