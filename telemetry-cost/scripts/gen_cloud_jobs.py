#!/usr/bin/env python3
"""Generate jobs/cloud/*.yaml from the base jobs/0*.yaml.

Cloud variants are identical to their base job EXCEPT they carry a top-level
`selector` block so Expanso Cloud schedules them ONLY onto the dedicated demo
node (the one cloud_setup.sh labelled `demo: telemetry-cost`). The base jobs
stay selector-free so they remain portable for `--local` / offline runs.

WHY this is a text insertion, not a YAML round-trip: the base jobs are full of
comments that double as the demo talk-track. A safe_load/dump cycle would strip
every one of them. So we insert the selector as raw text right after the
top-level `type:` line, leaving `name`, `description`, `config`, and every
comment byte-for-byte intact. The selector lands as a sibling of name/type/
config (spec top level), NOT inside config.

After writing each variant we validate it offline and confirm the selector is
present. Any failure makes the whole run exit non-zero (so `make cloud-jobs`
fails loudly in CI).
"""

import glob
import os
import subprocess
import sys

SRC_DIR = "jobs"
OUT_DIR = "jobs/cloud"

# Sibling of name/type/config at spec top level. match_labels must match the
# node labels written by cloud_setup.sh (.edge-cloud/config.d/30-demo-labels.yaml).
SELECTOR_BLOCK = "selector:\n  match_labels:\n    demo: telemetry-cost\n"


def inject_selector(text):
    """Return (new_text, ok). Insert SELECTOR_BLOCK after the first top-level
    `type:` line (a line beginning at column 0 with `type:`)."""
    lines = text.splitlines(keepends=True)
    out = []
    injected = False
    for line in lines:
        out.append(line)
        if not injected and line.startswith("type:"):
            # Guarantee the type line ends in a newline before we append.
            if not out[-1].endswith("\n"):
                out[-1] = out[-1] + "\n"
            out.append(SELECTOR_BLOCK)
            injected = True
    return "".join(out), injected


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    bases = sorted(
        f for f in glob.glob(os.path.join(SRC_DIR, "0*.yaml"))
        if os.path.isfile(f)
    )
    if not bases:
        print("cloud-jobs: no base jobs found under %s/0*.yaml" % SRC_DIR)
        return 1

    failures = 0
    for src in bases:
        dst = os.path.join(OUT_DIR, os.path.basename(src))
        with open(src, "r") as f:
            text = f.read()

        new_text, injected = inject_selector(text)
        if not injected:
            print("FAIL  %s  (no top-level 'type:' line to anchor selector)" % dst)
            failures += 1
            continue

        with open(dst, "w") as f:
            f.write(new_text)

        # Acceptance check 1: selector present at spec top level (column 0).
        has_top_level_selector = any(
            ln.startswith("selector:") for ln in new_text.splitlines()
        ) and "match_labels" in new_text

        # Acceptance check 2: offline validation passes.
        proc = subprocess.run(
            ["expanso-cli", "job", "validate", dst, "--offline"],
            capture_output=True,
            text=True,
        )

        if proc.returncode == 0 and has_top_level_selector:
            print("PASS  %s  (selector injected, offline-valid)" % dst)
        else:
            why = []
            if proc.returncode != 0:
                why.append("validate rc=%d" % proc.returncode)
            if not has_top_level_selector:
                why.append("selector missing/not-top-level")
            print("FAIL  %s  (%s)" % (dst, ", ".join(why)))
            detail = (proc.stdout + proc.stderr).strip()
            if detail:
                sys.stderr.write(detail + "\n")
            failures += 1

    print(
        "cloud-jobs: %d generated, %d passed, %d failed"
        % (len(bases), len(bases) - failures, failures)
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
