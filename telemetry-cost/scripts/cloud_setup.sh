#!/usr/bin/env bash
#
# cloud_setup.sh - one-time, interactive Expanso Cloud onboarding for the demo.
#
# It registers a DEDICATED edge node (label demo=telemetry-cost) in its own
# data dir so it never collides with any other Expanso node on this machine,
# saves a demo profile, and waits for the node to connect to the control plane.
# After this you deploy job YAMLs through cloud.expanso.io onto that node:
#   just demo
#
# Idempotent: if a connected demo node and a saved profile already exist, it
# skips straight to verification. Re-running with a fresh bootstrap token
# re-registers the node.
#
# Safety: this script only ever creates/uses the demo profile you name here
# (default 'telemetry-demo'). It never reads, writes, or deploys against any
# other profile.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Pre-fill secrets from a local .env (gitignored) so you do not have to paste
# them. Recognized keys: EXPANSO_EDGE_BOOTSTRAP_TOKEN, EXPANSO_CLI_ENDPOINT
# (or EXPANSO_ENDPOINT), EXPANSO_CLI_API_KEY (or EXPANSO_API_KEY). Anything not
# present here is prompted for interactively.
if [ -f "$ROOT/.env" ]; then
  set -a; . "$ROOT/.env"; set +a
fi

# Network control-plane endpoint. The SAME endpoint serves both node bootstrap
# (POST /api/v1/nodes/register) and the CLI, so we resolve it once here and use
# it for the bootstrap --url and the saved profile. Default the port to :9010
# (the Expanso Cloud control-plane port) when the endpoint carries none, which
# matches how saved profiles address a network. (The binary's built-in default
# bootstrap host is not used: it targets a global host that 404s for a
# per-network control plane.)
norm_endpoint() {  # echo the endpoint with :9010 appended if it has no port
  local e="$1"
  case "${e#*://}" in
    *:*) printf '%s' "$e" ;;
    *)   printf '%s:9010' "$e" ;;
  esac
}
CP_ENDPOINT="${EXPANSO_CLI_ENDPOINT:-${EXPANSO_ENDPOINT:-}}"
[ -n "$CP_ENDPOINT" ] && CP_ENDPOINT="$(norm_endpoint "$CP_ENDPOINT")"

PROFILE="${1:-${DEMO_PROFILE:-telemetry-demo}}"
EDGE_DATA="$ROOT/.edge-cloud"
RUN_DIR="$ROOT/.run"
PIDFILE="$RUN_DIR/cloud-edge.pid"
LOGFILE="$RUN_DIR/cloud-edge.log"
ENV_FILE="$ROOT/.demo-cloud.env"
LABELS_FILE="$EDGE_DATA/config.d/30-demo-labels.yaml"

if [ -t 1 ]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; BOLD=$'\033[1m'; RST=$'\033[0m'
else
  GREEN=""; RED=""; BOLD=""; RST=""
fi
say()  { printf '%s\n' "$1"; }
step() { printf '\n%s== %s ==%s\n' "$BOLD" "$1" "$RST"; }
ok()   { printf '%s\xe2\x9c\x93%s %s\n' "$GREEN" "$RST" "$1"; }
die()  { printf '%s\xe2\x9c\x97%s %s\n' "$RED" "$RST" "$1" >&2; exit 1; }

mkdir -p "$RUN_DIR"

# Echo CONNECTED / NONE / CLIERR for the demo node on the given profile.
node_state() {
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
    print("NONE"); raise SystemExit
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

profile_exists() {
  expanso-cli profile show "$1" >/dev/null 2>&1
}

write_env() {
  printf 'DEMO_PROFILE="%s"\n' "$PROFILE" > "$ENV_FILE"
  ok "wrote $ENV_FILE (DEMO_PROFILE=$PROFILE)"
}

print_node_row() {
  expanso-cli node list --profile "$PROFILE" \
    --label demo=telemetry-cost --wide 2>/dev/null || true
}

# --- step 1: tools -----------------------------------------------------------

step "1/8  Checking tools"
"$ROOT/scripts/preflight.sh" --tools || die "tool checks failed (see above) - install the missing tools and re-run"

# --- idempotency: already set up? -------------------------------------------

if profile_exists "$PROFILE" && [ "$(node_state "$PROFILE")" = "CONNECTED" ]; then
  step "Already set up"
  ok "profile '$PROFILE' exists and its demo node is connected"
  print_node_row
  write_env
  say ""
  ok "Setup already complete -> run: just demo"
  exit 0
fi

# --- step 2: bootstrap token -------------------------------------------------

step "2/8  Bootstrap token"
if [ -n "${EXPANSO_EDGE_BOOTSTRAP_TOKEN:-}" ]; then
  TOKEN="$EXPANSO_EDGE_BOOTSTRAP_TOKEN"
  ok "using bootstrap token from .env (EXPANSO_EDGE_BOOTSTRAP_TOKEN)"
else
  say "In your browser:"
  say "  1. Open https://cloud.expanso.io"
  say "  2. Create a network (e.g. 'telemetry-demo') if you do not have one"
  say "  3. Go to Nodes -> Add Node"
  say "  4. Copy the bootstrap token"
  printf '\nPaste the bootstrap token (input hidden): '
  IFS= read -r -s TOKEN || true
  printf '\n'
fi
[ -n "${TOKEN:-}" ] || die "no token entered"

# The bootstrap registers against the network control plane, so we need its
# endpoint now. Prompt if it was not supplied via .env.
if [ -z "$CP_ENDPOINT" ]; then
  printf '\nControl-plane endpoint (e.g. https://<network>.us2.cloud.expanso.io): '
  IFS= read -r ep || true
  [ -n "${ep:-}" ] || die "no endpoint entered"
  CP_ENDPOINT="$(norm_endpoint "$ep")"
fi
ok "control plane: $CP_ENDPOINT"

# --- step 3: bootstrap the edge node ----------------------------------------

step "3/8  Registering the demo edge node"
# --url targets the network's control plane (it serves /api/v1/nodes/register).
# An explicit non-cloud EXPANSO_EDGE_BOOTSTRAP_URL still wins for self-hosted.
BOOT_URL="$CP_ENDPOINT"
if [ -n "${EXPANSO_EDGE_BOOTSTRAP_URL:-}" ] && [[ "$EXPANSO_EDGE_BOOTSTRAP_URL" != *start.cloud.expanso.io* ]]; then
  BOOT_URL="$EXPANSO_EDGE_BOOTSTRAP_URL"
fi
expanso-edge bootstrap --token "$TOKEN" --data-dir "$EDGE_DATA" --url "$BOOT_URL" --force \
  || die "bootstrap failed. Note: the node needs a single-use bootstrap TOKEN (a JWT from Nodes -> Add Node), not a long-lived exp_bk_ bootstrap key. Copy a fresh token and re-run."
unset TOKEN
ok "edge node bootstrapped into $EDGE_DATA"

# --- step 4: node labels -----------------------------------------------------

step "4/8  Tagging the node (demo=telemetry-cost)"
OS_LABEL="$(uname -s | tr '[:upper:]' '[:lower:]')"
mkdir -p "$EDGE_DATA/config.d"
cat > "$LABELS_FILE" <<EOF
# Dedicated demo node. The 'demo: telemetry-cost' label is what every demo job
# selector targets, so these pipelines land ONLY here and never on a neighbor
# node in a shared network.
labels:
    demo: telemetry-cost
    os: $OS_LABEL
EOF
ok "wrote $LABELS_FILE (demo=telemetry-cost, os=$OS_LABEL)"

# --- step 5: start the node --------------------------------------------------

step "5/8  Starting the node"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  ok "demo node already running (pid $(cat "$PIDFILE"))"
else
  # --api-listen on a dedicated port: the node only connects OUTBOUND to the
  # control plane, but it still binds a local API. Pin it to 19011 so it never
  # collides with the default 9010 (often a tunnel) or the local demo edge
  # (19010, used by `just demo-local`). Override with EXPANSO_NODE_API_PORT.
  node_api="127.0.0.1:${EXPANSO_NODE_API_PORT:-19011}"
  nohup expanso-edge run --data-dir "$EDGE_DATA" --api-listen "$node_api" > "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  ok "demo node started (pid $(cat "$PIDFILE"), api $node_api, log $LOGFILE)"
fi

# --- step 6: control-plane endpoint + API key --------------------------------

step "6/8  Saving the CLI profile"
# Endpoint is already resolved (CP_ENDPOINT, with :9010). Only the API key is
# still needed. Accept the canonical name first, then the short alias.
API_KEY="${EXPANSO_CLI_API_KEY:-${EXPANSO_API_KEY:-}}"
if [ -n "$API_KEY" ]; then
  ok "using API key from .env"
else
  say "In Expanso Cloud, open the network's API access page and copy an API key"
  say "(starts with exp_ak_...). Tip: add EXPANSO_CLI_API_KEY to .env to skip this."
  printf '\nAPI key (input hidden): '
  IFS= read -r -s API_KEY || true
  printf '\n'
fi
[ -n "${API_KEY:-}" ] || die "no API key entered"

expanso-cli profile save "$PROFILE" --endpoint "$CP_ENDPOINT" --api-key "$API_KEY" \
  || die "profile save failed - re-check the endpoint and API key"
unset API_KEY
ok "saved profile '$PROFILE' -> $CP_ENDPOINT"

# --- step 7: wait for the node to connect ------------------------------------

step "7/8  Waiting for the node to connect (up to ~60s)"
connected=0
for _ in $(seq 1 30); do
  if [ "$(node_state "$PROFILE")" = "CONNECTED" ]; then
    connected=1; break
  fi
  sleep 2
done
if [ "$connected" -ne 1 ]; then
  say "Node has not reported connected yet."
  say "  - check the node log: $LOGFILE"
  say "  - confirm the endpoint/API key belong to the SAME network as the token"
  die "node did not connect within the timeout"
fi
ok "demo node is connected"
print_node_row

# --- step 8: persist + done --------------------------------------------------

step "8/8  Saving demo config"
write_env

say ""
ok "Setup done -> run: just demo"
