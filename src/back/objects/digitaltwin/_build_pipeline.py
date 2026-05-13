"""Internal helper that drives a single Digital Twin build.

Extracted from :class:`back.objects.digitaltwin.DigitalTwin.run_build_task`
(formerly an 839-line method) to make each phase — prepare → view →
apply → cache → archive — a named, focused method that shares state via
``self`` instead of a closure.

This module is **private** to the ``digitaltwin`` package; it is not
re-exported from ``__init__.py`` and external callers must keep using
``DigitalTwin.run_build_task`` (which is now a thin delegator).

Incremental diff / Delta-snapshot management was removed in v0.4.1.
All builds are full rebuilds; when the graph engine is ``lakebase``
in ``managed_synced`` mode the data-plane Lakeflow pipeline handles
the refresh automatically.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from back.core.errors import OntoBricksError
from back.core.logging import get_logger
from back.objects.digitaltwin.models import DomainSnapshot

logger = get_logger(__name__)


class _BuildPipeline:
    """One run of the Digital Twin build/sync pipeline.

    Constructed once per build with the same arguments as the legacy
    :meth:`DigitalTwin.run_build_task`. Call :meth:`run` to execute the
    pipeline; it never raises (errors flow through ``tm.fail_task``).
    """

    def __init__(
        self,
        tm,
        task_id: str,
        domain,
        settings,
        domain_snap: DomainSnapshot,
        host: str,
        token: str,
        warehouse_id: str,
        view_table: str,
        graph_name: str,
        r2rml_content: str,
        base_uri: str,
        mapping_config,
        ontology_config,
        delta_cfg: dict,
        *,
        build_kind: str = "session",
    ) -> None:
        self.tm = tm
        self.task_id = task_id
        self.domain = domain
        self.settings = settings
        self.domain_snap = domain_snap
        self.host = host
        self.token = token
        self.warehouse_id = warehouse_id
        self.view_table = view_table
        self.graph_name = graph_name
        self.r2rml_content = r2rml_content
        self.base_uri = base_uri
        self.mapping_config = mapping_config
        self.ontology_config = ontology_config
        self.delta_cfg = delta_cfg
        self.build_kind = build_kind

        self.is_api = build_kind == "api"
        self.start_time = time.time()
        self.phase_times: Dict[str, float] = {}
        self.parts = view_table.split(".")

        self.domain_name = (domain.info or {}).get("name", "<unknown>")

        # Lazy-initialised across phases.
        self.source_client = None
        self.store = None
        self.entity_mappings: list = []
        self.relationship_mappings: list = []
        self.spark_sql: str = ""
        self.triple_count: int = 0
        # Lakebase managed-synced mode flag, resolved once before _open_store.
        self._lakebase_engine_config: Dict[str, Any] = {}
        self._is_lakebase_synced: bool = False

    # ------------------------------------------------------------------
    # Phase utilities
    # ------------------------------------------------------------------

    def _log_phase(self, name: str, t0_phase: float) -> None:
        elapsed = time.time() - t0_phase
        self.phase_times[name] = elapsed
        logger.info(
            "[DT-BUILD %s] phase [%s]: %.2fs", self.task_id, name, elapsed
        )

    def _resolve_lakebase_mode(self) -> None:
        """Resolve graph engine + engine_config once, before ``_open_store``."""
        from back.core.triplestore.TripleStoreFactory import TripleStoreFactory

        try:
            engine = TripleStoreFactory._resolve_graph_engine(
                self.domain, self.settings
            )
            cfg = TripleStoreFactory._resolve_graph_engine_config(
                self.domain, self.settings
            ) or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[DT-BUILD %s] could not resolve lakebase mode: %s",
                self.task_id,
                exc,
            )
            engine = "lakebase"
            cfg = {}
        self._lakebase_engine_config = cfg
        self._is_lakebase_synced = (
            engine == "lakebase" and cfg.get("sync_mode") == "managed_synced"
        )
        if self._is_lakebase_synced:
            logger.info(
                "[DT-BUILD %s] lakebase managed_synced mode active — "
                "bulk data movement runs on the data plane via Lakeflow",
                self.task_id,
            )

    def _lakebase_managed_synced(self) -> bool:
        """Return ``True`` when bulk data movement should be delegated to Lakeflow."""
        return self._is_lakebase_synced

    def _count_view_triples(self) -> int:
        """Return the number of triples in the VIEW (server-side COUNT)."""
        try:
            rows = self.source_client.execute_query(
                f"SELECT COUNT(*) AS cnt FROM {self.view_table}"
            )
            return int(rows[0].get("cnt", 0)) if rows else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Drive the build through every phase, reporting progress to ``tm``."""
        self._log_start()
        try:
            t_phase = time.time()
            if not self._prepare_translation():
                return
            self._log_phase("prepare", t_phase)
            self.tm.update_progress(self.task_id, 10, "SQL generated")

            self._resolve_lakebase_mode()

            t_phase = time.time()
            if not self._create_view():
                return
            self._log_phase("create_view", t_phase)
            self._post_create_view_progress()

            t_phase = time.time()
            self._announce_apply_step()

            if not self._open_store():
                return

            if not self._apply_full_rebuild():
                return

            self._log_phase("apply_graph", t_phase)

            if not self.is_api:
                self._populate_session_cache()

            self._complete_task()

        except Exception as exc:  # noqa: BLE001 — orchestrator final guard
            self._fail_unexpected(exc)

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    def _log_start(self) -> None:
        logger.info(
            "[DT-BUILD %s] START kind=%s domain=%s view=%s graph=%s "
            "warehouse=%s",
            self.task_id,
            self.build_kind,
            self.domain_name,
            self.view_table,
            self.graph_name,
            self.warehouse_id,
        )

    def _prepare_translation(self) -> bool:
        """Parse R2RML, augment mappings, build the Spark SQL union query."""
        from back.core.databricks import DatabricksClient
        from back.core.w3c import sparql

        from back.objects.digitaltwin.DigitalTwin import DigitalTwin

        self.tm.start_task(self.task_id, "Preparing mappings...")
        self.source_client = DatabricksClient(
            host=self.host, token=self.token, warehouse_id=self.warehouse_id
        )

        entity_mappings, relationship_mappings = sparql.extract_r2rml_mappings(
            self.r2rml_content
        )
        logger.info(
            "[DT-BUILD %s] R2RML parsed: %d entity mapping(s), "
            "%d relationship mapping(s)",
            self.task_id,
            len(entity_mappings or []),
            len(relationship_mappings or []),
        )
        entity_mappings = DigitalTwin.augment_mappings_from_config(
            entity_mappings, self.mapping_config, self.base_uri, self.ontology_config
        )
        relationship_mappings = DigitalTwin.augment_relationships_from_config(
            relationship_mappings,
            self.mapping_config,
            self.base_uri,
            self.ontology_config,
        )
        logger.info(
            "[DT-BUILD %s] mappings augmented from config: %d entity, "
            "%d relationship (base_uri=%s)",
            self.task_id,
            len(entity_mappings or []),
            len(relationship_mappings or []),
            self.base_uri,
        )
        self.entity_mappings = entity_mappings
        self.relationship_mappings = relationship_mappings

        if not entity_mappings and not relationship_mappings:
            logger.warning(
                "[DT-BUILD %s] aborting: no valid mappings found "
                "(entities=%s, relationships=%s)",
                self.task_id,
                bool(entity_mappings),
                bool(relationship_mappings),
            )
            self.tm.fail_task(self.task_id, "No valid mappings found")
            return False

        all_data_sparql = (
            f"PREFIX : <{self.base_uri}>\n"
            "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n\n"
            "SELECT DISTINCT ?subject ?predicate ?object\n"
            "WHERE {\n"
            "    ?subject ?predicate ?object .\n"
            "}"
        )

        try:
            result = sparql.translate_sparql_to_spark(
                all_data_sparql,
                entity_mappings,
                None,
                relationship_mappings,
                dialect="spark",
            )
        except OntoBricksError as exc:
            logger.error(
                "[DT-BUILD %s] SPARQL→Spark translation failed: %s",
                self.task_id,
                exc.message,
            )
            self.tm.fail_task(self.task_id, exc.message)
            return False

        if self.is_api and not result.get("success"):
            logger.error(
                "[DT-BUILD %s] SPARQL→Spark translation returned failure: %s",
                self.task_id,
                result.get("message", "Translation failed"),
            )
            self.tm.fail_task(
                self.task_id, result.get("message", "Translation failed")
            )
            return False

        self.spark_sql = result["sql"]
        logger.info(
            "[DT-BUILD %s] SPARQL→Spark translation OK (sql_chars=%d)",
            self.task_id,
            len(self.spark_sql or ""),
        )
        return True

    def _create_view(self) -> bool:
        """Create or replace the Spark VIEW. Returns ``False`` on failure."""
        from back.objects.digitaltwin.DigitalTwin import DigitalTwin

        self.tm.advance_step(self.task_id, f"Creating VIEW {self.view_table}...")
        logger.info(
            "[DT-BUILD %s] creating VIEW %s on warehouse %s",
            self.task_id,
            self.view_table,
            self.warehouse_id,
        )
        try:
            catalog, schema, vname = self.parts
            view_ok, view_msg = self.source_client.create_or_replace_view(
                catalog, schema, vname, self.spark_sql
            )
            if not view_ok:
                if self.is_api:
                    logger.error(
                        "[DT-BUILD %s] failed to create VIEW %s: %s",
                        self.task_id,
                        self.view_table,
                        view_msg,
                    )
                    self.tm.fail_task(
                        self.task_id, f"Failed to create VIEW: {view_msg}"
                    )
                else:
                    detail = DigitalTwin.diagnose_view_error(
                        view_msg, self.entity_mappings, self.relationship_mappings
                    )
                    logger.error(
                        "[DT-BUILD %s] failed to create VIEW %s:\n%s",
                        self.task_id,
                        self.view_table,
                        detail,
                    )
                    self.tm.fail_task(
                        self.task_id, f"Failed to create VIEW: {detail}"
                    )
                return False
            logger.info(
                "[DT-BUILD %s] VIEW %s created", self.task_id, self.view_table
            )
            return True
        except Exception as exc:  # noqa: BLE001
            if self.is_api:
                logger.exception(
                    "[DT-BUILD %s] VIEW creation raised: %s", self.task_id, exc
                )
                self.tm.fail_task(self.task_id, str(exc))
                return False
            detail = DigitalTwin.diagnose_view_error(
                str(exc), self.entity_mappings, self.relationship_mappings
            )
            logger.exception(
                "[DT-BUILD %s] failed to create VIEW %s:\n%s",
                self.task_id,
                self.view_table,
                detail,
            )
            self.tm.fail_task(self.task_id, f"Failed to create VIEW: {detail}")
            return False

    def _post_create_view_progress(self) -> None:
        if self.is_api:
            self.tm.update_progress(self.task_id, 25, "VIEW created")
        else:
            self.tm.update_progress(
                self.task_id, 25, f"VIEW {self.view_table} created"
            )

    def _announce_apply_step(self) -> None:
        apply_msg = (
            "Applying changes to graph..."
            if self.is_api
            else "Applying changes to the knowledge graph..."
        )
        self.tm.advance_step(self.task_id, apply_msg)

    def _open_store(self) -> bool:
        """Initialise the graph backend. Returns ``False`` on failure."""
        from back.core.triplestore import get_triplestore as _get_ts

        self.store = _get_ts(self.domain_snap, self.settings, backend="graph")
        if not self.store:
            logger.error(
                "[DT-BUILD %s] could not initialize graph backend "
                "(domain=%s)",
                self.task_id,
                self.domain_name,
            )
            self.tm.fail_task(self.task_id, "Could not initialize graph backend")
            return False
        return True

    def _apply_full_rebuild(self) -> bool:
        """Drop, recreate, and bulk-insert all triples (or trigger Lakeflow sync).

        In ``managed_synced`` mode the entire branch is replaced by
        :meth:`_apply_via_synced_pipeline` — the Lakeflow snapshot pipeline
        rewrites the synced PG table and the app does not iterate triples.
        """
        if self._lakebase_managed_synced():
            return self._apply_via_synced_pipeline()

        t_fetch = time.time()
        logger.info(
            "[DT-BUILD %s] full rebuild: reading all triples from VIEW %s",
            self.task_id,
            self.view_table,
        )
        if not self.is_api:
            self.tm.update_progress(self.task_id, 40, "Reading all triples from VIEW...")

        triple_count = self._count_view_triples()
        self._log_phase("fetch_triples", t_fetch)

        self.triple_count = triple_count
        logger.info(
            "[DT-BUILD %s] VIEW reports %d triples to ingest",
            self.task_id,
            triple_count,
        )
        if triple_count == 0:
            logger.warning(
                "[DT-BUILD %s] VIEW %s returned 0 triples — check that "
                "your R2RML mappings match real data in the source "
                "tables (view will exist but graph will be empty)",
                self.task_id,
                self.view_table,
            )
            empty_msg = (
                "VIEW created but no triples generated (check your mappings)"
                if not self.is_api
                else "VIEW created but no triples generated"
            )
            self.tm.complete_task(
                self.task_id,
                result={
                    "triple_count": 0,
                    "view_table": self.view_table,
                    "graph_name": self.graph_name,
                    "build_mode": "full",
                    "duration_seconds": time.time() - self.start_time,
                },
                message=empty_msg,
            )
            return False

        t_insert = time.time()
        if not self.is_api:
            self.tm.update_progress(
                self.task_id, 50, f"Full rebuild: writing {triple_count} triples..."
            )
        logger.info(
            "[DT-BUILD %s] dropping & recreating graph table %s",
            self.task_id,
            self.graph_name,
        )
        self.store.drop_table(self.graph_name)
        self.store.create_table(self.graph_name)

        is_api_local = self.is_api
        tm_local = self.tm
        task_id_local = self.task_id
        total_local = triple_count

        def _on_progress_full(written: int, total: int) -> None:
            denom = total_local or total or 1
            progress = 50 + int(written / denom * 40)
            if is_api_local:
                tm_local.update_progress(
                    task_id_local,
                    progress,
                    f"Written {written}/{denom} triples...",
                )
            else:
                tm_local.update_progress(
                    task_id_local,
                    min(progress, 90),
                    f"Written {written}/{denom} triples...",
                )

        logger.info(
            "[DT-BUILD %s] streaming %d triples into %s (batch_size=5000)",
            self.task_id,
            triple_count,
            self.graph_name,
        )
        select_sql = (
            f"SELECT subject, predicate, object FROM {self.view_table}"
        )
        self._stream_triples_into_store(
            select_sql,
            insert_batch_size=5000,
            on_progress=_on_progress_full,
        )
        logger.info(
            "[DT-BUILD %s] optimizing graph table %s",
            self.task_id,
            self.graph_name,
        )
        self.store.optimize_table(self.graph_name)
        self._log_phase("graph_insert", t_insert)
        return True

    def _stream_triples_into_store(
        self,
        select_sql: str,
        *,
        insert_batch_size: int = 5000,
        on_progress: Optional[Any] = None,
    ) -> int:
        """Stream warehouse rows into the graph store via the bulk insert iterator."""
        triple_iter = self.source_client.iter_rows(
            select_sql, batch_size=insert_batch_size
        )
        if hasattr(self.store, "bulk_insert_iter"):
            return self.store.bulk_insert_iter(
                self.graph_name,
                triple_iter,
                batch_size=insert_batch_size,
                on_progress=on_progress,
            )
        triples = list(triple_iter)
        return self.store.insert_triples(
            self.graph_name,
            triples,
            batch_size=min(insert_batch_size, 500),
            on_progress=on_progress,
        )

    def _apply_via_synced_pipeline(self) -> bool:
        """Lakebase managed-synced apply path -- triples never enter the app.

        Steps:

        1. Build the synced UC FQN from ``engine_config.sync_uc_catalog``.
        2. ``CREATE SCHEMA IF NOT EXISTS`` in Unity Catalog.
        3. ``SyncedTableManager.ensure`` — registers the synced table.
        4. ``ensure_synced_companion`` — Postgres companion table.
        5. ``trigger_and_wait`` — wait until sync reaches an online state.
        6. ``ensure_synced_union_view`` — union view over ``_sync`` ∪ companion.
        7. ``TRUNCATE`` the companion for a clean reasoning slate.
        """
        from back.core.errors import InfrastructureError
        from back.core.graphdb.lakebase.LakebaseFlatStore import (
            resolve_sync_uc_fallback_catalog,
        )

        t0 = time.time()
        try:
            mgr = self.store.synced_manager()
            fallback_cat = resolve_sync_uc_fallback_catalog(
                self.domain, self.settings, self.delta_cfg
            )
            synced_uc = self.store.synced_uc_name(
                self.graph_name, fallback_catalog=fallback_cat
            )
            logger.info(
                "[DT-BUILD %s] Managed-sync registers UC synced table at %s "
                "(graph_engine_config.sync_uc_catalog=%r; fallback_catalog=%r; "
                "UC schema segment=%s)",
                self.task_id,
                synced_uc,
                (self.store.sync_uc_catalog or "").strip() or None,
                fallback_cat or None,
                self.store.graph_schema,
            )
        except InfrastructureError as exc:
            logger.error(
                "[DT-BUILD %s] managed_synced setup failed: %s",
                self.task_id,
                exc,
            )
            self.tm.fail_task(self.task_id, str(exc))
            return False

        if not self.is_api:
            self.tm.update_progress(
                self.task_id, 45, "Lakebase managed-synced — registering pipeline…"
            )
        try:
            from back.core.graphdb.lakebase._sync_uc_schema import (
                ensure_uc_schema_for_synced_table_fqn,
            )

            ensure_uc_schema_for_synced_table_fqn(
                self.source_client,
                synced_uc,
                task_log_prefix=f"[DT-BUILD {self.task_id}]",
            )
            mgr.ensure(
                synced_uc,
                source_table_full_name=self.view_table,
                primary_key_columns=["subject", "predicate", "object"],
                sync_mode=self.store.sync_table_mode,
            )
            self.store.ensure_synced_companion(self.graph_name)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[DT-BUILD %s] failed to register synced table %s: %s",
                self.task_id,
                synced_uc,
                exc,
            )
            self.tm.fail_task(
                self.task_id,
                f"Could not register Lakebase synced table: {exc}",
            )
            return False

        if not self.is_api:
            self.tm.update_progress(
                self.task_id,
                55,
                "Lakebase managed-synced — syncing graph from Delta…",
            )
        try:
            state = mgr.trigger_and_wait(
                synced_uc, timeout_s=self.store.sync_timeout_s
            )
            logger.info(
                "[DT-BUILD %s] synced table %s reached state=%s in %.1fs",
                self.task_id,
                synced_uc,
                state,
                time.time() - t0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[DT-BUILD %s] synced refresh failed for %s: %s",
                self.task_id,
                synced_uc,
                exc,
            )
            self.tm.fail_task(
                self.task_id, f"Lakebase sync did not complete: {exc}"
            )
            return False

        try:
            self.store.ensure_synced_union_view(self.graph_name)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[DT-BUILD %s] failed to create union view for %s: %s",
                self.task_id,
                synced_uc,
                exc,
            )
            self.tm.fail_task(
                self.task_id,
                f"Could not create Lakebase union view after sync: {exc}",
            )
            return False

        try:
            self.store.truncate_companion(self.graph_name)
            logger.info(
                "[DT-BUILD %s] truncated companion table for %s "
                "(full rebuild — reasoning starts clean)",
                self.task_id,
                self.graph_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[DT-BUILD %s] could not truncate companion (non-fatal): %s",
                self.task_id,
                exc,
            )

        try:
            self.triple_count = self._count_view_triples()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[DT-BUILD %s] could not count VIEW triples post-sync: %s",
                self.task_id,
                exc,
            )
            self.triple_count = 0

        if not self.is_api:
            self.tm.update_progress(
                self.task_id, 90, f"Synced {self.triple_count} triples"
            )
        return True

    def _populate_session_cache(self) -> None:
        from back.objects.digitaltwin.DigitalTwin import DigitalTwin

        try:
            final_count = self.triple_count
            build_stamp = self.domain.triplestore.get("build_last_update")

            status_cache = {
                "success": True,
                "has_data": final_count > 0,
                "count": final_count,
                "view_table": self.view_table,
                "graph_name": self.graph_name,
            }
            if build_stamp and final_count > 0:
                status_cache["last_modified"] = build_stamp

            try:
                graph_engine = DigitalTwin.resolve_graph_engine(
                    self.domain, self.settings
                )
            except Exception:  # noqa: BLE001
                graph_engine = "lakebase"

            local_lbug_exists = final_count > 0
            local_path = ""
            registry_lbug_path = ""

            dt = DigitalTwin(self.domain)

            existence_cache = {
                "view_exists": True,
                "view_table": self.view_table,
                "graph_name": self.graph_name,
                "graph_engine": graph_engine,
                "local_lbug_exists": local_lbug_exists,
                "lakebase_table_exists": local_lbug_exists,
                "local_lbug_path": local_path,
                "registry_lbug_exists": None,
                "registry_lbug_path": registry_lbug_path,
                "registry_archive_applicable": False,
                "last_built": self.domain.last_build,
                "last_update": self.domain.last_update,
            }

            dt.set_ts_cache("status", status_cache)
            dt.set_ts_cache("dt_existence", existence_cache)
            logger.debug(
                "Build cache populated: count=%d engine=%s local_lbug_exists=%s",
                final_count,
                graph_engine,
                local_lbug_exists,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not populate DT session cache: %s", exc)

    def _complete_task(self) -> None:
        duration = time.time() - self.start_time
        logger.info(
            "[DT-BUILD %s] DONE kind=%s domain=%s triples=%d "
            "duration=%.2fs phases={%s}",
            self.task_id,
            self.build_kind,
            self.domain_name,
            self.triple_count,
            duration,
            ", ".join(f"{k}={v:.2f}s" for k, v in self.phase_times.items())
            or "n/a",
        )

        result_data: Dict[str, Any] = {
            "triple_count": self.triple_count,
            "view_table": self.view_table,
            "graph_name": self.graph_name,
            "build_mode": "full",
            "duration_seconds": duration,
        }
        if not self.is_api:
            result_data["phase_times"] = self.phase_times

        msg = f"Full rebuild: {self.triple_count} triples in {duration:.1f}s"
        self.tm.complete_task(self.task_id, result=result_data, message=msg)

    def _fail_unexpected(self, exc: Exception) -> None:
        duration = time.time() - self.start_time
        logger.exception(
            "[DT-BUILD %s] FAILED kind=%s domain=%s after %.2fs: %s",
            self.task_id,
            self.build_kind,
            self.domain_name,
            duration,
            exc,
        )
        if self.is_api:
            self.tm.fail_task(self.task_id, str(exc))
        else:
            self.tm.fail_task(self.task_id, "Triple store sync failed")
