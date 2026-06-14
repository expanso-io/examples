#!/usr/bin/env bash
#
# demo.sh - THE telemetry-cost demo runner. Cloud-first.
#
# The headline path deploys job YAMLs THROUGH a saved Expanso Cloud profile
# onto a dedicated edge node (label demo=telemetry-cost). The control plane is
# cloud.expanso.io; the data plane (the node, the log streams, the dashboard)
# runs on this laptop. The same YAML lands on a fleet of 400 the same way.
#
# --local is the offline/CI escape hatch: it bypasses the control plane and
# deploys straight to a local edge. The pipelines are byte-for-byte identical;
# only the deploy target changes (--endpoint instead of --profile).
#
# Usage:
#   ./scripts/demo.sh [--profile NAME] [--scenario tax|audit|filter|tiers|all]
#                     [--local] [--no-pause] [--rate N] [--keep]
#
# Safety: in cloud mode this script deploys to and stops jobs on ONLY the demo
# profile you resolve here. It refuses to guess a profile and never falls back
# to another profile or silently to local.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- config ------------------------------------------------------------------

LOCAL_ENDPOINT="${EDGE_ENDPOINT:-http://127.0.0.1:19010}"
BOARD_URL="${BOARD_URL:-http://127.0.0.1:8090}"
SINK_PORT="${SINK_PORT:-5601}"
RUN_DIR="$ROOT/.run"
LOGSIM_SRC="${LOGSIM_SRC:-git+https://github.com/expanso-io/log-simulators}"
DWELL_SECS="${DEMO_DWELL:-10}"   # seconds to let the meter run in --no-pause mode

# --- args --------------------------------------------------------------------

ARG_PROFILE=""
SCENARIO="all"
LOCAL=0
NO_PAUSE=0
KEEP=0
RATE=""

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) ARG_PROFILE="${2:-}"; shift ;;
    --profile=*) ARG_PROFILE="${1#*=}" ;;
    --scenario) SCENARIO="${2:-}"; shift ;;
    --scenario=*) SCENARIO="${1#*=}" ;;
    --local) LOCAL=1 ;;
    --no-pause) NO_PAUSE=1 ;;
    --rate) RATE="${2:-}"; shift ;;
    --rate=*) RATE="${1#*=}" ;;
    --keep) KEEP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'demo: unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

case "$SCENARIO" in
  tax|audit|filter|tiers|all) ;;
  *) printf 'demo: invalid --scenario %s (want tax|audit|filter|tiers|all)\n' "$SCENARIO" >&2; exit 2 ;;
esac

if [ -t 1 ]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; CYAN=$'\033[36m'; BOLD=$'\033[1m'; RST=$'\033[0m'
else
  GREEN=""; RED=""; CYAN=""; BOLD=""; RST=""
fi
say()    { printf '%s\n' "$1"; }
hdr()    { printf '\n%s== %s ==%s\n' "$BOLD" "$1" "$RST"; }
beat()   { printf '%s>%s %s\n' "$CYAN" "$RST" "$1"; }
ok()     { printf '%s\xe2\x9c\x93%s %s\n' "$GREEN" "$RST" "$1"; }
die()    { printf '%s\xe2\x9c\x97%s %s\n' "$RED" "$RST" "$1" >&2; exit 1; }

mkdir -p "$RUN_DIR"

# --- profile resolution (refuses to guess) -----------------------------------

PROFILE=""
resolve_profile() {
  if [ -n "$ARG_PROFILE" ]; then PROFILE="$ARG_PROFILE"; return 0; fi
  if [ -n "${DEMO_PROFILE:-}" ]; then PROFILE="$DEMO_PROFILE"; return 0; fi
  if [ -f "$ROOT/.demo-cloud.env" ]; then
    # shellcheck disable=SC1091
    . "$ROOT/.demo-cloud.env"
    if [ -n "${DEMO_PROFILE:-}" ]; then PROFILE="$DEMO_PROFILE"; return 0; fi
  fi
  return 1
}

if [ "$LOCAL" -ne 1 ]; then
  if ! resolve_profile; then
    cat >&2 <<EOF
$(printf '%s\xe2\x9c\x97%s' "$RED" "$RST") No Expanso Cloud demo profile found, and --local was not given.

This demo deploys through Expanso Cloud onto a dedicated edge node. Set it up
once:

    just cloud-setup

That walks you through creating a network at https://cloud.expanso.io,
registering a demo node (label demo=telemetry-cost), and saving a profile. It
writes .demo-cloud.env so future runs just work.

Already have a profile? Pass it explicitly:

    ./scripts/demo.sh --profile <name>

Just want the offline path (no cloud)?

    ./scripts/demo.sh --local
EOF
    exit 1
  fi
fi

# --- helpers -----------------------------------------------------------------

pause() {  # $1 = prompt
  [ "$NO_PAUSE" -eq 1 ] && return 0
  printf '\n%s[Enter]%s %s ' "$BOLD" "$RST" "$1"
  IFS= read -r _ || true
}

dwell() { [ "$NO_PAUSE" -eq 1 ] && sleep "${1:-$DWELL_SECS}"; return 0; }

board_reset() { curl -fsS -X POST "$BOARD_URL/reset" >/dev/null 2>&1 || true; }

show_stats() {
  local stats_json
  stats_json=$(curl -fsS "$BOARD_URL/stats" 2>/dev/null) || { say "  (stats unavailable)"; return 0; }
  STATS_JSON="$stats_json" python3 <<'PY' || say "  (stats unavailable)"
import json, os
d = json.loads(os.environ["STATS_JSON"])
raw, hot, cold = d.get("raw", {}), d.get("hot", {}), d.get("cold", {})
print("  raw : events=%s bytes=%s" % (raw.get("events"), raw.get("bytes")))
print("  hot : events=%s bytes=%s cost_usd=%s" % (
    hot.get("events"), hot.get("bytes"), hot.get("cost_usd")))
print("  cold: events=%s bytes=%s" % (cold.get("events"), cold.get("bytes")))
print("  reduction_pct=%s" % d.get("reduction_pct"))
PY
}

open_board() {
  if [ "$NO_PAUSE" -eq 1 ]; then
    beat "dashboard: $BOARD_URL"
    return 0
  fi
  if command -v open >/dev/null 2>&1; then
    open "$BOARD_URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$BOARD_URL" >/dev/null 2>&1 || true
  fi
  beat "dashboard: $BOARD_URL"
}

jobpath() {  # $1 = basename -> path for the active mode
  if [ "$LOCAL" -eq 1 ]; then printf 'jobs/%s' "$1"; else printf 'jobs/cloud/%s' "$1"; fi
}

deploy_job() {  # $1 = path
  if [ "$LOCAL" -eq 1 ]; then
    expanso-cli job deploy "$1" --endpoint "$LOCAL_ENDPOINT" --force
  else
    expanso-cli job deploy "$1" --profile "$PROFILE" --force
  fi
}

stop_job() {  # $1 = job name (only ever our own names)
  if [ "$LOCAL" -eq 1 ]; then
    expanso-cli job stop "$1" --endpoint "$LOCAL_ENDPOINT" --force >/dev/null 2>&1 || true
  else
    expanso-cli job stop "$1" --profile "$PROFILE" --force >/dev/null 2>&1 || true
  fi
}

# The "it landed on the node" beat. Cloud only. The installed CLI (v2.1.17)
# filters executions by --job-id, not job name, and has no --watch, so we
# resolve the id via `job describe` and list once. Best-effort, never fatal.
show_execution() {  # $1 = job name
  [ "$LOCAL" -eq 1 ] && return 0
  local name="$1" jid
  jid=$(expanso-cli job describe "$name" --profile "$PROFILE" --format json 2>/dev/null \
    | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
def find_id(o):
    if isinstance(o, dict):
        for k in ("id", "ID", "Id"):
            if isinstance(o.get(k), str) and o[k]:
                return o[k]
        for v in o.values():
            r = find_id(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find_id(v)
            if r:
                return r
    return None
i = find_id(d)
if i:
    print(i)
' 2>/dev/null | head -1 || true)
  beat "control plane scheduling '$name' onto the demo node:"
  if [ -n "$jid" ]; then
    expanso-cli execution list --job-id "$jid" --profile "$PROFILE" || true
  else
    expanso-cli execution list --profile "$PROFILE" --limit 10 || true
  fi
}

# --- log-simulator streams ---------------------------------------------------

sims_up() {
  [ -f "$RUN_DIR/sim-app.pid" ] && kill -0 "$(cat "$RUN_DIR/sim-app.pid")" 2>/dev/null
}

scale_rate() {  # $1 = base rate, $2 = requested base; preserves the 8:6:10:2 mix
  local r=$(( $1 * $2 / 8 ))
  [ "$r" -lt 1 ] && r=1
  printf '%s' "$r"
}

start_sim() {  # $1 name, $2 logsim cmd, $3 extra args, $4 rate
  local name="$1" cmd="$2" extra="$3" rate="$4" pf="$RUN_DIR/sim-$1.pid"
  if [ -f "$pf" ] && kill -0 "$(cat "$pf")" 2>/dev/null; then return 0; fi
  # shellcheck disable=SC2086
  nohup uvx --from "$LOGSIM_SRC" "$cmd" $extra --rate "$rate" \
    --output "tcp://localhost:$SINK_PORT" > "$RUN_DIR/sim-$name.log" 2>&1 &
  echo $! > "$pf"
}

ensure_sims() {
  if sims_up; then
    beat "log streams already running -> tcp://localhost:$SINK_PORT"
    return 0
  fi
  if [ -n "$RATE" ]; then
    start_sim app   logsim-app   ""                       "$(scale_rate 8 "$RATE")"
    start_sim k8s   logsim-k8s   "--scenario crash-loop"  "$(scale_rate 6 "$RATE")"
    start_sim web   logsim-web   ""                       "$(scale_rate 10 "$RATE")"
    start_sim cloud logsim-cloud ""                       "$(scale_rate 2 "$RATE")"
    beat "log streams started (rate base $RATE) -> tcp://localhost:$SINK_PORT"
  else
    just -q sims
  fi
}

# --- scenario bodies ---------------------------------------------------------

deploy_scenario_and_intake() {  # $1 = scenario base job file (basename)
  deploy_job "$(jobpath "$1")"
  deploy_job "$(jobpath 00-intake.yaml)"
}

run_tax() {
  hdr "Scenario 1/?: Tax - the cost of shipping everything"
  board_reset
  deploy_scenario_and_intake 01-tax.yaml
  show_execution 01-tax
  ensure_sims
  open_board
  beat "CLAIM: with no filtering, every raw byte is metered into the hot lane."
  beat "WATCH: the dollar meter climbs; reduction_pct stays 0. That is the tax."
  dwell
  pause "let the meter run, then continue"
  say ""; say "stats:"; show_stats
}

run_audit() {
  hdr "Scenario: Audit - how much of what you index is never queried"
  board_reset
  deploy_scenario_and_intake 02-audit.yaml
  show_execution 02-audit
  ensure_sims
  open_board
  beat "CLAIM: still shipping everything, but a sample lane is tapped to disk."
  beat "WATCH: the meter runs as in Tax; the value comes from the audit table."
  dwell
  pause "let a sample collect, then run the audit table"
  say ""; say "audit table:"
  just -q audit 2>/dev/null || beat "(no sample yet; let it stream longer, then: just audit)"
  say ""; say "stats:"; show_stats
}

run_filter() {
  hdr "Scenario: Filter - tighten the rules one at a time (THE demo)"
  board_reset
  # Step 1 brings the scenario http_server up; intake feeds it. Steps 2-4 are
  # in-place redeploys of the SAME job name (03-filter), so each one bends the
  # line live without touching intake.
  beat "step 1/4: drop health checks + heartbeats"
  deploy_scenario_and_intake 03-filter-step1.yaml
  show_execution 03-filter
  ensure_sims
  open_board
  dwell; pause "watch the line bend, then go to step 2"
  say ""; say "stats after step 1:"; show_stats

  beat "step 2/4: + drop DEBUG"
  deploy_job "$(jobpath 03-filter-step2.yaml)"
  dwell; pause "watch the line bend, then go to step 3"
  say ""; say "stats after step 2:"; show_stats

  beat "step 3/4: + crash-loop dedupe"
  deploy_job "$(jobpath 03-filter-step3.yaml)"
  dwell; pause "watch the line bend, then go to step 4"
  say ""; say "stats after step 3:"; show_stats

  beat "step 4/4: + keep 100% ERROR/WARN and slow requests, sample 10% of the rest"
  deploy_job "$(jobpath 03-filter-step4.yaml)"
  dwell; pause "this is the headline reduction; continue when ready"
  say ""; say "stats after step 4:"; show_stats
}

iso_offset() {  # $1 = signed hours like -1 or +1
  if date -u -v"${1}H" +%Y-%m-%dT%H:%M:%SZ >/dev/null 2>&1; then
    date -u -v"${1}H" +%Y-%m-%dT%H:%M:%SZ
  else
    date -u -d "${1} hours" +%Y-%m-%dT%H:%M:%SZ
  fi
}

run_tiers() {
  hdr "Scenario: Tiers - hot/cold routing + rehydration"
  board_reset
  deploy_scenario_and_intake 04-tiers.yaml
  show_execution 04-tiers
  ensure_sims
  open_board
  beat "CLAIM: retain-but-not-signal goes to cheap cold storage; signal goes hot;"
  beat "       junk (health/heartbeat/DEBUG) is dropped before it costs anything."
  beat "WATCH: hot and cold lanes split; the hot meter stays low."
  dwell
  pause "let the lanes split, then run the auditor rehydrate beat"
  say ""; say "stats:"; show_stats

  local from to
  from="$(iso_offset -1)"; to="$(iso_offset +1)"
  beat "auditor calls: pull the last hour back out of cold storage"
  beat "  just rehydrate $from $to"
  just -q rehydrate "$from" "$to" 2>/dev/null \
    || beat "(nothing in cold yet; let tiers stream longer, then re-run the rehydrate line)"
}

# --- teardown ----------------------------------------------------------------

stop_board() {
  if [ -f "$RUN_DIR/board.pid" ] && kill -0 "$(cat "$RUN_DIR/board.pid")" 2>/dev/null; then
    kill "$(cat "$RUN_DIR/board.pid")" 2>/dev/null || true
    rm -f "$RUN_DIR/board.pid"
  fi
  pkill -f "costboard/server.py" 2>/dev/null || true
}

teardown() {
  hdr "Wrapping up"
  if [ "$KEEP" -eq 1 ]; then
    beat "--keep: leaving the dashboard, log streams, and jobs running for continued play"
    beat "stop everything later with: just clean"
    return 0
  fi
  just -q sims-stop >/dev/null 2>&1 || true
  beat "stopped log streams"
  for n in 00-intake 01-tax 02-audit 03-filter 04-tiers; do stop_job "$n"; done
  beat "stopped demo jobs"
  if [ "$LOCAL" -eq 1 ]; then
    beat "left the local edge + dashboard up (cheap, reused next run)"
    beat "fully tear down with: just clean"
  else
    stop_board
    beat "stopped the local dashboard"
    beat "left the demo node connected on profile '$PROFILE' (cheap, reused next run)"
    beat "the cloud node stays; nothing else to clean up"
  fi
}

# --- preflight gate ----------------------------------------------------------

hdr "Preflight"
if [ "$LOCAL" -eq 1 ]; then
  "$ROOT/scripts/preflight.sh" --tools \
    || die "tool checks failed (see above). Install the missing tools, then re-run."
else
  "$ROOT/scripts/preflight.sh" --profile "$PROFILE" \
    || die "cloud preflight failed for profile '$PROFILE'. Re-run: just cloud-setup"
fi

# --- stage: jobs + dashboard -------------------------------------------------

if [ "$LOCAL" -eq 1 ]; then
  hdr "Staging (local / offline path)"
  beat "starting a local edge ($LOCAL_ENDPOINT) and the costboard dashboard"
  just -q edge board
else
  hdr "Staging (cloud path)"
  beat "regenerating selector-scoped cloud jobs (jobs/cloud/)"
  just -q cloud-jobs || die "could not generate jobs/cloud/ - ensure the justfile has a cloud-jobs recipe"
  beat "starting the local costboard dashboard (the data-plane view)"
  just -q board
fi

# --- mental-model banner -----------------------------------------------------

hdr "Control plane vs data plane"
if [ "$LOCAL" -eq 1 ]; then
  say "LOCAL MODE (offline/CI). The control plane is bypassed: jobs deploy"
  say "straight to a local edge on $LOCAL_ENDPOINT. The cloud path is identical"
  say "except --profile replaces --endpoint and is validated offline."
else
  say "Control plane = Expanso Cloud. You deploy each job once; the cloud"
  say "schedules it onto every node matching the selector and manages its"
  say "lifecycle. You never SSH a node."
  say ""
  say "Data plane = the edge node. Here it runs on this laptop (a real"
  say "registered node, just nearby), so the log streams and the dashboard are"
  say "local while the orchestration is genuinely remote. The same job YAML"
  say "lands on a fleet of 400 the same way it lands on this one."
  say ""
  say "Profile: $PROFILE   Node label: demo=telemetry-cost"
fi

# --- run scenarios -----------------------------------------------------------

run_one() {
  case "$1" in
    tax)    run_tax ;;
    audit)  run_audit ;;
    filter) run_filter ;;
    tiers)  run_tiers ;;
  esac
}

if [ "$SCENARIO" = "all" ]; then
  for s in tax audit filter tiers; do run_one "$s"; done
else
  run_one "$SCENARIO"
fi

teardown

hdr "Done"
ok "demo complete (scenario: $SCENARIO, mode: $([ "$LOCAL" -eq 1 ] && echo local || echo "cloud/$PROFILE"))"
