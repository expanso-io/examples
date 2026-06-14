#!/usr/bin/env bash
#
# preflight.sh - the demo doctor.
#
# Checks the tools the demo needs (uv, expanso-edge, expanso-cli, python3,
# curl) and, unless --tools is passed, the Expanso Cloud readiness: a saved
# demo profile whose dedicated edge node (label demo=telemetry-cost) is
# connected to the control plane.
#
# Every line is a green check or a red x. Each failure prints an exact fix
# line underneath it. Exit 0 only when every required check passes; tools are
# always required, the cloud checks are required unless --tools is given.
#
# Read-only: this script never deploys, stops, or mutates anything. The only
# cloud call it makes is `expanso-cli node list` (read-only).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
usage: preflight.sh [--tools] [--profile NAME]

  --tools          check tools only (skip Expanso Cloud readiness)
  --profile NAME   demo profile to check (else $DEMO_PROFILE, else .demo-cloud.env)
EOF
}

TOOLS_ONLY=0
ARG_PROFILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tools) TOOLS_ONLY=1 ;;
    --profile) ARG_PROFILE="${2:-}"; shift ;;
    --profile=*) ARG_PROFILE="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'preflight: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ -t 1 ]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; BOLD=$'\033[1m'; RST=$'\033[0m'
else
  GREEN=""; RED=""; BOLD=""; RST=""
fi

FAILED=0
ok()  { printf '%s\xe2\x9c\x93%s %s\n' "$GREEN" "$RST" "$1"; }
bad() { printf '%s\xe2\x9c\x97%s %s\n' "$RED" "$RST" "$1"; FAILED=$((FAILED + 1)); }
fix() { printf '      fix: %s\n' "$1"; }

# --- tools -------------------------------------------------------------------

printf '%sTools%s\n' "$BOLD" "$RST"

if command -v uv >/dev/null 2>&1; then
  ok "uv ($(uv --version 2>/dev/null | head -1))"
else
  bad "uv not found (needed for uvx log-simulators)"
  fix "curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

if command -v expanso-edge >/dev/null 2>&1; then
  ok "expanso-edge ($(expanso-edge version 2>/dev/null | head -1))"
else
  bad "expanso-edge not found"
  fix "curl -fsSL https://get.expanso.io/cli/install.sh | sh"
fi

if command -v expanso-cli >/dev/null 2>&1; then
  ok "expanso-cli ($(expanso-cli version 2>/dev/null | head -1))"
else
  bad "expanso-cli not found"
  fix "curl -fsSL https://get.expanso.io/cli/install.sh | sh"
fi

if command -v python3 >/dev/null 2>&1; then
  ok "python3 ($(python3 --version 2>/dev/null | head -1))"
else
  bad "python3 not found (needed for the costboard dashboard)"
  fix "install Python 3.10+ (e.g. brew install python3)"
fi

if command -v curl >/dev/null 2>&1; then
  ok "curl ($(curl --version 2>/dev/null | head -1))"
else
  bad "curl not found"
  fix "install curl (e.g. brew install curl)"
fi

# --- cloud readiness ---------------------------------------------------------

resolve_profile() {
  # Resolution order: --profile > $DEMO_PROFILE > .demo-cloud.env
  if [ -n "$ARG_PROFILE" ]; then
    printf '%s' "$ARG_PROFILE"; return 0
  fi
  if [ -n "${DEMO_PROFILE:-}" ]; then
    printf '%s' "$DEMO_PROFILE"; return 0
  fi
  if [ -f "$ROOT/.demo-cloud.env" ]; then
    # shellcheck disable=SC1091
    . "$ROOT/.demo-cloud.env"
    if [ -n "${DEMO_PROFILE:-}" ]; then
      printf '%s' "$DEMO_PROFILE"; return 0
    fi
  fi
  return 1
}

# Echo CONNECTED / NONE / CLIERR / PARSEERR for the demo node on $1.
cloud_node_state() {
  local profile="$1" json
  if ! json=$(expanso-cli node list --profile "$profile" \
      --label demo=telemetry-cost --format json 2>/dev/null); then
    printf 'CLIERR'; return 0
  fi
  NODE_JSON="$json" python3 <<'PY'
import json, os
raw = os.environ.get("NODE_JSON", "")
try:
    d = json.loads(raw) if raw.strip() else []
except Exception:
    print("PARSEERR"); raise SystemExit

def states(o):
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, str) and k.lower() in (
                "state", "status", "connectionstate", "connection_state", "connection"
            ):
                out.append(v.lower())
        for v in o.values():
            out += states(v)
    elif isinstance(o, list):
        for v in o:
            out += states(v)
    return out

st = states(d)
connected = [s for s in st if any(t in s for t in ("connected", "healthy", "ready"))]
print("CONNECTED" if connected else "NONE")
PY
}

if [ "$TOOLS_ONLY" -ne 1 ]; then
  printf '\n%sExpanso Cloud%s\n' "$BOLD" "$RST"

  if PROFILE="$(resolve_profile)"; then
    if ! command -v expanso-cli >/dev/null 2>&1; then
      bad "cannot check cloud node: expanso-cli missing"
      fix "install expanso-cli first (see Tools above)"
    else
      state="$(cloud_node_state "$PROFILE")"
      case "$state" in
        CONNECTED)
          ok "demo node connected on profile '$PROFILE' (label demo=telemetry-cost)"
          ;;
        NONE)
          bad "no connected demo node on profile '$PROFILE' (label demo=telemetry-cost)"
          fix "start it: nohup expanso-edge run --data-dir $ROOT/.edge-cloud & (or re-run: make cloud-setup)"
          ;;
        CLIERR)
          bad "could not reach the control plane for profile '$PROFILE'"
          fix "check the profile: expanso-cli profile show $PROFILE  (or re-run: make cloud-setup)"
          ;;
        *)
          bad "unexpected node list output for profile '$PROFILE'"
          fix "inspect: expanso-cli node list --profile $PROFILE --label demo=telemetry-cost"
          ;;
      esac
    fi
  else
    bad "no demo profile (run make cloud-setup)"
    fix "make cloud-setup"
  fi
fi

# --- verdict -----------------------------------------------------------------

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  if [ "$TOOLS_ONLY" -eq 1 ]; then
    ok "all tool checks passed"
  else
    ok "all checks passed - ready to run: make demo"
  fi
  exit 0
fi
bad "$FAILED check(s) failed - fix the lines above, then re-run preflight"
exit 1
