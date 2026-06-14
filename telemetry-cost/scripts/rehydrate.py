#!/usr/bin/env python3
"""Rehydrate raw lines from cold storage: the "auditor calls" story.

Compliance logs were routed to cheap object storage instead of the hot
SIEM (Pillar 4). Eighteen months later an auditor asks for "everything
around the June 10th incident, 14:00-16:00". This script is that answer:

  python3 scripts/rehydrate.py \
      --from 2026-06-10T14:00:00Z --to 2026-06-10T16:00:00Z \
      --grep 'POST /login' --stats > pulled.log

The cold lane stores the RAW ORIGINAL LINES (verbatim, byte-honest) in
gzip partitions under cold-storage/YYYY/MM/DD/HH/ — partitioned by the
hour costboard RECEIVED them. Raw lines carry no uniform timestamp field,
so time filtering here is hour-granular BY DESIGN: --from/--to select
which receive-hour partition directories to pull, and every line in a
selected partition is returned. An hour partition is selected when it
overlaps the half-open window [--from, --to). Use --grep to narrow lines
with a regular expression (re.search per line).

With --stats, a summary of matched lines/bytes and partitions scanned
goes to stderr — so stdout stays pipe-clean. Stdlib only.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Pattern, Tuple

HOUR = timedelta(hours=1)


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; tolerate trailing 'Z' on Python 3.10.

    Naive timestamps are assumed UTC -- the cold lane partitions in UTC.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def partition_hour(path: Path, root: Path) -> Optional[datetime]:
    """Map cold-storage/YYYY/MM/DD/HH/file.jsonl.gz -> that hour (UTC).

    Returns None for files that don't sit in a recognizable partition;
    those are scanned anyway rather than silently skipped.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) != 5:
        return None
    try:
        year, month, day, hour = (int(p) for p in parts[:4])
        return datetime(year, month, day, hour, tzinfo=timezone.utc)
    except ValueError:
        return None


def find_partitions(cold_dir: Path, ts_from: datetime, ts_to: datetime) -> List[Path]:
    """All *.jsonl.gz files whose receive-hour partition overlaps the window.

    Hour H is selected when [H, H+1h) intersects [ts_from, ts_to) — i.e.
    H < ts_to and H + 1h > ts_from. Files outside that range are pruned
    without being opened; files in an unrecognizable layout are kept.
    """
    matches: List[Path] = []
    for path in sorted(cold_dir.rglob("*.jsonl.gz")):
        hour = partition_hour(path, cold_dir)
        if hour is not None and not (hour < ts_to and hour + HOUR > ts_from):
            continue
        matches.append(path)
    return matches


def rehydrate(
    cold_dir: Path,
    ts_from: datetime,
    ts_to: datetime,
    grep: Optional[Pattern[str]] = None,
) -> Tuple[List[str], int, int]:
    """Pull raw lines back out of cold storage.

    Returns (lines, matched_bytes, partitions_scanned). Lines come back
    verbatim (no parsing — the cold lane stores original bytes); bytes are
    the UTF-8 size of the emitted lines (newlines excluded).
    """
    lines: List[str] = []
    matched_bytes = 0
    partitions = find_partitions(cold_dir, ts_from, ts_to)

    for path in partitions:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.rstrip("\r\n")
                if not raw.strip():
                    continue
                if grep is not None and grep.search(raw) is None:
                    continue
                lines.append(raw)
                matched_bytes += len(raw.encode("utf-8"))

    return lines, matched_bytes, len(partitions)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rehydrate.py",
        description=(
            "Pull a time range back out of cold storage. Selects the "
            "cold-storage/YYYY/MM/DD/HH/*.jsonl.gz receive-hour partitions "
            "overlapping the window, optionally greps lines, and writes "
            "the matching RAW lines verbatim to stdout."
        ),
        epilog=(
            "Time filtering is hour-granular: raw lines carry no uniform "
            "timestamp field, so --from/--to select partition directories "
            "(half-open window, hour overlap). Example: rehydrate.py "
            "--from 2026-06-10T14:00:00Z --to 2026-06-10T16:00:00Z "
            "--grep 'POST /login' --stats"
        ),
    )
    parser.add_argument(
        "--from",
        dest="ts_from",
        required=True,
        metavar="ISO",
        help="window start, ISO-8601, inclusive (e.g. 2026-06-10T14:00:00Z)",
    )
    parser.add_argument(
        "--to",
        dest="ts_to",
        required=True,
        metavar="ISO",
        help="window end, ISO-8601, exclusive",
    )
    parser.add_argument(
        "--grep",
        default=None,
        metavar="REGEX",
        help="only return lines matching this regular expression",
    )
    parser.add_argument(
        "--cold-dir",
        default="cold-storage",
        metavar="DIR",
        help="root of the cold-storage partition tree (default: cold-storage)",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="print matched lines/bytes and partitions scanned to stderr",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        ts_from = parse_iso(args.ts_from)
        ts_to = parse_iso(args.ts_to)
    except ValueError as exc:
        print(f"rehydrate.py: bad timestamp: {exc}", file=sys.stderr)
        return 2
    if ts_from >= ts_to:
        print("rehydrate.py: --from must be earlier than --to", file=sys.stderr)
        return 2

    grep: Optional[Pattern[str]] = None
    if args.grep is not None:
        try:
            grep = re.compile(args.grep)
        except re.error as exc:
            print(f"rehydrate.py: bad --grep regex: {exc}", file=sys.stderr)
            return 2

    cold_dir = Path(args.cold_dir)
    if not cold_dir.is_dir():
        print(
            f"rehydrate.py: cold storage directory not found: {cold_dir}",
            file=sys.stderr,
        )
        return 2

    lines, matched_bytes, partitions = rehydrate(cold_dir, ts_from, ts_to, grep)

    out = sys.stdout
    for line in lines:
        out.write(line)
        out.write("\n")
    out.flush()

    if args.stats:
        pat = args.grep if args.grep is not None else "(none)"
        print(
            f"rehydrate.py: matched {len(lines)} line(s), "
            f"{matched_bytes} bytes, scanned {partitions} partition file(s) "
            f"[window {ts_from.isoformat()} .. {ts_to.isoformat()} "
            f"(hour-granular), grep {pat}]",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
