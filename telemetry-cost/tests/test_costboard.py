"""End-to-end tests for costboard/server.py.

Starts the real server as a subprocess on a scratch port, posts single and
batched events to every lane, and asserts the /stats byte/event/cost math
exactly, the cold-storage gzip partitions, and /reset behavior.

v2: lanes carry RAW ORIGINAL LINES — non-JSON lines (NCSA access logs,
CRI/k8s lines) are opaque events whose bytes and line counts still meter;
hot level tracking uses the parsed 'level' when a line is a JSON object
that has one, else "RAW". text/plain bodies are accepted.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

PORT = 18295
BASE = f"http://127.0.0.1:{PORT}"
REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "costboard" / "server.py"

GB = 1_000_000_000
MILLION = 1_000_000
PRESETS = {
    "_note": "test presets",
    "hot_per_gb_ingest": 0.10,
    "hot_per_million_events": 1.70,
    "cold_per_gb_month": 0.023,
}


def post(path: str, body: bytes | None = None, ctype: str | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path, data=body if body is not None else b"", method="POST"
    )
    if ctype is not None:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def get_stats() -> dict:
    with urllib.request.urlopen(BASE + "/stats", timeout=5) as resp:
        return json.loads(resp.read())


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("costboard")
    presets_path = tmp / "presets.json"
    presets_path.write_text(json.dumps(PRESETS))
    cold_dir = tmp / "cold-storage"

    proc = subprocess.Popen(
        [
            sys.executable,
            str(SERVER),
            "--port",
            str(PORT),
            "--presets",
            str(presets_path),
            "--cold-dir",
            str(cold_dir),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 10
        while True:
            try:
                get_stats()
                break
            except (urllib.error.URLError, ConnectionError):
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"server died: {proc.stderr.read().decode()}"
                    ) from None
                if time.time() > deadline:
                    raise RuntimeError("server never came up") from None
                time.sleep(0.1)
        yield {"cold_dir": cold_dir}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def event(level: str = "INFO", kind: str = "app", ts: str = "2026-06-10T14:05:00.123Z") -> dict:
    return {
        "ts": ts,
        "site": "site-03",
        "service": "payments-api",
        "level": level,
        "kind": kind,
        "message": f"{kind} line at {level}",
    }


def encode_single(ev: dict) -> bytes:
    return json.dumps(ev).encode()


def encode_ndjson(events: list) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def test_full_meter_flow(server):
    post("/reset")

    # --- raw lane: one single object + one NDJSON batch of 3 -------------
    raw_single = encode_single(event())
    raw_batch = encode_ndjson([event(), event("DEBUG", "debug"), event("WARN")])
    assert post("/ingest/raw", raw_single) == {"ok": True, "events": 1}
    assert post("/ingest/raw", raw_batch) == {"ok": True, "events": 3}
    raw_bytes = len(raw_single) + len(raw_batch)

    # --- hot lane: single ERROR + batch of INFO/WARN/ERROR ---------------
    hot_single = encode_single(event("ERROR", "error"))
    hot_batch = encode_ndjson([event("INFO"), event("WARN"), event("ERROR", "error")])
    assert post("/ingest/hot", hot_single)["events"] == 1
    assert post("/ingest/hot", hot_batch)["events"] == 3
    hot_bytes = len(hot_single) + len(hot_batch)

    # --- cold lane: batch in hour 14 + single in hour 15 ------------------
    cold_batch_events = [
        event("INFO", ts="2026-06-10T14:10:00.000Z"),
        event("WARN", ts="2026-06-10T14:55:59.999Z"),
    ]
    cold_single_event = event("INFO", ts="2026-06-10T15:01:00.000Z")
    cold_batch = encode_ndjson(cold_batch_events)
    cold_single = encode_single(cold_single_event)
    assert post("/ingest/cold", cold_batch)["events"] == 2
    assert post("/ingest/cold", cold_single)["events"] == 1
    cold_bytes = len(cold_batch) + len(cold_single)

    # --- /stats: bytes, events, levels, costs — exact ---------------------
    s = get_stats()
    assert s["raw"] == {"bytes": raw_bytes, "events": 4}
    assert s["hot"]["bytes"] == hot_bytes
    assert s["hot"]["events"] == 4
    assert s["hot"]["events_by_level"] == {"ERROR": 2, "INFO": 1, "WARN": 1}
    assert s["cold"]["bytes"] == cold_bytes
    assert s["cold"]["events"] == 3

    expected_hot_cost = (
        hot_bytes / GB * PRESETS["hot_per_gb_ingest"]
        + 4 / MILLION * PRESETS["hot_per_million_events"]
    )
    expected_cold_cost = cold_bytes / GB * PRESETS["cold_per_gb_month"]
    expected_reduction = (1 - hot_bytes / raw_bytes) * 100
    assert s["hot"]["cost_usd"] == expected_hot_cost
    assert s["cold"]["cost_usd_month"] == expected_cold_cost
    assert s["reduction_pct"] == expected_reduction
    assert s["presets"]["hot_per_gb_ingest"] == 0.10
    assert "started_at" in s

    # --- cold gzip partitions written by event ts hour --------------------
    cold_dir = server["cold_dir"]
    part14 = cold_dir / "2026" / "06" / "10" / "14" / "events-14.jsonl.gz"
    part15 = cold_dir / "2026" / "06" / "10" / "15" / "events-15.jsonl.gz"
    assert part14.is_file() and part15.is_file()
    with gzip.open(part14, "rt") as fh:
        got14 = [json.loads(line) for line in fh if line.strip()]
    with gzip.open(part15, "rt") as fh:
        got15 = [json.loads(line) for line in fh if line.strip()]
    assert got14 == cold_batch_events
    assert got15 == [cold_single_event]

    # --- /reset zeroes counters but keeps cold files ----------------------
    assert post("/reset") == {"ok": True}
    z = get_stats()
    assert z["raw"] == {"bytes": 0, "events": 0}
    assert z["hot"]["bytes"] == 0 and z["hot"]["events"] == 0
    assert z["hot"]["events_by_level"] == {}
    assert z["hot"]["cost_usd"] == 0.0
    assert z["cold"] == {"bytes": 0, "events": 0, "cost_usd_month": 0.0}
    assert z["reduction_pct"] == 0.0
    assert part14.is_file() and part15.is_file()  # archive untouched


def test_cold_append_accumulates(server):
    """A second POST to the same hour appends a new gzip member that still
    decompresses as one stream containing all events."""
    post("/reset")
    cold_dir = server["cold_dir"]
    first = [event("INFO", ts="2026-06-11T09:00:01.000Z")]
    second = [event("ERROR", "error", ts="2026-06-11T09:30:00.000Z")]
    post("/ingest/cold", encode_ndjson(first))
    post("/ingest/cold", encode_single(second[0]))
    part = cold_dir / "2026" / "06" / "11" / "09" / "events-09.jsonl.gz"
    with gzip.open(part, "rt") as fh:
        got = [json.loads(line) for line in fh if line.strip()]
    assert got == first + second


# --- v2: raw original lines are first-class events --------------------------

NCSA_LINE = (
    '203.0.113.7 - alice [10/Jun/2026:16:04:12 +0000] '
    '"GET /healthz HTTP/1.1" 200 512 "-" "kube-probe/1.29"'
)
CRI_LINE = (
    "2026-06-10T16:04:12.123456789Z stdout F "
    '{"level":"warn","ts":1718035452.1,"caller":"sync/loop.go:42","msg":"retrying"}'
)
APP_LINE = json.dumps(
    {
        "timestamp": "2026-06-10T16:04:12.123Z",
        "level": "error",
        "service": "checkout",
        "msg": "payment failed",
    }
)
NO_LEVEL_LINE = json.dumps({"eventVersion": "1.08", "eventName": "PutObject"})


def test_mixed_ndjson_body_raw_and_json_lines(server):
    """One NDJSON body mixing a raw NCSA line, a CRI line, a JSON app line,
    and a JSON line without a level: every line is an event; only parsed
    JSON objects with a 'level' get a real level key, the rest are RAW."""
    post("/reset")
    body = "\n".join([NCSA_LINE, CRI_LINE, APP_LINE, NO_LEVEL_LINE]).encode() + b"\n"
    assert post("/ingest/hot", body) == {"ok": True, "events": 4}
    s = get_stats()
    assert s["hot"]["bytes"] == len(body)  # exact request-body bytes
    assert s["hot"]["events"] == 4
    assert s["hot"]["events_by_level"] == {"RAW": 3, "error": 1}


def test_text_plain_body_accepted(server):
    post("/reset")
    body = (NCSA_LINE + "\n").encode()
    assert post("/ingest/raw", body, ctype="text/plain")["events"] == 1
    s = get_stats()
    assert s["raw"] == {"bytes": len(body), "events": 1}


def test_single_raw_line_is_one_opaque_event(server):
    post("/reset")
    assert post("/ingest/hot", b"this is not json")["events"] == 1
    s = get_stats()
    assert s["hot"]["events_by_level"] == {"RAW": 1}
    assert s["hot"]["bytes"] == len(b"this is not json")


def test_cold_raw_lines_stored_verbatim_in_receive_hour(server):
    """Raw lines have no parsed ts: they partition by the hour costboard
    received them, and the stored gzip lines are byte-identical originals."""
    post("/reset")
    cold_dir = server["cold_dir"]
    before = {p for p in cold_dir.rglob("*.jsonl.gz")}
    body = "\n".join([NCSA_LINE, CRI_LINE]).encode()
    hour_before = datetime.now(timezone.utc)
    assert post("/ingest/cold", body)["events"] == 2
    hour_after = datetime.now(timezone.utc)

    new_files = {p for p in cold_dir.rglob("*.jsonl.gz")} - before
    candidates = {
        cold_dir / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
        / f"{d.hour:02d}" / f"events-{d.hour:02d}.jsonl.gz"
        for d in (hour_before, hour_after)
    }
    written = new_files | {p for p in candidates if p in before and p.is_file()}
    # The write went to the receive-hour partition (modulo an hour rollover).
    assert all(p in candidates for p in new_files)
    lines = []
    for path in sorted(written):
        with gzip.open(path, "rt") as fh:
            lines += [ln.rstrip("\n") for ln in fh if ln.strip()]
    assert NCSA_LINE in lines and CRI_LINE in lines  # verbatim originals


def test_empty_body_rejected_and_not_counted(server):
    post("/reset")
    with pytest.raises(urllib.error.HTTPError) as exc:
        post("/ingest/hot", b"   \n  \n")
    assert exc.value.code == 400
    s = get_stats()
    assert s["hot"]["bytes"] == 0 and s["hot"]["events"] == 0


def test_unknown_lane_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        post("/ingest/lukewarm", encode_single(event()))
    assert exc.value.code == 404
