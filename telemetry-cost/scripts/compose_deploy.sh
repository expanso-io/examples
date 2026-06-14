#!/bin/sh
# One-shot deployer used by the docker-compose demo profiles.
#
# Installs expanso-cli via the official installer (the documented deploy
# mechanism, https://get.expanso.io), waits for the edge service to report
# healthy, then deploys each job named on the command line:
#
#   expanso-cli job deploy <job.yaml> --endpoint http://edge:19010 --force
#
# The same job files deploy unchanged to a real fleet through Expanso Cloud
# (cloud.expanso.io). Endpoint paths verified against expanso-edge v2.1.17:
# health is GET /api/v1/health.
#
# Inside compose, costboard is reachable at http://costboard:8090 instead of
# localhost, so this script prefers a pre-generated jobs/compose/<name>
# variant (see `make compose-jobs`) and otherwise derives one on the fly with
# sed. The generator -> http_server hop stays on localhost: both jobs run
# inside the same edge container.
set -eu

EDGE_URL="${EDGE_URL:-http://edge:19010}"
JOBS_DIR="${JOBS_DIR:-/work/jobs}"

if ! command -v expanso-cli > /dev/null 2>&1; then
  # alpine base: need curl + bash for the installer
  if command -v apk > /dev/null 2>&1; then
    apk add --no-cache curl bash ca-certificates > /dev/null
  fi
  curl -fsSL https://get.expanso.io/cli/install.sh | EXPANSO_INSTALL_DIR=/usr/local/bin sh
fi

i=0
until curl -fsS "$EDGE_URL/api/v1/health" > /dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 120 ]; then
    echo "error: edge not healthy at $EDGE_URL after 120s" >&2
    exit 1
  fi
  sleep 1
done

for name in "$@"; do
  src="$JOBS_DIR/compose/$name"
  if [ ! -f "$src" ]; then
    if [ ! -f "$JOBS_DIR/$name" ]; then
      echo "error: $JOBS_DIR/$name not found" >&2
      exit 1
    fi
    mkdir -p /tmp/compose
    sed -e 's#http://localhost:8090#http://costboard:8090#g' \
        -e 's#http://127.0.0.1:8090#http://costboard:8090#g' \
        "$JOBS_DIR/$name" > "/tmp/compose/$name"
    src="/tmp/compose/$name"
  fi
  expanso-cli job deploy "$src" --endpoint "$EDGE_URL" --force
done
echo "deploy complete — dashboard: http://localhost:8090"
