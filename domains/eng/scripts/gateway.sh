#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Start / stop / check the frozen tool gateway.
# WORKSPACE_BASE defaults to <repo>/runs/eng/workspaces, the adapter's root under the
# default --runs. `start` records who owns the port in $WORKSPACE_BASE/.gateway-$PORT.json
# and refuses a port that is already served by anything without that identity.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$REPO"
PY="${AGENT_PYTHON:-${RRSI_AGENT_PYTHON:?set RRSI_AGENT_PYTHON}}"
PORT="${GATEWAY_PORT:-8996}"
WS="${WORKSPACE_BASE:-$(cd "$REPO/../.." && pwd)/runs/eng/workspaces}"
port_up() { "$PY" -c "import socket,sys;s=socket.socket();s.settimeout(1);sys.exit(0 if s.connect_ex(('127.0.0.1',$PORT))==0 else 1)"; }
identity_ok() {  # same test as gateway_identity_issue() in adapter.py
  "$PY" -c '
import json, os, sys
ws, port = sys.argv[1], int(sys.argv[2])
try:
    d = json.load(open(os.path.join(ws, f".gateway-{port}.json")))
    assert d["service"] == "rrsi-eng-gateway" and d["port"] == port
    assert os.path.realpath(d["workspace_base"]) == os.path.realpath(ws)
    try:
        os.kill(int(d["pid"]), 0)
    except PermissionError:
        pass
except Exception:
    sys.exit(1)
' "$WS" "$PORT"
}
case "${1:-start}" in
  start)
    if port_up; then
      identity_ok && { echo "[gateway] already up on $PORT"; exit 0; }
      echo "[gateway] ERROR: port $PORT is in use by a service not started for workspace root $WS" \
           "(no live identity file $WS/.gateway-$PORT.json). Stop it or set GATEWAY_PORT to a free port." >&2
      exit 1
    fi
    mkdir -p "${RRSI_LOGS:-logs}" "$WS"; WS="$(cd "$WS" && pwd -P)"
    rm -f "$WS/.gateway-$PORT.json"      # nothing listens, so any identity file is stale
    GATEWAY_PORT="$PORT" WORKSPACE_BASE="$WS" \
      setsid "$PY" "$REPO/mcp_gateway/gateway.py" >> "${RRSI_LOGS:-logs}/gateway.log" 2>&1 &
    GPID=$!
    for i in $(seq 1 40); do
      kill -0 "$GPID" 2>/dev/null || break
      if port_up; then
        "$PY" -c 'import json,sys;print(json.dumps({"service":"rrsi-eng-gateway","pid":int(sys.argv[3]),"port":int(sys.argv[2]),"workspace_base":sys.argv[1]}))' \
          "$WS" "$PORT" "$GPID" > "$WS/.gateway-$PORT.json.tmp" && mv "$WS/.gateway-$PORT.json.tmp" "$WS/.gateway-$PORT.json"
        echo "[gateway] up on $PORT (workspace base $WS)"; exit 0
      fi
      sleep 1
    done
    echo "[gateway] FAILED to come up; see logs/gateway.log"; exit 1;;
  stop)   pkill -f "mcp_gateway/gateway.py" && echo "[gateway] stopped";;
  status) "$PY" -c "import socket,sys;s=socket.socket();s.settimeout(1);print('UP' if s.connect_ex(('127.0.0.1',$PORT))==0 else 'DOWN')";;
esac
