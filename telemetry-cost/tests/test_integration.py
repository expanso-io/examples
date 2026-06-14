"""End-to-end integration test: costboard + local Expanso Edge + real jobs.

Boots the costboard (port 8090) and a local edge node (API on 19014, so it
never collides with a demo edge on 19010), then replays the committed
fixture streams through the deterministic file-input job variants:

1. deploy the tax pipeline   -> every raw line ships, reduction stays ~0
2. /reset, deploy the step-4 filter -> >=30% reduction, reference-based
   error retention (every app error/warn line back in hot, plus at least
   the deduped k8s/web signal and the slow requests)

Ground truth comes from scripts/classify.py — the reference implementation
of the pipelines' classification rules. Skips cleanly when the Expanso
binaries (or the jobs, still being built) are missing. Every child process
is killed in a finally block — no survivors.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import classify  # noqa: E402

FIXTURES = [
    ROOT / "fixtures" / "app.ndjson",
    ROOT / "fixtures" / "k8s.log",
    ROOT / "fixtures" / "web.log",
    ROOT / "fixtures" / "cloudtrail.ndjson",
]
BOARD_URL = "http://127.0.0.1:8090"
EDGE_API = "http://127.0.0.1:19014"
EDGE_DATA = ROOT / ".edge-data-itest"
STABLE_SECS = 3.0
TIMEOUT_SECS = 300.0

pytestmark = pytest.mark.skipif(
    shutil.which("expanso-edge") is None or shutil.which("expanso-cli") is None,
    reason="expanso-edge / expanso-cli not installed",
)


# --- tiny stdlib http helpers -------------------------------------------------

def _get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, data: bytes = b"", timeout: float = 120.0) -> None:
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait_until_up(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2.0)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise TimeoutError(f"{url} never came up")


def _lane_counts(stats: dict) -> tuple:
    return (
        stats["raw"]["bytes"], stats["raw"]["events"],
        stats["hot"]["bytes"], stats["hot"]["events"],
        stats["cold"]["bytes"], stats["cold"]["events"],
    )


def _poll_stable(initial: tuple) -> dict:
    """Wait until counters have moved past `initial` and then sat still 3s."""
    deadline = time.monotonic() + TIMEOUT_SECS
    last = initial
    last_change = time.monotonic()
    moved = False
    while time.monotonic() < deadline:
        try:
            stats = _get_json(f"{BOARD_URL}/stats")
        except (urllib.error.URLError, TimeoutError, OSError):
            # During the initial fixture flood the board can stall a poll
            # past the socket timeout; that's load, not failure.
            time.sleep(0.5)
            continue
        cur = _lane_counts(stats)
        if cur != last:
            last = cur
            last_change = time.monotonic()
            moved = True
        elif moved and time.monotonic() - last_change >= STABLE_SECS:
            return stats
        time.sleep(0.5)
    raise TimeoutError(f"costboard counters never stabilized (moved={moved}, last={last})")


def _deploy(job_file: Path) -> None:
    proc = subprocess.run(
        ["expanso-cli", "job", "deploy", str(job_file),
         "--endpoint", EDGE_API, "--force"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"deploy {job_file} failed:\n{proc.stdout}\n{proc.stderr}"


def _post_raw_baseline() -> None:
    """The raw lane baseline: each fixture stream, byte-for-byte."""
    for f in FIXTURES:
        _post(f"{BOARD_URL}/ingest/raw", f.read_bytes())


# --- fixtures / setup ----------------------------------------------------------

@pytest.fixture(scope="module")
def stack():
    if any(not f.exists() for f in FIXTURES):
        subprocess.run(["bash", str(ROOT / "scripts" / "make_fixtures.sh")],
                       cwd=ROOT, check=False)
        if any(not f.exists() for f in FIXTURES):
            pytest.skip("fixtures missing (run scripts/make_fixtures.sh)")
    for j in ("01-tax.yaml", "03-filter-step4.yaml"):
        if not (ROOT / "jobs" / j).exists():
            pytest.skip(f"jobs/{j} not built yet")
    if not (ROOT / "costboard" / "server.py").exists():
        pytest.skip("costboard/server.py not built yet")
    if not _port_free(8090):
        pytest.skip("port 8090 busy — stop the demo costboard first (just clean)")
    if not _port_free(19014):
        pytest.skip("port 19014 busy")

    # Deterministic file-input variants of the live jobs (input swap only).
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_test_jobs.py"), "--force"],
        cwd=ROOT, check=True,
    )
    shutil.rmtree(EDGE_DATA, ignore_errors=True)

    board = edge = None
    try:
        board = subprocess.Popen(
            [sys.executable, str(ROOT / "costboard" / "server.py")],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait_until_up(f"{BOARD_URL}/stats")
        edge = subprocess.Popen(
            ["expanso-edge", "run", "--local",
             "--api-listen", "127.0.0.1:19014",
             "--data-dir", str(EDGE_DATA), "--no-watch"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _wait_until_up(f"{EDGE_API}/api/v1/health")
        yield {"board": board, "edge": edge}
    finally:
        for proc in (edge, board):
            if proc is None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        shutil.rmtree(EDGE_DATA, ignore_errors=True)


# --- the test -------------------------------------------------------------------

def test_tax_then_filter(stack):
    g = classify.summarize(FIXTURES)
    n_total = g["lines"]
    n_app_error = g["app_by_level"].get("error", 0)
    assert n_total > 0 and n_app_error > 0

    # --- scenario 1: the tax — everything ships, the meter just runs --------
    _post(f"{BOARD_URL}/reset")
    _post_raw_baseline()
    baseline = _lane_counts(_get_json(f"{BOARD_URL}/stats"))
    _deploy(ROOT / "jobs" / "test" / "01-tax.yaml")
    stats = _poll_stable(baseline)

    assert stats["hot"]["events"] == n_total, (
        f"tax pipeline must ship every raw line: hot={stats['hot']['events']} != {n_total}"
    )
    assert abs(float(stats["reduction_pct"])) < 0.5, (
        f"tax pipeline reduces nothing: reduction_pct={stats['reduction_pct']}"
    )

    # --- scenario 2: edge filter step 4 --------------------------------------
    _post(f"{BOARD_URL}/reset")
    _post_raw_baseline()
    baseline = _lane_counts(_get_json(f"{BOARD_URL}/stats"))
    _deploy(ROOT / "jobs" / "test" / "03-filter-step4.yaml")
    stats = _poll_stable(baseline)

    assert stats["reduction_pct"] >= 30, (
        f"step-4 filter must cut >=30% of bytes: got {stats['reduction_pct']}%"
    )
    # App lines are JSON, so the costboard levels them natively: exact
    # reference-based retention for every app error line.
    hot_errors = stats["hot"]["events_by_level"].get("error", 0)
    assert hot_errors == n_app_error, (
        f"100% app error retention required: hot error={hot_errors}, "
        f"reference={n_app_error}"
    )
    # k8s/web signal ships as opaque raw lines: check via the reference total
    # (every error/warn with crash-loop bursts collapsed to one per dup_key,
    # plus every slow request).
    assert stats["hot"]["events"] >= g["signal_dedup"], (
        f"all reference signal must be retained: hot={stats['hot']['events']} "
        f"< signal_dedup={g['signal_dedup']}"
    )
