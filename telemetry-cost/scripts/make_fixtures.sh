#!/usr/bin/env bash
# Regenerate the committed fixtures from the public log-simulators suite.
#
# Every stream is seeded and time-anchored, so the output is byte-identical
# on every run (and on every contributor's machine): the eval numbers in
# evals/REPORT.md are reproducible from these exact commands.
#
#   fixtures/app.ndjson        structured JSON app logs   (logsim-app,   6000)
#   fixtures/k8s.log           Kubernetes CRI lines       (logsim-k8s,   5000, crash-loop)
#   fixtures/web.log           NCSA combined access logs  (logsim-web,   6000)
#   fixtures/cloudtrail.ndjson AWS CloudTrail JSON lines  (logsim-cloud, 1200)
#
# Needs only uv (https://docs.astral.sh/uv/). Pulls the simulators from
# GitHub via uvx; set LOGSIM_SRC to a local checkout to run offline:
#
#   LOGSIM_SRC=~/code/log-simulators scripts/make_fixtures.sh
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p fixtures

GIT_SRC="git+https://github.com/expanso-io/log-simulators"
LOCAL_SRC="${LOGSIM_SRC:-$HOME/code/log-simulators}"

SRC="$GIT_SRC"
if ! uvx --from "$SRC" logsim-app --help >/dev/null 2>&1; then
    if [ -d "$LOCAL_SRC" ]; then
        echo "warn: cannot reach $GIT_SRC — falling back to $LOCAL_SRC" >&2
        SRC="$LOCAL_SRC"
    else
        echo "error: cannot reach $GIT_SRC and no local checkout at $LOCAL_SRC" >&2
        exit 1
    fi
fi

# Shared determinism anchor: same seed, same synthetic 2h window for every stream.
COMMON=(--seed 42 --backfill 2h --start-time 2026-06-10T16:00:00+00:00)

sim() {
    tool="$1"; out="$2"; shift 2
    echo "  $tool -> fixtures/$out"
    # The simulators' file sink appends (rotation-friendly); fixtures must be
    # regenerated from scratch, so truncate first.
    rm -f "fixtures/$out"
    uvx --from "$SRC" "$tool" "${COMMON[@]}" "$@" --output "fixtures/$out"
}

echo "regenerating fixtures from $SRC"
sim logsim-app   app.ndjson        --count 6000
sim logsim-k8s   k8s.log           --count 5000 --scenario crash-loop
sim logsim-web   web.log           --count 6000
sim logsim-cloud cloudtrail.ndjson --count 1200

wc -l fixtures/app.ndjson fixtures/k8s.log fixtures/web.log fixtures/cloudtrail.ndjson
