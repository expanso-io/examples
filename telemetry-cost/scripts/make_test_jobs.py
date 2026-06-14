#!/usr/bin/env python3
"""Generate deterministic file-input variants of the demo jobs.

The demo jobs read live traffic from an http_server input (fed by the
00-intake tee over the logsim TCP streams). Tests and evals need repeatable
runs over the committed fixtures instead, so this script swaps each job's
``input:`` block for a ``file`` input reading the four raw fixture streams
line by line. Everything else in the job — processors, outputs, name — is
untouched, so tests exercise the exact pipeline logic that ships.

00-intake has no file-input analogue: it IS the live transport (socket_server
tee), and the eval harness posts the raw baseline to the costboard itself.

Stdlib-only on purpose (no YAML dependency): the job wrapper layout is fixed
by DESIGN.md (``config:`` with 2-space-indented ``input:``/``pipeline:``/
``output:`` keys), so the input block can be located by indentation alone.

Fixture paths are kept RELATIVE — start the edge node with the repo root as
its working directory (the harness does) and the variants stay portable.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The four raw fixture streams, replayed in a deterministic order.
# (file input consumes `paths` sequentially; `codec: lines` emits one
# message per raw line — verified against expanso-cli job validate.)
FIXTURE_PATHS = [
    "fixtures/app.ndjson",
    "fixtures/k8s.log",
    "fixtures/web.log",
    "fixtures/cloudtrail.ndjson",
]

# Matches the input key nested directly under the job's `config:` block.
INPUT_KEY = re.compile(r"^  input:\s*$")
# Any other 2-space-indented key under config: (pipeline:, output:, ...)
SIBLING_KEY = re.compile(r"^  \S")
TOP_KEY = re.compile(r"^\S")

FILE_INPUT_BLOCK = (
    "  input:\n"
    "    file:\n"
    "      paths:\n"
    + "".join(f"        - {p}\n" for p in FIXTURE_PATHS)
    + "      codec: lines\n"
)


def rewrite(text: str) -> str:
    """Replace the job's config.input block with the fixtures file input."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if INPUT_KEY.match(line):
            start = i
            break
    if start is None:
        raise ValueError("no '  input:' block found (job wrapper contract)")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if SIBLING_KEY.match(lines[j]) or TOP_KEY.match(lines[j]):
            end = j
            break
    return "".join(lines[:start]) + FILE_INPUT_BLOCK + "".join(lines[end:])


def generate(jobs_dir: Path, out_dir: Path, force: bool) -> list[Path]:
    written: list[Path] = []
    for src in sorted(jobs_dir.glob("0*.yaml")):
        if src.name.startswith("00-"):
            continue  # the live intake tee has no file-input analogue
        dst = out_dir / src.name
        if dst.exists() and not force:
            continue  # respect hand-written or previously generated variants
        out_dir.mkdir(parents=True, exist_ok=True)
        dst.write_text(rewrite(src.read_text()))
        written.append(dst)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jobs-dir", default=str(ROOT / "jobs"))
    ap.add_argument("--out-dir", default=str(ROOT / "jobs" / "test"))
    ap.add_argument("--force", action="store_true", help="overwrite existing variants")
    args = ap.parse_args()

    jobs_dir = Path(args.jobs_dir)
    if not jobs_dir.is_dir():
        print(f"error: jobs dir not found: {jobs_dir}", file=sys.stderr)
        return 1
    written = generate(jobs_dir, Path(args.out_dir), args.force)
    for p in written:
        print(f"wrote {p}")
    if not written:
        print("nothing to do (variants already exist; use --force to regenerate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
