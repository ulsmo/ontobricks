# OntoBricks — Release Notes V0.5.1

**Release window:** June, 2026
**Type:** Patch release (bug fix + reliability hardening — no schema migration required)
**Test status:** touched-area suites green (SQL warehouse + build pipeline + registry store: 93 passing; review/lifecycle green). Full multi-tier run unchanged except pre-existing e2e/MCP ordering flakes that require a live app server.

---

## Highlights

- **Bug fix — "Submit for review" no longer blocked on a built Digital Twin.** Interactive ("Build" button) and external API builds now persist `last_build` to the registry version record, the same way scheduled builds already did. The Validation page's Submit gate and the lifecycle guard read that field, so a built DRAFT version can now be submitted for review.
- **Reliability — SQL warehouse pool survives a server-side session drop.** A stale pooled connection (warehouse auto-stop / idle disconnect) is now detected and retried once on a fresh connection instead of crashing a long build.
- **Reliability — Digital Twin builds fail loudly on a broken triple count** instead of silently reporting a "healthy" zero-triple build.

---

## Bug Report — "Submit for Review" blocked despite a built Digital Twin

### Symptom

On the Domain → **Validation** page, a DRAFT version whose Digital Twin was
already built (the page's Consistency-checks panel shows a green
**"Digital Twin built"** tick, with thousands of triples in the view) was
still blocked from **Submit for review** with the banner:

> "This version has never been built. Run a Digital Twin build first."

The lifecycle stayed stuck at Draft and Submit was disabled. The two
indicators on the same page disagreed:

| Indicator | Source it reads | Result |
| --- | --- | --- |
| "Digital Twin built" (green tick) | Live triplestore — graph triple count + view existence (`compute_dtwin_indicator`) | Built |
| "Never been built" banner / Submit gate | Registry version record field `info.last_build` | Empty -> blocked |

### Root cause

`read_version` sources `last_build` from the dedicated
`domain_versions.last_build` **column**, and the Submit gate
(`ReviewService.review_detail`: `can_submit = ... and bool(last_build)`) plus
the lifecycle guard (`version_lifecycle`, DRAFT -> IN-REVIEW) both depend on it.

Only the **scheduled** build path persisted that field
(`scheduler._persist_domain_metadata` -> `write_version`). The **interactive**
and **API** build paths stamped `last_build` on the in-memory session only
(`dtwin.py` -> `domain.save()`), never on the registry version record. The
build's success path recorded a `build_runs` trace but did not update
`last_build`. So an interactive build left the column empty and Submit stayed
blocked, even though the live triplestore (read by the green tick) was healthy.

Two secondary issues surfaced during the investigation and are hardened here:

1. **Stale pooled SQL connection** — `SQLWarehouse` validated pooled
   connections by age only, with no liveness check. A build spanning a
   server-side session drop reused a dead connection, surfacing as
   `'NoneType' object has no attribute 'request'` from `unified_http_client`.
2. **Triple count swallowed failures to `0`** — `_count_view_triples` returned
   `0` on any error, so a missing view / transient failure was indistinguishable
   from a genuinely empty view and could be reported as a "successful"
   zero-triple build.

### The fix

1. New `RegistryStore.update_last_build(folder, version, ts)` — a targeted
   single-row `UPDATE` of `domain_versions.last_build` (with a `jsonb_set`
   mirror into the `info` blob), keyed by registry/folder/version. It does not
   rewrite the full version document, so it never clobbers concurrent session
   edits. Cache is invalidated on success.
   (`store/base.py`, `store/lakebase/store.py`, `RegistryService.py`)
2. The build success path (`_BuildPipeline._complete_task`) now calls a
   best-effort `_persist_last_build(ts)` that stamps both the registry version
   record and the in-process session, reusing the same folder/version
   derivation as the build-run trace. It never fails a healthy build.
   (`digitaltwin/_build_pipeline.py`)
3. `SQLWarehouse` now splits connection checkout/checkin and adds a one-shot
   retry: a **reused** pooled connection that fails with a connection-level
   error (including the `NoneType ... request` symptom) is discarded and the
   operation retried once on a fresh connection. The streaming `iter_rows`
   path is intentionally not retried.
   (`core/databricks/SQLWarehouse.py`)
4. `_count_view_triples` now raises `InfrastructureError` on a failed count
   (view missing / transient error). A successful `COUNT` of `0` is still a
   genuinely empty view (non-fatal upstream); only real failures surface as an
   error so a broken build cannot be misreported as healthy.
   (`digitaltwin/_build_pipeline.py`)

Scheduled builds use a separate code path, so they are unaffected and there is
no double-stamping.

---

## How to apply and redeploy

This is a **code-only** patch. The `domain_versions.last_build` column already
exists in the 0.5.x schema, so **no Lakebase migration or bootstrap is
required**.

1. **Pull the fix** onto the deployment host:

```bash
git pull            # or: git checkout v0.5.1
```

2. **Redeploy the app + MCP server** via the Databricks Asset Bundle
   orchestrator (syncs files, renders `app.yaml`, `databricks bundle deploy`,
   bootstraps perms, restarts the app):

```bash
make deploy
```

   Optional dry run first (preflight + validate + resource checks, no changes):

```bash
make deploy-dry-run
```

3. **Verify.** Run a Digital Twin build from the **Build** button on a DRAFT
   version, then open Domain → **Validation**: the "never been built" banner
   clears and **Submit for review** is enabled. Re-running the unit suites for
   the touched areas stays green:

```bash
python -m pytest tests/units/core/test_sql_warehouse.py \
  tests/back/core/digitaltwin/test_build_pipeline_units.py \
  tests/units/registry/test_registry_store.py -q
```

### Unblocking a version already affected (no rebuild)

For a domain whose view is healthy but whose `last_build` was left empty by a
pre-0.5.1 interactive build, you have two options without redeploying:

- **Re-run a build through the fixed path** (after redeploy): one more click of
  **Build** stamps `last_build` correctly.
- **Trigger a scheduled "Run now"**: the scheduler path already stamps
  `last_build` and invalidates the app cache. Create a schedule for the domain
  version (any interval), click **Run now**, then optionally delete the
  schedule. (Re-runs the full build.)
- **Surgical SQL stamp** (no rebuild) directly against the registry Postgres
  schema — bypasses the app cache, so the Validation page may show stale state
  until the cache TTL expires or the app restarts:

```sql
UPDATE <schema>.domain_versions dv
SET last_build = now()::text
FROM <schema>.domains d
WHERE dv.domain_id = d.id
  AND d.folder  = '<domain_folder>'
  AND dv.version = '<version>';
```

---

## Upgrade Notes

- **No migration.** Patch is application code only; the `last_build` column is
  unchanged. A plain `make deploy` is sufficient.
- **Interactive builds now write the registry.** After upgrade, every Build
  (UI or API) stamps `last_build`; previously only scheduled builds did.
- **Builds fail more honestly.** A build that cannot count its view's triples
  now fails with a clear error instead of completing as a zero-triple "success".
