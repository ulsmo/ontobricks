"""
End-to-end test fixtures.

Starts a Uvicorn server on localhost:18765 backed by **real Databricks
integration-workspace credentials**, waits for it to be healthy, then
provides a Playwright browser and page to each test.

Default behaviour: the conftest mints a workspace OAuth token from the
Databricks CLI profile named by ``ONTOBRICKS_E2E_PROFILE`` (default
``fevm-ontobricks-int``) and exports it as ``DATABRICKS_TOKEN``/``HOST``
into the subprocess. This makes Databricks-dependent pages
(``/dtwin/``, ``/domain``, ``/resolve``) load instead of hanging.

Escape hatches (env vars):

* ``ONTOBRICKS_E2E_PROFILE`` — which CLI profile to mint a token from.
* ``ONTOBRICKS_E2E_WAREHOUSE_ID`` — override the int warehouse.
* ``ONTOBRICKS_E2E_FAKE_CREDS=1`` — disable the auto-mint and fall
  back to the original fake env (``test.databricks.com`` etc.). Useful
  for tests that should NOT touch a real workspace; in this mode the
  6 Databricks-dependent tests will time out.
* ``DATABRICKS_HOST`` / ``DATABRICKS_TOKEN`` already set in the
  environment — respected (no auto-mint).

If the CLI cannot mint a token AND ``ONTOBRICKS_E2E_FAKE_CREDS`` is not
set, the whole suite is skipped with a clear message — better than
running with fake creds and producing 6 confusing timeouts.

Usage:
    .venv/bin/python -m pytest tests/e2e/ -v
"""

import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest


E2E_PORT = 18765
E2E_BASE = f"http://localhost:{E2E_PORT}"

# Default integration workspace settings. Override via the
# ``ONTOBRICKS_E2E_*`` env vars documented at the top of this module.
DEFAULT_E2E_PROFILE = "fevm-ontobricks-int"
DEFAULT_E2E_HOST = "https://fevm-ontobricks-int.cloud.databricks.com"
DEFAULT_E2E_WAREHOUSE_ID = "fcdf5a06992ad225"

_server_proc = None


def _mint_workspace_token(profile: str) -> tuple[str, str] | None:
    """Mint a workspace OAuth token from the named Databricks CLI profile.

    Returns ``(host, access_token)`` on success, ``None`` if the CLI is
    not installed, the profile does not exist, or token minting fails.
    All failures are silent — the caller decides whether to skip or
    fall back to fake creds.
    """
    try:
        token_proc = subprocess.run(
            ["databricks", "auth", "token", "--profile", profile],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    try:
        token = json.loads(token_proc.stdout).get("access_token")
    except json.JSONDecodeError:
        return None
    if not token:
        return None

    # Resolve the profile's host (from `databricks auth describe`).
    try:
        desc_proc = subprocess.run(
            ["databricks", "auth", "describe", "--profile", profile, "-o", "json"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        host = json.loads(desc_proc.stdout).get("details", {}).get("host", "")
    except (subprocess.SubprocessError, json.JSONDecodeError):
        host = ""
    if not host:
        # Fall back to the well-known default for the int profile.
        host = DEFAULT_E2E_HOST
    return host.rstrip("/"), token


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) != 0


def _wait_for_server(port: int, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) == 0:
                return
        time.sleep(0.25)
    raise RuntimeError(f"Server on port {port} did not start within {timeout}s")


def _kill_server():
    global _server_proc
    if _server_proc and _server_proc.poll() is None:
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
    _server_proc = None


@pytest.fixture(scope="session", autouse=True)
def _set_env():
    """Resolve Databricks env vars for the server subprocess.

    Default mode: mint a workspace OAuth token from the Databricks CLI
    profile named by ``ONTOBRICKS_E2E_PROFILE`` (default
    ``fevm-ontobricks-int``) and export it as ``DATABRICKS_TOKEN`` /
    ``DATABRICKS_HOST`` so the local uvicorn talks to the real int
    workspace.

    Respects any pre-set ``DATABRICKS_HOST``/``DATABRICKS_TOKEN`` (so
    CI can pass workspace creds via env). Falls back to fake values
    only if ``ONTOBRICKS_E2E_FAKE_CREDS=1`` is set. Otherwise, if
    neither pre-set creds nor a working CLI profile exist, skips the
    whole suite with a clear message.
    """
    fake_only = os.environ.get("ONTOBRICKS_E2E_FAKE_CREDS") == "1"

    # Always set SECRET_KEY.
    os.environ.setdefault("SECRET_KEY", "test-secret-key-e2e")

    if fake_only:
        os.environ.setdefault("DATABRICKS_HOST", "https://test.databricks.com")
        os.environ.setdefault("DATABRICKS_TOKEN", "test-token")
        os.environ.setdefault("DATABRICKS_SQL_WAREHOUSE_ID", "test-warehouse")
        return

    # If the caller already exported real creds, respect them.
    have_host = bool(os.environ.get("DATABRICKS_HOST"))
    have_token = bool(os.environ.get("DATABRICKS_TOKEN"))
    if have_host and have_token:
        os.environ.setdefault(
            "DATABRICKS_SQL_WAREHOUSE_ID",
            os.environ.get(
                "ONTOBRICKS_E2E_WAREHOUSE_ID", DEFAULT_E2E_WAREHOUSE_ID
            ),
        )
        return

    # Auto-mint from the configured profile.
    profile = os.environ.get("ONTOBRICKS_E2E_PROFILE", DEFAULT_E2E_PROFILE)
    minted = _mint_workspace_token(profile)
    if minted is None:
        pytest.skip(
            f"E2E suite needs real Databricks creds. Either:\n"
            f"  - run `databricks auth login --profile {profile} "
            f"--host {DEFAULT_E2E_HOST}` and retry, or\n"
            f"  - export DATABRICKS_HOST + DATABRICKS_TOKEN explicitly, "
            f"or\n"
            f"  - set ONTOBRICKS_E2E_FAKE_CREDS=1 to use fake values "
            f"(note: 6 Databricks-dependent tests will time out)."
        )

    host, token = minted
    os.environ["DATABRICKS_HOST"] = host
    os.environ["DATABRICKS_TOKEN"] = token
    os.environ.setdefault(
        "DATABRICKS_SQL_WAREHOUSE_ID",
        os.environ.get(
            "ONTOBRICKS_E2E_WAREHOUSE_ID", DEFAULT_E2E_WAREHOUSE_ID
        ),
    )


@pytest.fixture(scope="session")
def live_server(_set_env):
    """Start OntoBricks in a subprocess to isolate from test process env changes."""
    global _server_proc

    if not _port_free(E2E_PORT):
        pytest.skip(f"Port {E2E_PORT} is already in use -- cannot start test server")

    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    src_dir = os.path.join(repo_root, "src")
    env = {**os.environ}
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    # The unit-test harness (tests/conftest.py) forces CSRF_DISABLED=1 so
    # TestClient assertions don't need tokens; the E2E subprocess, however,
    # is meant to mirror real deployment where CSRF is live.  Strip the
    # override so tests/e2e/test_permissions_flows.py can actually exercise
    # the rejection branch.
    env.pop("CSRF_DISABLED", None)

    # Capture stdout/stderr into a session log so startup failures are
    # debuggable. Path is printed on failure.
    log_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_e2e_server.log"
    )
    log_fh = open(log_path, "w")
    _server_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "shared.fastapi.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(E2E_PORT),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    atexit.register(_kill_server)

    try:
        _wait_for_server(E2E_PORT)
    except RuntimeError:
        _kill_server()
        log_fh.close()
        with open(log_path) as f:
            tail = f.read()[-4000:]
        pytest.fail(
            f"Failed to start test server. uvicorn log tail:\n{tail}"
        )

    yield E2E_BASE
    _kill_server()


@pytest.fixture(scope="session")
def browser_instance():
    """Launch a Playwright Chromium browser for the session."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    yield browser
    browser.close()
    pw.stop()


@pytest.fixture
def page(browser_instance, live_server):
    """Provide a fresh browser page pointed at the live server."""
    ctx = browser_instance.new_context()
    pg = ctx.new_page()
    pg.base_url = live_server
    yield pg
    pg.close()
    ctx.close()
