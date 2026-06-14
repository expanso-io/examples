#!/usr/bin/env python3
"""Telemetry audit: find out what you're paying to ship but never look at.

HONESTY NOTE: this is a heuristic stand-in for your real query-audit join.
In production you would join shipped log volume against your SIEM / log
platform's query history (the Pillar 2 method). Here, scripts/classify.py
encodes "what your query audit would tell you" as rules (errors/warns/auth
get queried regularly, slow-request traces rarely, health checks and debug
chatter never), so the demo numbers are reproducible.

Methodology:

  1. Group lines into patterns by (src, service-or-stream, level,
     queried_class).
  2. For each pattern, total events and bytes on the wire.
  3. Classify each pattern into one of four action buckets:

       STOP SHIPPING  never queried, no compliance hold -> just stop
       ROUTE COLD     never queried, but compliance says retain -> object store
       SAMPLE         queried rarely -> keep a sample, not the firehose
       KEEP HOT       queried regularly -> this is the signal, keep it

  4. Print the volume-vs-queried table plus the overall garbage ratio:
     the percentage of total bytes in never-queried patterns.

Inputs (stdlib only):

  python3 scripts/audit.py                          # data/audit-sample.jsonl,
                                                    # the parsed envelopes the
                                                    # 02-audit job tees off
  python3 scripts/audit.py data/audit-sample.jsonl  # same, explicit
  python3 scripts/audit.py --raw fixtures/web.log fixtures/app.ndjson
                                                    # raw log files, classified
                                                    # on the fly via classify.py
  python3 scripts/audit.py --json                   # machine-readable, for evals
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

DEFAULT_INPUT = "data/audit-sample.jsonl"

HONESTY_HEADER = (
    "NOTE: heuristic stand-in for your real query-audit join -- in production,\n"
    "join shipped volume against SIEM/platform query logs (Pillar 2 method)."
)

BUCKET_STOP = "STOP SHIPPING"
BUCKET_COLD = "ROUTE COLD"
BUCKET_SAMPLE = "SAMPLE"
BUCKET_HOT = "KEEP HOT"
BUCKET_ORDER = [BUCKET_STOP, BUCKET_COLD, BUCKET_SAMPLE, BUCKET_HOT]

QUERIED_CLASSES = ("never", "rare", "regular")


def load_classifier():
    """Import classify() from scripts/classify.py (raw mode only)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from classify import classify  # noqa: PLC0415 - lazy: envelope mode works without it

    return classify


def iter_lines(path: str) -> Iterable[str]:
    if path == "-":
        yield from sys.stdin
        return
    with open(path, "r", encoding="utf-8") as fh:
        yield from fh


def read_envelopes(paths: List[str]) -> Tuple[List[Dict[str, Any]], int]:
    """Read the parsed-envelope sample the 02-audit job tees off.

    Each line is a JSON envelope carrying the classification fields
    (src, level, retain, queried_class, ...) plus the original line.
    Returns (records, skipped_count); each record is the normalized dict
    that analyze() consumes.
    """
    records: List[Dict[str, Any]] = []
    skipped = 0
    for path in paths:
        for line in iter_lines(path):
            line = line.strip()
            if not line:
                continue
            try:
                env = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(env, dict):
                skipped += 1
                continue
            records.append(normalize(env, fallback_bytes=len(line.encode("utf-8"))))
    return records, skipped


def read_raw(paths: List[str]) -> Tuple[List[Dict[str, Any]], int]:
    """Classify raw log lines on the fly with scripts/classify.py."""
    classify = load_classifier()
    records: List[Dict[str, Any]] = []
    skipped = 0
    for path in paths:
        for line in iter_lines(path):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            verdict = classify(line)
            if not isinstance(verdict, dict):
                skipped += 1
                continue
            records.append(
                normalize(verdict, fallback_bytes=len(line.encode("utf-8")))
            )
    return records, skipped


def normalize(fields: Dict[str, Any], fallback_bytes: int) -> Dict[str, Any]:
    """Reduce an envelope / classify() verdict to what analyze() needs."""
    nbytes = fields.get("raw_bytes")
    if not isinstance(nbytes, int) or nbytes <= 0:
        raw = fields.get("_raw")
        if isinstance(raw, str) and raw:
            nbytes = len(raw.encode("utf-8"))
        else:
            nbytes = fallback_bytes
    queried_class = fields.get("queried_class")
    if queried_class not in QUERIED_CLASSES:
        queried_class = "rare"  # unknown lines: assume someone might look
    svc = fields.get("service") or fields.get("stream") or "-"
    return {
        "src": str(fields.get("src", "unknown")),
        "svc": str(svc),
        "level": str(fields.get("level", "unknown")),
        "queried_class": queried_class,
        "retain": bool(fields.get("retain")),
        "bytes": nbytes,
    }


def bucket_for(queried_class: str, retain_events: int) -> str:
    """Map a pattern's query class + compliance posture to an action.

    Never-queried patterns split on compliance: if even one line carries
    a retention hold, you can't drop the pattern -- route it to cheap
    cold storage instead.
    """
    if queried_class == "never":
        return BUCKET_COLD if retain_events > 0 else BUCKET_STOP
    if queried_class == "rare":
        return BUCKET_SAMPLE
    return BUCKET_HOT


def analyze(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate records into the per-pattern table and overall summary."""
    patterns: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}

    for rec in records:
        key = (rec["src"], rec["svc"], rec["level"], rec["queried_class"])
        row = patterns.setdefault(key, {"events": 0, "bytes": 0, "retain": 0})
        row["events"] += 1
        row["bytes"] += rec["bytes"]
        if rec["retain"]:
            row["retain"] += 1

    rows: List[Dict[str, Any]] = []
    for (src, svc, level, queried_class), agg in patterns.items():
        rows.append(
            {
                "pattern": f"{src}/{svc}/{level}/{queried_class}",
                "src": src,
                "service_or_stream": svc,
                "level": level,
                "queried_class": queried_class,
                "events": agg["events"],
                "bytes": agg["bytes"],
                "retain_events": agg["retain"],
                "bucket": bucket_for(queried_class, agg["retain"]),
            }
        )

    rows.sort(key=lambda r: r["bytes"], reverse=True)

    total_bytes = sum(r["bytes"] for r in rows)
    total_events = sum(r["events"] for r in rows)
    garbage_bytes = sum(r["bytes"] for r in rows if r["queried_class"] == "never")
    garbage_ratio_pct = 100.0 * garbage_bytes / total_bytes if total_bytes else 0.0

    buckets: Dict[str, Dict[str, int]] = {
        b: {"patterns": 0, "events": 0, "bytes": 0} for b in BUCKET_ORDER
    }
    for r in rows:
        b = buckets[r["bucket"]]
        b["patterns"] += 1
        b["events"] += r["events"]
        b["bytes"] += r["bytes"]

    return {
        "method_note": HONESTY_HEADER.replace("\n", " "),
        "total_events": total_events,
        "total_bytes": total_bytes,
        "garbage_bytes": garbage_bytes,
        "garbage_ratio_pct": round(garbage_ratio_pct, 2),
        "patterns": rows,
        "buckets": buckets,
    }


def human_bytes(n: int) -> str:
    """1234567 -> '1.2 MB'. Keeps the table readable at demo scale."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(n)} B"


def render_table(summary: Dict[str, Any]) -> str:
    """Format the aligned volume-vs-queried table plus the garbage ratio."""
    headers = ["PATTERN", "EVENTS", "EST. BYTES", "QUERIED", "ACTION"]
    table: List[List[str]] = []
    for r in summary["patterns"]:
        table.append(
            [
                r["pattern"],
                f"{r['events']:,}",
                human_bytes(r["bytes"]),
                r["queried_class"],
                r["bucket"],
            ]
        )

    widths = [len(h) for h in headers]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: List[str]) -> str:
        # Pattern, queried class and action left-aligned; numbers right-aligned.
        cells = [
            row[0].ljust(widths[0]),
            row[1].rjust(widths[1]),
            row[2].rjust(widths[2]),
            row[3].ljust(widths[3]),
            row[4].ljust(widths[4]),
        ]
        return "  ".join(cells).rstrip()

    sep = "-" * (sum(widths) + 2 * (len(widths) - 1))
    lines = [HONESTY_HEADER, "", fmt(headers), sep]
    lines.extend(fmt(row) for row in table)
    lines.append(sep)

    lines.append(
        f"Total: {summary['total_events']:,} events, "
        f"{human_bytes(summary['total_bytes'])}"
    )
    for bucket in BUCKET_ORDER:
        b = summary["buckets"][bucket]
        if b["patterns"] == 0:
            continue
        pct = (
            100.0 * b["bytes"] / summary["total_bytes"]
            if summary["total_bytes"]
            else 0.0
        )
        lines.append(
            f"  {bucket:<13} {b['patterns']:>3} patterns  "
            f"{b['events']:>8,} events  {human_bytes(b['bytes']):>10}  "
            f"({pct:.1f}% of bytes)"
        )
    lines.append("")
    lines.append(
        f"GARBAGE RATIO: {summary['garbage_ratio_pct']:.1f}% of bytes are in "
        f"never-queried patterns (you pay to ship them; nobody reads them)"
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit.py",
        description=(
            "Audit telemetry for waste: group lines into (src, service-or-"
            "stream, level, queried-class) patterns, report volume vs. how "
            "often each pattern gets queried, and recommend an action per "
            "pattern (STOP SHIPPING / ROUTE COLD / SAMPLE / KEEP HOT)."
        ),
        epilog=(
            "Heuristic stand-in for a real query-audit join: on production "
            "systems, derive 'queried' from your SIEM/log-platform query "
            "logs; here scripts/classify.py encodes those heuristics "
            "(Pillar 2 method). The garbage ratio is the share of total "
            "bytes in patterns nobody ever queries."
        ),
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        default=[DEFAULT_INPUT],
        metavar="FILE",
        help=(
            "input file(s): parsed envelopes from the 02-audit job, or raw "
            f"log files with --raw (default: {DEFAULT_INPUT}; '-' for stdin)"
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help=(
            "treat inputs as raw log files (NCSA/CRI/JSON lines) and "
            "classify each line via scripts/classify.py"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable JSON summary instead of the table",
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = args.inputs or [DEFAULT_INPUT]

    try:
        if args.raw:
            records, skipped = read_raw(paths)
        else:
            records, skipped = read_envelopes(paths)
    except OSError as exc:
        print(f"audit.py: cannot read input: {exc}", file=sys.stderr)
        return 2
    except ImportError as exc:
        print(
            f"audit.py: --raw needs scripts/classify.py ({exc})", file=sys.stderr
        )
        return 2

    if skipped:
        print(f"audit.py: skipped {skipped} malformed line(s)", file=sys.stderr)

    if not records:
        if args.json:
            print(json.dumps(analyze([]), indent=2))
            return 0
        print(f"audit.py: no events found in {', '.join(paths)}", file=sys.stderr)
        return 1

    summary = analyze(records)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(render_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
