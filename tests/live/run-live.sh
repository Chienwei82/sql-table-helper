#!/usr/bin/env bash
# Run the live suite inside the ODAC-enabled runner image.
#
#   ./tests/live/run-live.sh                       # full live suite
#   ./tests/live/run-live.sh tests/live/test_mssql_writes_live.py -v
#
# Assumes the SQL Server compose stack is already up (`cd tests/live && docker compose up -d`).
# The runner shares the compose network, so it reaches the server as `sqlserver` rather
# than through the published port.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
image="stsk-odbc-runner"
project="swissknife-live"

network="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' \
    "${project}-sqlserver-1" 2>/dev/null || true)"
if [ -z "$network" ]; then
    echo "The SQL Server container is not running. Start it with:" >&2
    echo "  cd $here && docker compose up -d" >&2
    exit 1
fi

docker build -q -t "$image" -f "$here/Dockerfile.runner" "$here" >/dev/null

exec docker run --rm --network "$network" \
    -v "$repo":/src -w /src \
    -e "SWISSKNIFE_TEST_DB_URL=mssql://sa:SwissKnife%212022_Test@sqlserver:1433/SwissKnifeSample" \
    "$image" uv run --group dev pytest -m live "$@"
