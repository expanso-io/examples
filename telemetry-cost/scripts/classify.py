#!/usr/bin/env python3
"""Reference classifier: the pipeline's classification rules, in Python.

This is the ground truth for the evals and the engine behind audit.py. The
Bloblang classification block shared by jobs/01..04 and this module implement
the SAME rules (DESIGN.md "The classification mapping"); evals compare what
the pipelines actually did against the verdicts produced here.

A verdict is one dict per raw line:

    src            app | k8s | web | cloudtrail | unknown
    level          debug | info | warn | error   (normalized lowercase)
    is_health      health-check / probe traffic (path or kube-probe UA)
    is_debug       debug-level chatter
    dup_key        k8s zap lines only: "caller|msg" (crash-loop dedupe key)
    slow           app request with duration_ms > 2000
    retain         compliance hold: cloudtrail, app auth service, web POST /login
    queried_class  regular | rare | never  ("would your query audit find this?")
    raw_bytes      UTF-8 length of the line (excl. newline) — wire cost

Source detection order (first match wins):
  1. JSON object with `eventVersion`        -> cloudtrail
  2. JSON object with `level` and `service` -> app
  3. CRI prefix `<ts> stdout|stderr F|P `   -> k8s (payload re-classified:
     zap JSON / klog / nginx-ingress access line / plain text)
  4. NCSA combined/common access line       -> web
  5. anything else                          -> unknown (kept, info)

The queried_class heuristic is intentionally honest about being a heuristic:
it encodes what a query-log audit typically finds (errors/warns/5xx/auth get
queried regularly; slow-request investigations are rare; health checks, debug
chatter and 2xx static-asset hits are never looked at again).

Usage:
    python3 scripts/classify.py [FILE ...]      # NDJSON verdicts to stdout
    python3 scripts/classify.py --summary       # one-line JSON aggregate
Default files: the four committed fixtures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_FILES = [
    ROOT / "fixtures" / "app.ndjson",
    ROOT / "fixtures" / "k8s.log",
    ROOT / "fixtures" / "web.log",
    ROOT / "fixtures" / "cloudtrail.ndjson",
]

SLOW_MS = 2000

# Health-check surface: probe paths plus the kubelet's probe user-agent.
HEALTH_PATHS = {"/health", "/healthz", "/ready", "/livez", "/metrics"}

# CRI container log line: <RFC3339Nano> stdout|stderr F|P <payload>
CRI_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+) (stdout|stderr) ([FP]) (.*)$")

# klog: I0610 16:00:01.676301       1 leaderelection.go:260] ...
KLOG_RE = re.compile(r"^([IWEF])\d{4} \d{2}:\d{2}:\d{2}")
KLOG_LEVELS = {"I": "info", "W": "warn", "E": "error", "F": "error"}

# NCSA combined/common: IP - user [ts] "METHOD path HTTP/x" status bytes ...
NCSA_RE = re.compile(
    r'^(\S+) (\S+) (\S+) \[([^\]]+)\] "([^"]*)" (\d{3}) (\S+)'
    r'(?: "([^"]*)" "([^"]*)")?'
)

STATIC_EXTS = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".ico",
               ".svg", ".woff", ".woff2", ".map", ".txt")


def _try_json(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _path_only(target: str) -> str:
    return target.split("?", 1)[0]


def _is_static(path: str) -> bool:
    return path.startswith("/static/") or path.endswith(STATIC_EXTS)


def _web_semantics(method: str, path: str, status: int, user_agent: str) -> Dict[str, Any]:
    """Access-log semantics shared by web.log and k8s nginx-ingress payloads."""
    path = _path_only(path)
    if status >= 500:
        level = "error"
    elif status >= 400:
        level = "warn"
    else:
        level = "info"
    return {
        "level": level,
        "is_health": path in HEALTH_PATHS or "kube-probe" in user_agent,
        "retain": method == "POST" and path == "/login",
        "auth": method == "POST" and path == "/login",
        "static_2xx": status < 300 and _is_static(path),
    }


def classify(line: str) -> dict:
    """Classify one raw log line. The single source of truth for the evals."""
    raw_bytes = len(line.encode("utf-8"))
    verdict: Dict[str, Any] = {
        "src": "unknown",
        "level": "info",
        "is_health": False,
        "is_debug": False,
        "dup_key": None,
        "slow": False,
        "retain": False,
        "queried_class": "rare",
        "raw_bytes": raw_bytes,
    }
    auth = False
    static_2xx = False

    obj = _try_json(line)
    cri = CRI_RE.match(line) if obj is None else None

    if obj is not None and "eventVersion" in obj:
        # 1. AWS CloudTrail: an audit trail, so it is retained by definition.
        verdict["src"] = "cloudtrail"
        verdict["level"] = "info"
        verdict["retain"] = True

    elif obj is not None and "level" in obj and "service" in obj:
        # 2. Structured app log.
        verdict["src"] = "app"
        verdict["level"] = str(obj["level"]).lower()
        http = obj.get("http") or {}
        path = _path_only(str(http.get("path", "")))
        verdict["is_health"] = path in HEALTH_PATHS
        duration = obj.get("duration_ms")
        verdict["slow"] = isinstance(duration, (int, float)) and duration > SLOW_MS
        if obj.get("service") == "auth":
            verdict["retain"] = True
            auth = True

    elif cri is not None:
        # 3. Kubernetes CRI line; re-classify the embedded payload.
        verdict["src"] = "k8s"
        payload = cri.group(4)
        zap = _try_json(payload)
        klog = KLOG_RE.match(payload)
        ncsa = NCSA_RE.match(payload)
        if zap is not None and "level" in zap:
            verdict["level"] = str(zap["level"]).lower()
            caller = zap.get("caller")
            msg = zap.get("msg")
            if caller or msg:
                # The crash-loop dedupe key: identical zap caller+msg repeats.
                verdict["dup_key"] = f"{caller or ''}|{msg or ''}"
        elif klog is not None:
            verdict["level"] = KLOG_LEVELS[klog.group(1)]
        elif ncsa is not None:
            req = ncsa.group(5).split()
            method = req[0] if req else ""
            path = req[1] if len(req) > 1 else ""
            web = _web_semantics(method, path, int(ncsa.group(6)), ncsa.group(9) or "")
            verdict["level"] = web["level"]
            verdict["is_health"] = web["is_health"]
            verdict["retain"] = web["retain"]
            auth = web["auth"]
            static_2xx = web["static_2xx"]
        # else: plain container stdout/stderr (panic traces, partial lines):
        # kept as k8s/info — the tiers demo treats it as droppable chatter.

    else:
        ncsa = NCSA_RE.match(line)
        if ncsa is not None:
            # 4. NCSA web access log.
            req = ncsa.group(5).split()
            method = req[0] if req else ""
            path = req[1] if len(req) > 1 else ""
            web = _web_semantics(method, path, int(ncsa.group(6)), ncsa.group(9) or "")
            verdict["src"] = "web"
            verdict["level"] = web["level"]
            verdict["is_health"] = web["is_health"]
            verdict["retain"] = web["retain"]
            auth = web["auth"]
            static_2xx = web["static_2xx"]
        # else: 5. unknown — kept, info (defaults already set).

    verdict["is_debug"] = verdict["level"] == "debug"

    # queried_class: error/warn/5xx/auth get queried regularly; health, debug
    # and 2xx static hits never; everything else (incl. slow lookups) rarely.
    if verdict["level"] in ("error", "warn") or auth:
        verdict["queried_class"] = "regular"
    elif verdict["is_health"] or verdict["is_debug"] or static_2xx:
        verdict["queried_class"] = "never"
    else:
        verdict["queried_class"] = "rare"

    return verdict


# --- aggregate helpers (shared with evals/run_evals.py) ------------------------

def is_signal(verdict: dict) -> bool:
    """Hot-lane membership: errors, warnings (incl. 5xx) and slow requests."""
    return verdict["level"] in ("error", "warn") or verdict["slow"]


def lane(verdict: dict) -> str:
    """The 04-tiers routing: junk deleted first, then hot, then cold."""
    if verdict["is_health"] or verdict["is_debug"]:
        return "dropped"
    if is_signal(verdict):
        return "hot"
    if verdict["retain"]:
        return "cold"
    return "dropped"


def iter_lines(files: Iterable[Path]) -> Iterable[str]:
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line:
                    yield line


def summarize(files: List[Path]) -> dict:
    """Aggregate verdict counts over `files` — the eval ground truth."""
    n = 0
    total_bytes = 0
    by_src: Dict[str, int] = {}
    by_level: Dict[str, int] = {}
    lanes = {"hot": 0, "cold": 0, "dropped": 0}
    lane_bytes = {"hot": 0, "cold": 0, "dropped": 0}
    signal = 0
    signal_bytes = 0
    dup_signal = 0          # error/warn lines carrying a dup_key
    dup_keys_signal = set()  # ...and their unique keys (dedupe survivors)
    dup_total = 0
    dup_keys_total = set()
    slow = 0
    retain = 0
    health = 0
    debug = 0
    rest = 0  # neither junk nor signal: the step-4 10%-sample pool
    never_bytes = 0
    queried: Dict[str, int] = {}
    app_levels: Dict[str, int] = {}

    for line in iter_lines(files):
        v = classify(line)
        n += 1
        total_bytes += v["raw_bytes"]
        by_src[v["src"]] = by_src.get(v["src"], 0) + 1
        by_level[v["level"]] = by_level.get(v["level"], 0) + 1
        queried[v["queried_class"]] = queried.get(v["queried_class"], 0) + 1
        if v["src"] == "app":
            app_levels[v["level"]] = app_levels.get(v["level"], 0) + 1
        which = lane(v)
        lanes[which] += 1
        lane_bytes[which] += v["raw_bytes"]
        if is_signal(v):
            signal += 1
            signal_bytes += v["raw_bytes"]
        if v["dup_key"] is not None:
            dup_total += 1
            dup_keys_total.add(v["dup_key"])
            if v["level"] in ("error", "warn"):
                dup_signal += 1
                dup_keys_signal.add(v["dup_key"])
        slow += v["slow"]
        retain += v["retain"]
        health += v["is_health"]
        debug += v["is_debug"]
        if not (v["is_health"] or v["is_debug"]) and not is_signal(v):
            rest += 1
        if v["queried_class"] == "never":
            never_bytes += v["raw_bytes"]

    # Step-3/4 dedupe expectation: each dup_key burst survives as ONE line.
    signal_dedup = signal - dup_signal + len(dup_keys_signal)
    return {
        "files": [p.name for p in files],
        "lines": n,
        "bytes": total_bytes,
        "by_src": dict(sorted(by_src.items())),
        "by_level": dict(sorted(by_level.items())),
        "app_by_level": dict(sorted(app_levels.items())),
        "queried_class": dict(sorted(queried.items())),
        "signal": signal,
        "signal_bytes": signal_bytes,
        "signal_dedup": signal_dedup,
        "dup_lines": dup_total,
        "dup_keys": len(dup_keys_total),
        "slow": slow,
        "retain": retain,
        "is_health": health,
        "is_debug": debug,
        "lanes": lanes,
        "lane_bytes": lane_bytes,
        "garbage_pct": round(100.0 * never_bytes / total_bytes, 2) if total_bytes else 0.0,
        "rest_after_junk_and_signal": rest,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="raw log files (default: the four fixtures)")
    ap.add_argument("--summary", action="store_true",
                    help="emit a one-line JSON aggregate instead of per-line verdicts")
    args = ap.parse_args()

    files = [Path(f) for f in args.files] if args.files else DEFAULT_FILES
    missing = [p for p in files if not p.exists()]
    if missing:
        print(f"error: missing input file(s): {', '.join(map(str, missing))}",
              file=sys.stderr)
        return 2

    if args.summary:
        print(json.dumps(summarize(files), sort_keys=True))
        return 0

    out = sys.stdout
    for line in iter_lines(files):
        out.write(json.dumps(classify(line), sort_keys=True))
        out.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
