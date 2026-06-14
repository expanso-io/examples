#!/usr/bin/env python3
"""costboard — metered telemetry sinks + live cost dashboard.

Three ingest lanes (raw / hot / cold) meter exact request-body bytes and
event counts, convert them to dollars using editable pricing presets, and
serve a single-file dashboard that polls /stats once a second.

Lanes accept JSON, NDJSON, or raw text bodies (text/plain included): each
non-empty line is one event. Non-JSON lines are opaque events — bytes and
lines still count (that's what vendors bill), level-tracked as "RAW".

Stdlib only. Python 3.10+.

Endpoints:
    POST /ingest/raw   - everything the fleet emits (the "before" lane)
    POST /ingest/hot   - what reaches the expensive indexed backend
    POST /ingest/cold  - cheap object-storage tier; also writes gzip JSONL
                         partitions under <cold-dir>/YYYY/MM/DD/HH/
    GET  /stats        - counters + cost math (shape in DESIGN.md)
    POST /reset        - zero the counters (cold partition files are kept)
    GET  /             - the dashboard

Usage:
    python3 costboard/server.py [--port 8090] [--presets presets.json]
                                [--cold-dir cold-storage]
    PRESETS_FILE env var overrides the default presets path.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GB = 1_000_000_000  # decimal gigabyte, matches vendor ingest pricing
MILLION = 1_000_000

DEFAULT_PRESETS = {
    "hot_per_gb_ingest": 0.10,
    "hot_per_million_events": 1.70,
    "cold_per_gb_month": 0.023,
    "_note": (
        "Editable estimates, list prices as of June 2026. "
        "hot = per-GB ingest + per-million-events indexing (15-day); "
        "cold = object storage per GB-month."
    ),
}

STATIC_DIR = Path(__file__).resolve().parent / "static"


def load_presets(path: str) -> dict:
    """Merge presets file over built-in defaults; missing file -> defaults."""
    presets = dict(DEFAULT_PRESETS)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            presets.update(data)
    except (OSError, json.JSONDecodeError):
        pass  # defaults keep the demo running even without the file
    return presets


def parse_events(body: bytes) -> list:
    """Split a request body into events: (raw_line, parsed_dict_or_None) pairs.

    Lanes carry the ORIGINAL log lines (byte-honest billing), so bodies can
    be JSON, NDJSON, or raw text (NCSA access lines, CRI/k8s lines, ...) —
    including text/plain bodies. Each non-empty line is one event. Lines
    that parse as a JSON object keep the parsed form alongside (used for
    level tracking and cold-partition timestamps); everything else is an
    opaque event whose bytes and line count still get metered.

    Raises ValueError only for an effectively empty body.
    """
    text = body.decode("utf-8", errors="replace")
    # Whole body as one JSON document (covers single objects, even
    # pretty-printed ones spanning multiple lines, and JSON arrays).
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        doc = None
    if isinstance(doc, dict):
        return [(text.strip(), doc)]
    if isinstance(doc, list):
        return [
            (json.dumps(item), item if isinstance(item, dict) else None)
            for item in doc
        ]
    # Line-delimited: JSON lines AND raw text lines, freely mixed.
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        events.append((line, obj if isinstance(obj, dict) else None))
    if not events:
        raise ValueError("empty request body")
    return events


def partition_for(event) -> datetime:
    """UTC hour partition from a parsed event's ts, falling back to wall
    clock (the costboard receive hour). Opaque raw lines have no parsed
    `ts`, so they always partition by receive hour."""
    ts = event.get("ts") if isinstance(event, dict) else None
    if isinstance(ts, str):
        try:
            # Python 3.10 fromisoformat does not accept a trailing 'Z'.
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class Counters:
    """Thread-safe lane counters. Cost math lives here, next to the data."""

    def __init__(self, presets: dict):
        self.presets = presets
        self._lock = threading.Lock()
        self._zero()

    def _zero(self) -> None:
        self.raw_bytes = 0
        self.raw_events = 0
        self.hot_bytes = 0
        self.hot_events = 0
        self.hot_by_level: dict = {}
        self.cold_bytes = 0
        self.cold_events = 0
        self.started_at = datetime.now(timezone.utc).isoformat()

    def reset(self) -> None:
        with self._lock:
            self._zero()

    def add(self, lane: str, nbytes: int, events: list) -> None:
        with self._lock:
            if lane == "raw":
                self.raw_bytes += nbytes
                self.raw_events += len(events)
            elif lane == "hot":
                self.hot_bytes += nbytes
                self.hot_events += len(events)
                for _raw, parsed in events:
                    # Level keys are whatever the source emits (lowercase
                    # "error" for app JSON, "ERROR" for legacy events);
                    # opaque non-JSON lines are tracked as "RAW".
                    level = "RAW"
                    if isinstance(parsed, dict) and isinstance(parsed.get("level"), str):
                        level = parsed["level"]
                    self.hot_by_level[level] = self.hot_by_level.get(level, 0) + 1
            elif lane == "cold":
                self.cold_bytes += nbytes
                self.cold_events += len(events)

    def stats(self) -> dict:
        with self._lock:
            p = self.presets
            hot_cost = (
                self.hot_bytes / GB * p["hot_per_gb_ingest"]
                + self.hot_events / MILLION * p["hot_per_million_events"]
            )
            cold_cost = self.cold_bytes / GB * p["cold_per_gb_month"]
            reduction = 0.0
            if self.raw_bytes > 0:
                reduction = (1 - self.hot_bytes / self.raw_bytes) * 100
            return {
                "raw": {"bytes": self.raw_bytes, "events": self.raw_events},
                "hot": {
                    "bytes": self.hot_bytes,
                    "events": self.hot_events,
                    "events_by_level": dict(self.hot_by_level),
                    "cost_usd": hot_cost,
                },
                "cold": {
                    "bytes": self.cold_bytes,
                    "events": self.cold_events,
                    "cost_usd_month": cold_cost,
                },
                "reduction_pct": reduction,
                "presets": dict(p),
                "started_at": self.started_at,
            }


class ColdStore:
    """Appends gzip line partitions: <root>/YYYY/MM/DD/HH/events-<HH>.jsonl.gz.

    The cold lane stores the RAW ORIGINAL LINES, verbatim — what you'd
    actually park in object storage. Parsed JSON events with a `ts` still
    partition by that hour; opaque raw lines partition by the costboard
    receive hour (scripts/rehydrate.py filters by these hour directories).

    Each append writes a complete gzip member; concatenated members
    decompress as one stream, so plain `gzip.open(...).read()` rehydrates
    every line ever written to the partition.
    """

    def __init__(self, root: str):
        self.root = Path(root)
        self._lock = threading.Lock()

    def append(self, events: list) -> None:
        groups: dict = {}
        for raw, parsed in events:
            dt = partition_for(parsed)
            key = (f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}", f"{dt.hour:02d}")
            groups.setdefault(key, []).append(raw)
        with self._lock:
            for (yy, mm, dd, hh), group in groups.items():
                pdir = self.root / yy / mm / dd / hh
                pdir.mkdir(parents=True, exist_ok=True)
                path = pdir / f"events-{hh}.jsonl.gz"
                payload = "".join(line + "\n" for line in group)
                with open(path, "ab") as fh:
                    fh.write(gzip.compress(payload.encode("utf-8")))


class Handler(BaseHTTPRequestHandler):
    # Injected by serve(): counters, cold_store
    counters: Counters
    cold_store: ColdStore
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A002 - keep 1s polling quiet
        pass

    # -- helpers ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path == "/stats":
            self._send_json(200, self.counters.stats())
        elif path in ("/", "/index.html"):
            page = STATIC_DIR / "index.html"
            if page.is_file():
                self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(200, b"costboard: dashboard not found", "text/plain")
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path == "/reset":
            # Counters only — cold partition files are the archive; they stay.
            self.counters.reset()
            self._send_json(200, {"ok": True})
            return
        if path.startswith("/ingest/"):
            lane = path[len("/ingest/"):]
            if lane not in ("raw", "hot", "cold"):
                self._send_json(404, {"error": f"unknown lane {lane!r}"})
                return
            body = self._read_body()
            try:
                events = parse_events(body)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": f"bad payload: {exc}"})
                return
            # Meter EXACT request-body bytes — that is what vendors bill.
            self.counters.add(lane, len(body), events)
            if lane == "cold":
                self.cold_store.append(events)
            self._send_json(200, {"ok": True, "events": len(events)})
            return
        self._send_json(404, {"error": "not found"})


def serve(port: int, presets_path: str, cold_dir: str) -> ThreadingHTTPServer:
    Handler.counters = Counters(load_presets(presets_path))
    Handler.cold_store = ColdStore(cold_dir)
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="costboard: metered sinks + dashboard")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--presets",
        default=os.environ.get("PRESETS_FILE", "presets.json"),
        help="pricing presets JSON (env PRESETS_FILE overrides the default)",
    )
    parser.add_argument("--cold-dir", default="cold-storage")
    args = parser.parse_args()

    httpd = serve(args.port, args.presets, args.cold_dir)
    print(f"costboard listening on http://0.0.0.0:{args.port}  "
          f"(presets={args.presets}, cold-dir={args.cold_dir})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
