"""Manage Lakebase synced tables (Lakeflow snapshot pipelines).

Wraps the ``WorkspaceClient.database`` SDK behind a narrow surface so the
build pipeline can drive bulk R2RML data movement entirely on the data
plane (Databricks Delta -> Lakebase Postgres) without iterating triples
in the FastAPI process.

The full architecture is described in
:doc:`/graphdb-integration` (Lakebase managed-synced section).

SDK compatibility
-----------------
``databricks.sdk.service.database`` is a relatively new module that may be
absent from the SDK version bundled into a Databricks Apps container.  When
the module is missing the manager falls back to:

* **REST calls** — direct ``requests`` calls to
  ``/api/2.0/database/synced_tables/…`` authenticated with
  ``DATABRICKS_TOKEN`` / ``DATABRICKS_HOST`` (auto-injected by Apps).
* **Plain-dict payloads** — synced-table specs are built as ``dict``
  objects rather than SDK dataclasses.
* **``_DictNamespace`` responses** — API responses are wrapped in a
  simple attribute-access shim so the same ``getattr``-based helpers
  (``_extract_state``, ``_extract_pipeline_id``) work for both paths.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, List, Optional

from back.core.errors import InfrastructureError, ValidationError
from back.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_S = 600
_POLL_INTERVAL_S = 5

# State names are normalised by ``_extract_state`` — the ``SYNCED_TABLE_``
# SDK prefix is stripped, and the SDK typo ``SYNCED_TABLED_OFFLINE`` becomes
# ``TABLED_OFFLINE`` (included in _TERMINAL_FAIL below).
_TERMINAL_OK = {"ONLINE", "ONLINE_NO_PENDING_UPDATE"}
_TERMINAL_FAIL = {"FAILED", "OFFLINE_FAILED", "TABLED_OFFLINE"}
_IN_PROGRESS = {
    "PROVISIONING",
    "PROVISIONING_INITIAL_SNAPSHOT",
    "PROVISIONING_PIPELINE_RESOURCES",
    "ONLINE_TRIGGERED_UPDATE",
    "ONLINE_PIPELINE_FAILED",
    "ONLINE_UPDATING_PIPELINE_RESOURCES",
    "ONLINE_CONTINUOUS_UPDATE",
    "OFFLINE",
}

# ---------------------------------------------------------------------------
# SDK availability probe
# ---------------------------------------------------------------------------

try:
    from databricks.sdk.service import database as _db_svc  # noqa: F401

    _SDK_DATABASE_AVAILABLE = True
except ImportError:
    _db_svc = None  # type: ignore[assignment]
    _SDK_DATABASE_AVAILABLE = False
    logger.debug(
        "databricks.sdk.service.database not available — "
        "SyncedTableManager will use direct REST calls"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DictNamespace:
    """Recursively wrap a JSON dict so nested keys are accessible as attributes.

    Used to normalise SDK-object vs. plain-dict responses so the same
    ``getattr``-based helpers work regardless of which code path ran.
    """

    def __init__(self, data: Any) -> None:
        self._data = data if isinstance(data, dict) else {}

    def __getattr__(self, name: str) -> Any:
        val = self._data.get(name)
        if isinstance(val, dict):
            return _DictNamespace(val)
        if isinstance(val, list):
            return [_DictNamespace(v) if isinstance(v, dict) else v for v in val]
        return val

    def __repr__(self) -> str:  # pragma: no cover
        return f"_DictNamespace({self._data!r})"


def _to_dict(obj: Any) -> dict:
    """Convert an SDK dataclass *or* a plain dict to ``dict``."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    return {}


# ---------------------------------------------------------------------------
# REST-only database API (no databricks.sdk.service.database required)
# ---------------------------------------------------------------------------


class _RestDatabaseAPI:
    """Direct REST implementation of the three synced-table endpoints.

    Uses the SDK ``WorkspaceClient.api_client.do()`` for auth so the same
    credential flow works in local dev (PAT), Databricks Apps (OIDC M2M),
    and CI (env-var token).  Falls back to a raw ``requests`` call with
    ``DATABRICKS_TOKEN`` only when the SDK's ``api_client`` is unavailable.
    """

    def __init__(self, workspace_client: Any) -> None:
        self._wc = workspace_client

    def _do(self, method: str, path: str, **kwargs: Any) -> Any:
        """Route through SDK's ApiClient when possible; raw requests otherwise."""
        api = getattr(self._wc, "api_client", None)
        if api is not None and hasattr(api, "do"):
            # SDK ApiClient handles auth (PAT / OIDC / Apps credential injection).
            return api.do(method, path, **kwargs)

        # Last-resort fallback: raw requests with DATABRICKS_TOKEN.
        import os

        import requests

        host = (os.environ.get("DATABRICKS_HOST") or "").strip().rstrip("/")
        if host and not host.startswith(("http://", "https://")):
            host = "https://" + host
        token = os.environ.get("DATABRICKS_TOKEN") or ""
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        body = kwargs.get("body")
        resp = requests.request(
            method,
            f"{host}{path}",
            json=body,
            params=kwargs.get("query"),
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 404:
            raise ValueError(f"404 Not Found: {path}")
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def get_synced_database_table(self, *, name: str) -> Any:
        try:
            data = self._do("GET", f"/api/2.0/database/synced_tables/{name}")
        except Exception as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                raise ValueError(f"Synced table {name!r} not found") from exc
            raise
        if _SDK_DATABASE_AVAILABLE and _db_svc is not None:
            return _db_svc.SyncedDatabaseTable.from_dict(data)
        return _DictNamespace(data)

    def create_synced_database_table(self, *, synced_table: Any) -> Any:
        body = _to_dict(synced_table)
        data = self._do("POST", "/api/2.0/database/synced_tables", body=body)
        if _SDK_DATABASE_AVAILABLE and _db_svc is not None:
            return _db_svc.SyncedDatabaseTable.from_dict(data)
        return _DictNamespace(data)

    def delete_synced_database_table(self, *, name: str, purge_data: bool = True) -> None:
        try:
            self._do(
                "DELETE",
                f"/api/2.0/database/synced_tables/{name}",
                query={"purge_data": "true" if purge_data else "false"},
            )
        except Exception as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return
            raise


class _CompatWorkspaceClient:
    """Thin wrapper that injects a REST-based ``.database`` when the SDK lacks one."""

    def __init__(self, workspace_client: Any) -> None:
        self._wc = workspace_client
        self.database = _RestDatabaseAPI(workspace_client)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wc, name)


# ---------------------------------------------------------------------------
# SyncedTableManager
# ---------------------------------------------------------------------------


class SyncedTableManager:
    """Idempotent wrapper for Lakebase synced-table CRUD + refresh.

    The manager is bound to a single Lakebase project + logical database.
    The synced table's UC fully-qualified name (``<catalog>.<schema>.<table>``)
    must include a *schema* segment that matches the Postgres-side schema
    where the companion table will live, so both end up in the same
    ``search_path``.
    """

    def __init__(
        self,
        database_instance_name: str,
        logical_database_name: str,
        *,
        client_factory: Optional[Any] = None,
        sleep: Optional[Any] = None,
    ) -> None:
        self._instance = database_instance_name
        self._logical_db = logical_database_name
        self._client_factory = client_factory or self._default_factory
        self._client: Optional[Any] = None
        self._sleep = sleep or time.sleep

    @staticmethod
    def _default_factory() -> Any:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        if hasattr(w, "database"):
            return w
        # SDK in this environment predates WorkspaceClient.database — use REST.
        logger.debug(
            "WorkspaceClient.database not available — using REST fallback for synced-table API"
        )
        return _CompatWorkspaceClient(w)

    def _w(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    # -- CRUD -------------------------------------------------------------

    def get(self, name: str) -> Optional[Any]:
        """Return the ``SyncedDatabaseTable`` (or dict-namespace) or *None*."""
        try:
            return self._w().database.get_synced_database_table(name=name)
        except Exception as exc:  # noqa: BLE001
            if self._is_not_found(exc):
                return None
            raise

    def exists(self, name: str) -> bool:
        return self.get(name) is not None

    def ensure(
        self,
        name: str,
        *,
        source_table_full_name: str,
        primary_key_columns: List[str],
        sync_mode: str = "snapshot",
        create_database_objects_if_missing: bool = True,
    ) -> Any:
        """Create the synced table if missing; otherwise return the existing one."""
        existing = self.get(name)
        if existing is not None:
            logger.info("Synced table %s already exists — reusing", name)
            return existing

        synced = self._build_synced_table_payload(
            name=name,
            source_table_full_name=source_table_full_name,
            primary_key_columns=primary_key_columns,
            sync_mode=sync_mode,
            create_database_objects_if_missing=create_database_objects_if_missing,
        )
        try:
            created = self._w().database.create_synced_database_table(
                synced_table=synced
            )
            logger.info(
                "Created synced table %s (source=%s, mode=%s)",
                name,
                source_table_full_name,
                sync_mode,
            )
            return created
        except Exception as exc:  # noqa: BLE001
            if self._is_already_exists(exc):
                logger.info("Synced table %s already exists (race)", name)
                return self.get(name)
            raise InfrastructureError(
                f"Failed to create synced table {name}: {exc}"
            ) from exc

    def trigger_refresh(self, name: str) -> str:
        """Kick off a Lakeflow update for this synced table; return the ``update_id``."""
        synced = self.get(name)
        if synced is None:
            raise ValidationError(f"Synced table {name} does not exist")
        pipeline_id = self._extract_pipeline_id(synced)
        if not pipeline_id:
            raise InfrastructureError(
                f"Synced table {name} has no pipeline_id yet — initial provisioning "
                f"is still in progress"
            )
        try:
            from databricks.sdk.service.pipelines import StartUpdateCause

            update = self._w().pipelines.start_update(
                pipeline_id=pipeline_id,
                full_refresh=True,
                cause=StartUpdateCause.API_CALL,
            )
            return getattr(update, "update_id", "") or ""
        except Exception as exc:  # noqa: BLE001
            if self._is_active_pipeline_update_conflict(exc):
                logger.info(
                    "Skipping start_update for %s — pipeline %s already has an "
                    "active update (will poll sync state instead)",
                    name,
                    pipeline_id,
                )
                return ""
            raise

    def wait_for_completion(
        self,
        name: str,
        *,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        pipeline_update_id: Optional[str] = None,
    ) -> str:
        """Block until the sync reaches a terminal state; raise on failure / timeout."""
        deadline = time.time() + max(1, int(timeout_s))
        last_state = ""
        idle_pipeline_wait_done = False

        uid = (pipeline_update_id or "").strip()
        if uid:
            synced = self.get(name)
            pid = self._extract_pipeline_id(synced) if synced else ""
            if not pid:
                raise InfrastructureError(
                    f"Synced table {name} has no pipeline_id — cannot track update {uid}"
                )
            remaining = max(0.0, deadline - time.time())
            if remaining > 1:
                logger.info(
                    "Waiting for Lakeflow pipeline update %s (pipeline %s · synced %s)",
                    uid,
                    pid,
                    name,
                )
                self._wait_for_pipeline_update(pid, uid, remaining)
            idle_pipeline_wait_done = True

        while True:
            now = time.time()
            if now >= deadline:
                raise InfrastructureError(
                    f"Timed out after {timeout_s}s waiting for synced table {name} "
                    f"(last state={last_state or '?'})"
                )

            synced = self.get(name)
            state = self._extract_state(synced) if synced else "MISSING"
            if state and state != last_state:
                logger.info("Synced table %s state=%s", name, state)
                last_state = state

            if state in _TERMINAL_FAIL:
                raise InfrastructureError(
                    f"Synced table {name} reached terminal failure state: {state}"
                )

            if not idle_pipeline_wait_done:
                pid = self._extract_pipeline_id(synced) if synced else ""
                if pid:
                    remaining = deadline - time.time()
                    if remaining > 1:
                        logger.info(
                            "Waiting for Lakeflow pipeline %s to finish "
                            "(Databricks · synced table %s)",
                            pid,
                            name,
                        )
                        try:
                            self._w().pipelines.wait_get_pipeline_idle(
                                pipeline_id=pid,
                                timeout=timedelta(seconds=float(remaining)),
                            )
                        except Exception as exc:  # noqa: BLE001
                            raise InfrastructureError(
                                f"Lakeflow pipeline {pid} did not become idle while "
                                f"syncing {name}: {exc}"
                            ) from exc
                    idle_pipeline_wait_done = True
                    continue

            if state in _TERMINAL_OK:
                return state

            self._sleep(_POLL_INTERVAL_S)

    def trigger_and_wait(
        self,
        name: str,
        *,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        skip_initial_trigger: bool = False,
    ) -> str:
        """Convenience: ``trigger_refresh`` then ``wait_for_completion``."""
        update_id = ""
        if not skip_initial_trigger:
            try:
                update_id = self.trigger_refresh(name)
            except (ValidationError, InfrastructureError) as exc:
                logger.debug(
                    "Could not trigger refresh on %s yet (will poll): %s", name, exc
                )
        uid = (update_id or "").strip()
        return self.wait_for_completion(
            name,
            timeout_s=timeout_s,
            pipeline_update_id=uid or None,
        )

    def _wait_for_pipeline_update(
        self, pipeline_id: str, update_id: str, timeout_s: float
    ) -> None:
        """Poll ``pipelines.get_update`` until the update completes or fails."""
        deadline = time.time() + max(1.0, float(timeout_s))
        terminal_ok = {"COMPLETED"}
        terminal_bad = {"FAILED", "CANCELED"}
        while time.time() < deadline:
            try:
                resp = self._w().pipelines.get_update(
                    pipeline_id=pipeline_id,
                    update_id=update_id,
                )
            except Exception as exc:  # noqa: BLE001
                raise InfrastructureError(
                    f"Could not read pipeline update {update_id} for {pipeline_id}: {exc}"
                ) from exc
            upd = getattr(resp, "update", None)
            st = getattr(upd, "state", None) if upd else None
            raw = getattr(st, "name", st) if st is not None else None
            name_st = str(raw).upper() if raw is not None else ""
            if "." in name_st:
                name_st = name_st.split(".")[-1]
            if name_st in terminal_ok:
                logger.info(
                    "Pipeline update %s finished successfully (pipeline %s)",
                    update_id,
                    pipeline_id,
                )
                return
            if name_st in terminal_bad:
                raise InfrastructureError(
                    f"Pipeline update {update_id} for {pipeline_id} ended with {name_st}"
                )
            self._sleep(_POLL_INTERVAL_S)
        raise InfrastructureError(
            f"Timed out waiting for pipeline update {update_id} (pipeline {pipeline_id})"
        )

    def delete(self, name: str, *, purge_data: bool = True) -> None:
        """Delete the synced table from UC; optionally drop the underlying PG table."""
        try:
            self._w().database.delete_synced_database_table(
                name=name, purge_data=purge_data
            )
            logger.info("Deleted synced table %s (purge_data=%s)", name, purge_data)
        except Exception as exc:  # noqa: BLE001
            if self._is_not_found(exc):
                return
            raise InfrastructureError(
                f"Failed to delete synced table {name}: {exc}"
            ) from exc

    # -- Payload builders -------------------------------------------------

    def _build_synced_table_payload(
        self,
        *,
        name: str,
        source_table_full_name: str,
        primary_key_columns: List[str],
        sync_mode: str,
        create_database_objects_if_missing: bool,
    ) -> Any:
        """Return a synced-table payload — SDK object when available, dict otherwise."""
        if _SDK_DATABASE_AVAILABLE and _db_svc is not None:
            return self._build_synced_table_sdk(
                name=name,
                source_table_full_name=source_table_full_name,
                primary_key_columns=primary_key_columns,
                sync_mode=sync_mode,
                create_database_objects_if_missing=create_database_objects_if_missing,
            )
        return self._build_synced_table_dict(
            name=name,
            source_table_full_name=source_table_full_name,
            primary_key_columns=primary_key_columns,
            sync_mode=sync_mode,
            create_database_objects_if_missing=create_database_objects_if_missing,
        )

    def _build_synced_table_sdk(
        self,
        *,
        name: str,
        source_table_full_name: str,
        primary_key_columns: List[str],
        sync_mode: str,
        create_database_objects_if_missing: bool,
    ) -> Any:
        sync_mode_norm = (sync_mode or "snapshot").upper()
        scheduling_policy_cls = getattr(_db_svc, "SyncedTableSchedulingPolicy", None)
        if scheduling_policy_cls is not None:
            policy = getattr(scheduling_policy_cls, sync_mode_norm, None) or getattr(
                scheduling_policy_cls, "SNAPSHOT"
            )
        else:
            policy = sync_mode_norm

        spec = _db_svc.SyncedTableSpec(  # type: ignore[union-attr]
            source_table_full_name=source_table_full_name,
            primary_key_columns=list(primary_key_columns),
            scheduling_policy=policy,
            create_database_objects_if_missing=create_database_objects_if_missing,
        )
        return _db_svc.SyncedDatabaseTable(  # type: ignore[union-attr]
            name=name,
            database_instance_name=self._instance,
            logical_database_name=self._logical_db,
            spec=spec,
        )

    def _build_synced_table_dict(
        self,
        *,
        name: str,
        source_table_full_name: str,
        primary_key_columns: List[str],
        sync_mode: str,
        create_database_objects_if_missing: bool,
    ) -> dict:
        return {
            "name": name,
            "database_instance_name": self._instance,
            "logical_database_name": self._logical_db,
            "spec": {
                "source_table_full_name": source_table_full_name,
                "primary_key_columns": list(primary_key_columns),
                "scheduling_policy": (sync_mode or "snapshot").upper(),
                "create_database_objects_if_missing": create_database_objects_if_missing,
            },
        }

    # -- SDK shape helpers ------------------------------------------------

    @staticmethod
    def _extract_pipeline_id(synced: Any) -> str:
        status = getattr(synced, "data_synchronization_status", None)
        if status is None:
            return ""
        return getattr(status, "pipeline_id", "") or ""

    @staticmethod
    def _extract_state(synced: Any) -> str:
        status = getattr(synced, "data_synchronization_status", None)
        if status is None:
            return ""
        state = (
            getattr(status, "detailed_state", None)
            or getattr(status, "state", None)
            or ""
        )
        raw = str(getattr(state, "name", state)).upper()
        return raw.removeprefix("SYNCED_TABLE_")

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "not found" in msg or "does not exist" in msg or "404" in msg

    @staticmethod
    def _is_already_exists(exc: Exception) -> bool:
        msg = str(exc).lower()
        return (
            "already exists" in msg
            or "already_exists" in msg
            or "alreadyexists" in msg
        )

    @staticmethod
    def _is_active_pipeline_update_conflict(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return (
            "active update" in msg
            and "already exists" in msg
            and "pipeline" in msg
        )
