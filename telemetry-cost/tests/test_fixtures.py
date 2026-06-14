"""Fixture guarantees: determinism, per-stream formats, and classifier rules.

The eval harness treats the four committed fixture streams as ground truth,
so these tests pin the properties the published numbers depend on:

  1. log-simulators is byte-deterministic for a given --seed/--start-time
     (two runs, identical bytes) — small counts, so this stays fast;
  2. each committed stream is the format the classifier expects;
  3. scripts/classify.py applies the DESIGN.md classification rules.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import classify  # noqa: E402

FIXTURES = ROOT / "fixtures"

# Same source-selection logic as scripts/make_fixtures.sh: prefer the public
# repo, allow a local checkout override for offline runs.
GIT_SRC = "git+https://github.com/expanso-io/log-simulators"
LOCAL_SRC = Path.home() / "code" / "log-simulators"

CRI_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+ (stdout|stderr) [FP] ")
NCSA_RE = re.compile(r'^\S+ \S+ \S+ \[[^\]]+\] "[^"]*" \d{3} \S+')

pytestmark = pytest.mark.skipif(
    shutil.which("uvx") is None,
    reason="uv not installed (https://docs.astral.sh/uv/)",
)


def _logsim_src() -> str:
    import os
    src = os.environ.get("LOGSIM_SRC")
    if src:
        return src
    if LOCAL_SRC.is_dir():
        return str(LOCAL_SRC)  # offline-friendly; same code as the git source
    return GIT_SRC


def _run_sim(tool: str, out: Path, *extra: str) -> bytes:
    cmd = [
        "uvx", "--from", _logsim_src(), tool,
        "--seed", "7", "--count", "200",
        "--backfill", "10m", "--start-time", "2026-06-10T16:00:00+00:00",
        *extra,
        "--output", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    return out.read_bytes()


# --- 1. determinism --------------------------------------------------------------

@pytest.mark.parametrize(
    ("tool", "extra"),
    [
        ("logsim-app", ()),
        ("logsim-k8s", ("--scenario", "crash-loop")),
        ("logsim-web", ()),
        ("logsim-cloud", ()),
    ],
)
def test_simulator_deterministic(tmp_path: Path, tool: str, extra: tuple) -> None:
    a = _run_sim(tool, tmp_path / "a.out", *extra)
    b = _run_sim(tool, tmp_path / "b.out", *extra)
    assert a, f"{tool} produced no output"
    assert a == b, f"{tool}: same seed must produce byte-identical output"


# --- 2. committed stream formats ---------------------------------------------------

def _lines(name: str) -> list[str]:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"fixtures/{name} not present (run scripts/make_fixtures.sh)")
    return path.read_text(encoding="utf-8").splitlines()


def test_app_stream_format() -> None:
    lines = _lines("app.ndjson")
    assert len(lines) == 6000
    for line in lines[::293]:
        ev = json.loads(line)
        assert ev["level"] in {"debug", "info", "warn", "error"}
        assert "service" in ev and "timestamp" in ev and "msg" in ev


def test_k8s_stream_format() -> None:
    lines = _lines("k8s.log")
    assert len(lines) >= 5000  # 5000 events; partial-line mechanics add P-lines
    for line in lines[::173]:
        assert CRI_RE.match(line), f"not a CRI line: {line[:80]!r}"


def test_web_stream_format() -> None:
    lines = _lines("web.log")
    assert len(lines) == 6000
    for line in lines[::293]:
        assert NCSA_RE.match(line), f"not an NCSA access line: {line[:80]!r}"


def test_cloudtrail_stream_format() -> None:
    lines = _lines("cloudtrail.ndjson")
    assert len(lines) == 1200
    for line in lines[::97]:
        ev = json.loads(line)
        assert "eventVersion" in ev and "eventSource" in ev


# --- 3. classifier rules (the eval ground-truth engine) -----------------------------

def test_classify_app_error() -> None:
    v = classify.classify(
        '{"timestamp":"2026-06-10T16:00:00Z","level":"ERROR","service":"auth",'
        '"msg":"boom","duration_ms":2500,"http":{"method":"POST","path":"/api/v1/login","status":500}}'
    )
    assert v["src"] == "app"
    assert v["level"] == "error"  # normalized lowercase
    assert v["slow"] is True
    assert v["retain"] is True  # auth service
    assert v["queried_class"] == "regular"


def test_classify_k8s_zap_dup_key() -> None:
    v = classify.classify(
        '2026-06-10T16:00:00.401573514Z stderr F '
        '{"level":"warn","ts":"2026-06-10T16:00:00.401Z",'
        '"caller":"gateway/charge.go:203","msg":"provider latency high"}'
    )
    assert v["src"] == "k8s"
    assert v["level"] == "warn"
    assert v["dup_key"] == "gateway/charge.go:203|provider latency high"


def test_classify_k8s_klog() -> None:
    v = classify.classify(
        "2026-06-10T16:00:01.999252576Z stderr F "
        "W0610 16:00:01.999252       1 reflector.go:458] watch ended"
    )
    assert v["src"] == "k8s"
    assert v["level"] == "warn"
    assert v["dup_key"] is None


def test_classify_k8s_nginx_health() -> None:
    v = classify.classify(
        '2026-06-10T16:00:03.382687151Z stdout F '
        '10.244.114.26 - - [10/Jun/2026:16:00:03 +0000] "GET /healthz HTTP/1.1" '
        '200 560 "-" "kube-probe/1.29"'
    )
    assert v["src"] == "k8s"
    assert v["is_health"] is True
    assert v["queried_class"] == "never"


def test_classify_web_levels_and_retain() -> None:
    line = ('1.2.3.4 - bob [10/Jun/2026:16:00:00 +0000] "{req} HTTP/1.1" {st} 123 '
            '"-" "Mozilla/5.0"')
    err = classify.classify(line.format(req="GET /cart", st=502))
    assert (err["src"], err["level"]) == ("web", "error")
    warn = classify.classify(line.format(req="GET /nope", st=404))
    assert warn["level"] == "warn"
    login = classify.classify(line.format(req="POST /login", st=200))
    assert login["retain"] is True and login["queried_class"] == "regular"
    static = classify.classify(line.format(req="GET /static/css/main.css", st=200))
    assert static["queried_class"] == "never"


def test_classify_cloudtrail_and_unknown() -> None:
    ct = classify.classify('{"eventVersion":"1.08","eventSource":"ec2.amazonaws.com"}')
    assert ct["src"] == "cloudtrail" and ct["retain"] is True and ct["level"] == "info"
    unk = classify.classify("some completely freeform line")
    assert unk["src"] == "unknown" and unk["level"] == "info"
    assert unk["raw_bytes"] == len("some completely freeform line")


def test_classify_summary_consistent() -> None:
    if not all((FIXTURES / n).exists()
               for n in ("app.ndjson", "k8s.log", "web.log", "cloudtrail.ndjson")):
        pytest.skip("committed fixtures not present (run scripts/make_fixtures.sh)")
    g = classify.summarize([
        FIXTURES / "app.ndjson", FIXTURES / "k8s.log",
        FIXTURES / "web.log", FIXTURES / "cloudtrail.ndjson",
    ])
    assert g["lines"] == sum(g["by_src"].values()) == sum(g["lanes"].values())
    assert g["signal_dedup"] <= g["signal"]
    assert g["lanes"]["hot"] <= g["signal"]  # junk-overlap can only shrink hot
    assert 0.0 < g["garbage_pct"] < 100.0
    assert g["bytes"] == sum(g["lane_bytes"].values())
