#!/usr/bin/env bash
set -euo pipefail

# ── OntoBricks — Lakebase Schema Permission Bootstrap ───────────────
# A Databricks Apps service principal is created without any privileges
# on Lakebase Postgres objects, even when the app's ``postgres:`` resource
# binding is wired correctly. The first time the app tries to read a schema
# it silently sees an empty information_schema — the canonical false negative
# for a missing USAGE grant.
#
# This script connects to the Lakebase instance with your *human*
# credentials (as the schema owner), looks up each app's service
# principal client id, and grants it the privileges OntoBricks needs:
#
#   - CAN_USE on the Lakebase database instance (control-plane)
#   - USAGE + CREATE on the Postgres schema (data-plane)
#   - SELECT/INSERT/UPDATE/DELETE on every existing table
#   - USAGE/SELECT/UPDATE on every existing sequence (bigserial PKs)
#   - The same set as ALTER DEFAULT PRIVILEGES so future tables inherit
#
# Idempotent — re-running is a no-op for objects that already carry the
# privileges.
#
# ── OntoBricks uses up to THREE Postgres schemas that each need this grant ──
#
#   1. Registry schema  (e.g. ontobricks_registry)
#      Instance : deploy.config.sh → LAKEBASE_BOOTSTRAP_INSTANCE
#      Database : deploy.config.sh → LAKEBASE_BOOTSTRAP_DATABASE
#      Schema   : deploy.config.sh → LAKEBASE_BOOTSTRAP_SCHEMA
#      → Run once after "Settings > Registry > Initialize"
#
#   2. Graph schema  (e.g. ontobricks_graph)
#      Instance : deploy.config.sh → LAKEBASE_GRAPH_PROJECT  (defaults to registry instance)
#      Database : deploy.config.sh → LAKEBASE_GRAPH_DATABASE (defaults to registry database)
#      Schema   : deploy.config.sh → LAKEBASE_GRAPH_SCHEMA
#      → Run once after the first "Build" in Digital Twin
#      → If the Graph DB is on a DIFFERENT Lakebase instance, set
#        LAKEBASE_GRAPH_PROJECT / LAKEBASE_GRAPH_BRANCH / LAKEBASE_GRAPH_DATABASE
#        in deploy.config.sh and pass them here via -i / -b / -d.
#
#   3. Sync schema  (e.g. ontobricks — mirrors the UC registry schema segment)
#      Same instance/database as the Graph DB (Lakebase creates it there).
#      Schema   : deploy.config.sh → LAKEBASE_SYNC_SCHEMA
#      → Only needed when sync_mode = managed_synced.
#      → Run after the first Lakeflow snapshot has completed (Lakebase
#        creates the schema automatically; this script grants USAGE + DML).
#
#   ``scripts/deploy.sh`` calls this script three times automatically when
#   LAKEBASE_GRAPH_SCHEMA and LAKEBASE_SYNC_SCHEMA are set. You can also
#   run it manually:
#
#     # Registry
#     scripts/bootstrap-lakebase-perms.sh \
#       -i ontobricks-app -b production -d ontobricks_registry \
#       -s ontobricks_registry -a ontobricks-030 -a mcp-ontobricks
#
#     # Graph DB on SAME instance
#     scripts/bootstrap-lakebase-perms.sh \
#       -i ontobricks-app -b production -d ontobricks_registry \
#       -s ontobricks_graph -a ontobricks-030 -a mcp-ontobricks
#
#     # Graph DB on DIFFERENT instance
#     scripts/bootstrap-lakebase-perms.sh \
#       -i ontobricks-graph-instance -b production -d ontobricks_graph_db \
#       -s ontobricks_graph -a ontobricks-030 -a mcp-ontobricks
#
#     # Sync schema (managed_synced mode only, same instance as Graph DB)
#     scripts/bootstrap-lakebase-perms.sh \
#       -i ontobricks-app -b production -d ontobricks_registry \
#       -s ontobricks -a ontobricks-030 -a mcp-ontobricks
#
# Prerequisites:
#   - Databricks CLI authenticated against the same workspace as the apps
#   - ``psql`` on PATH (libpq client; ``brew install libpq && brew link --force libpq``)
#   - You own the schema (or otherwise have GRANT OPTION on it).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

INSTANCE="${INSTANCE:-ontobricks-app}"
BRANCH="${BRANCH:-${LAKEBASE_BRANCH:-production}}"
DATABASE="${DATABASE:-ontobricks_registry}"
SCHEMA="${SCHEMA:-ontobricks_registry}"
APPS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--instance) INSTANCE="$2"; shift 2 ;;
        -b|--branch)   BRANCH="$2"; shift 2 ;;
        -d|--database) DATABASE="$2"; shift 2 ;;
        -s|--schema)   SCHEMA="$2"; shift 2 ;;
        -a|--app)      APPS+=("$2"); shift 2 ;;
        -h|--help)
            sed -n '2,32p' "$0"
            exit 0 ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit 2 ;;
    esac
done

if [[ ${#APPS[@]} -eq 0 ]]; then
    # Defaults to the Lakebase-backed dev app only. The production
    # ``ontobricks`` app currently runs on the Volume backend and
    # would not benefit from these grants — pass ``-a ontobricks``
    # explicitly when you migrate it. ``APP_NAME`` / ``MCP_APP_NAME``
    # come from ``scripts/deploy.config.sh`` when invoked via
    # ``scripts/deploy.sh``.
    APPS=("${APP_NAME:-ontobricks-030}" "${MCP_APP_NAME:-mcp-ontobricks}")
fi

for cmd in databricks psql python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: '$cmd' not on PATH" >&2
        exit 1
    fi
done

if ! databricks current-user me >/dev/null 2>&1; then
    echo "ERROR: Databricks CLI not authenticated." >&2
    echo "       Run: databricks auth login --host https://<workspace>" >&2
    exit 1
fi

PGUSER="$(databricks current-user me 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("userName",""))')"
if [[ -z "$PGUSER" ]]; then
    echo "ERROR: Could not resolve your Databricks userName for PGUSER." >&2
    exit 1
fi

# Resolve the project's primary endpoint via the Postgres API.
# OntoBricks targets Lakebase Autoscaling exclusively — the legacy
# ``/api/2.0/database/instances/<name>`` endpoint 404s on Autoscaling-
# only projects, so we resolve the endpoint from the configured
# project+branch pair.
INSTANCE_NAME="$INSTANCE"
BRANCH_NAME="$BRANCH"
ENDPOINT_INFO="$(INSTANCE_NAME="$INSTANCE_NAME" BRANCH_NAME="$BRANCH_NAME" python3 - <<'PY'
import json, os, subprocess, sys

instance = os.environ["INSTANCE_NAME"]
branch = os.environ["BRANCH_NAME"]


def api_get(path):
    out = subprocess.run(
        ["databricks", "api", "get", path],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


branch_path = f"projects/{instance}/branches/{branch}"
endpoints = (
    api_get(f"/api/2.0/postgres/{branch_path}/endpoints") or {}
).get("endpoints") or []
for ep in endpoints:
    hosts = (ep.get("status") or {}).get("hosts") or {}
    host = (hosts.get("host") or "").strip()
    endpoint_path = ep.get("name") or ""
    if host and endpoint_path:
        print(host)
        print(endpoint_path)
        sys.exit(0)
sys.exit(1)
PY
)"
if [[ -z "$ENDPOINT_INFO" ]]; then
    echo "ERROR: Could not resolve a primary endpoint for Lakebase Autoscaling project '${INSTANCE}' on branch '${BRANCH}'." >&2
    echo "       Check 'databricks api get /api/2.0/postgres/projects/${INSTANCE}/branches/${BRANCH}/endpoints'" >&2
    echo "       and confirm project/branch values match the app postgres resource binding." >&2
    exit 1
fi
PGHOST="$(printf '%s\n' "$ENDPOINT_INFO" | sed -n 1p)"
ENDPOINT_PATH="$(printf '%s\n' "$ENDPOINT_INFO" | sed -n 2p)"

echo "=== OntoBricks — Lakebase Schema Permission Bootstrap ==="
echo "Project  : ${INSTANCE} (${PGHOST})"
echo "Branch   : ${BRANCH}"
echo "Endpoint : ${ENDPOINT_PATH}"
echo "Database : ${DATABASE}"
echo "Schema   : ${SCHEMA}"
echo "Acting as: ${PGUSER}"
echo "Apps     : ${APPS[*]}"
echo

# Mint a Lakebase JWT via the Autoscaling Postgres API. The legacy
# ``/api/2.0/database/credentials`` mint cannot scope tokens to
# Autoscaling-only project endpoints.
PGPASSWORD="$(databricks api post /api/2.0/postgres/credentials \
    --json "{\"endpoint\":\"${ENDPOINT_PATH}\"}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))')"
if [[ -z "$PGPASSWORD" ]]; then
    echo "ERROR: Failed to mint a Lakebase JWT for instance '${INSTANCE}'." >&2
    exit 1
fi
export PGPASSWORD

PGCONN="host=${PGHOST} port=5432 user=${PGUSER} dbname=${DATABASE} sslmode=require"

# Ensure the target schema actually exists. If not, the operator
# probably ran the script before deploying / initialising the app.
if ! psql "$PGCONN" -tAc "SELECT 1 FROM information_schema.schemata WHERE schema_name='${SCHEMA}'" \
        | grep -q 1; then
    echo "ERROR: Schema '${SCHEMA}' does not exist in database '${DATABASE}'." >&2
    echo "       Initialise the registry from the OntoBricks Settings UI first." >&2
    exit 1
fi

FAILED=0
for app in "${APPS[@]}"; do
    sp_id="$(databricks apps get "$app" -o json 2>/dev/null \
        | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(2)
print(d.get("service_principal_client_id") or "")' 2>/dev/null || true)"

    if [[ -z "$sp_id" || "$sp_id" == "None" ]]; then
        echo "  [$app] SKIP — could not resolve service principal (app may not exist yet)"
        FAILED=$((FAILED+1))
        continue
    fi

    echo "  [$app] service principal: $sp_id"

    # ── 1. Control-plane: CAN_USE on the Lakebase database instance ─────────
    # Required for the app SP to call /api/2.0/database/synced_tables.
    # The CLI `permissions update` does not support database-instances; use
    # the raw API endpoint keyed by instance name.
    echo "  [$app] granting CAN_USE on Lakebase instance '${INSTANCE}'..."
    if databricks api patch "/api/2.0/permissions/database-instances/${INSTANCE}" \
        --json "{\"access_control_list\": [{\"service_principal_name\": \"${sp_id}\", \"permission_level\": \"CAN_USE\"}]}" \
        >/dev/null 2>&1; then
        echo "  [$app] ✓ CAN_USE granted on instance '${INSTANCE}'"
    else
        echo "  [$app] ⚠ CAN_USE grant failed (may already be set or you lack CAN_MANAGE on the instance)"
    fi

    # ── 2. Postgres schema: USAGE + DML ──────────────────────────────────────
    if ! psql "$PGCONN" -v ON_ERROR_STOP=1 -q <<SQL
GRANT USAGE, CREATE ON SCHEMA "${SCHEMA}" TO "${sp_id}";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "${SCHEMA}" TO "${sp_id}";
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA "${SCHEMA}" TO "${sp_id}";
ALTER DEFAULT PRIVILEGES IN SCHEMA "${SCHEMA}"
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "${sp_id}";
ALTER DEFAULT PRIVILEGES IN SCHEMA "${SCHEMA}"
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO "${sp_id}";
SQL
    then
        echo "  [$app] ✗ Postgres GRANT failed (need ownership of schema '${SCHEMA}' or GRANT OPTION)"
        FAILED=$((FAILED+1))
        continue
    fi

    has_usage="$(psql "$PGCONN" -tAc \
        "SELECT has_schema_privilege('${sp_id}', '${SCHEMA}', 'USAGE')" \
        | tr -d '[:space:]')"
    # Check table-level SELECT on the first available table in the schema
    # (the `registries` table only exists in the registry schema, not the graph schema).
    first_table="$(psql "$PGCONN" -tAc \
        "SELECT tablename FROM pg_tables WHERE schemaname='${SCHEMA}' LIMIT 1" \
        | tr -d '[:space:]')"
    if [[ -n "$first_table" ]]; then
        has_select="$(psql "$PGCONN" -tAc \
            "SELECT has_table_privilege('${sp_id}', '${SCHEMA}.${first_table}', 'SELECT')" \
            | tr -d '[:space:]')"
    else
        # No tables yet (schema exists but is empty) — USAGE is enough to verify.
        has_select="t"
    fi
    if [[ "$has_usage" == "t" && "$has_select" == "t" ]]; then
        echo "  [$app] ✓ granted USAGE + DML on schema '${SCHEMA}'"
    else
        echo "  [$app] ✗ verify failed (USAGE=$has_usage, SELECT=${first_table:-<no tables>}=$has_select)"
        FAILED=$((FAILED+1))
    fi
done

echo
if [[ $FAILED -eq 0 ]]; then
    echo "=== Done — Lakebase schema bootstrap complete ==="
    exit 0
else
    echo "=== Done with $FAILED failure(s) — see messages above ==="
    exit 1
fi
