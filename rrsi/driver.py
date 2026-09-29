# Copyright 2026 The rrsi Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
"""Sequential driver: baseline + calibration if missing, then rounds until T.

A round is settled once the frontier trajectory has an entry for t+1. Each
iteration runs the earliest unsettled round and retries it on failure; it never
skips ahead, since round t+1 needs round t settled. `touch runs/<domain>/STOP`
stops before the next round (`drive` returns "stopped"; rerun to resume).
Consecutive infrastructure failures stop the driver so a broken environment
cannot burn the whole budget. "completed" means every round below T is settled.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

MAX_CONSECUTIVE_INFRA = 3


def settled_rounds(frontier_path: Path) -> int:
    if not frontier_path.exists():
        return -1
    fr = json.loads(frontier_path.read_text())
    return len(fr["trajectory"]) - 1          # trajectory has entries 0..settled


def drive(entry: Path, domain: str, runs_dir: Path, T: int, start: int = 0,
          extra_args: list[str] | None = None) -> str:
    """Returns "completed" or "stopped" (STOP file); any failure raises SystemExit(1)."""
    run_dir = runs_dir / domain
    fr = run_dir / "frontier.json"
    stop = run_dir / "STOP"
    calib = run_dir / "calibration.json"
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    base_args = [sys.executable, str(entry), "--domain", domain,
                 "--runs", str(runs_dir)] + list(extra_args or [])
    if not fr.exists():
        print(f"[driver:{domain}] baseline + calibrate", flush=True)
        with open(logs / "baseline.log", "a") as lf:
            r = subprocess.run(base_args + ["baseline"], stdout=lf, stderr=subprocess.STDOUT)
        if r.returncode != 0 or not fr.exists():
            print(f"[driver:{domain}] baseline failed (rc={r.returncode})", flush=True)
            sys.exit(1)
    if not calib.exists():
        print(f"[driver:{domain}] calibrate", flush=True)
        with open(logs / "calibrate.log", "a") as lf:
            r = subprocess.run(base_args + ["calibrate"], stdout=lf, stderr=subprocess.STDOUT)
        if r.returncode != 0 or not calib.exists():
            print(f"[driver:{domain}] calibration failed (rc={r.returncode}); "
                  f"see {logs / 'calibrate.log'}", flush=True)
            sys.exit(1)
    infra = 0
    while True:
        t = settled_rounds(fr)                # earliest unsettled round
        if stop.exists():
            print(f"[driver:{domain}] STOP present; stopped before round {t}; "
                  f"remove {stop} and rerun to resume", flush=True)
            return "stopped"
        if t >= T:
            break
        if start > t:
            print(f"[driver:{domain}] round {t} is unsettled but --start {start} skips it; "
                  f"rounds before {start} must be settled first", flush=True)
            sys.exit(1)
        log = logs / f"r{t}.log"
        print(f"[driver:{domain}] === round {t} ({time.strftime('%H:%M:%S')}) -> {log}",
              flush=True)
        with open(log, "a") as lf:
            r = subprocess.run(base_args + ["round", "--t", str(t)],
                               stdout=lf, stderr=subprocess.STDOUT)
        if r.returncode != 0 or settled_rounds(fr) <= t:
            infra += 1
            print(f"[driver:{domain}] round {t} did not settle (rc={r.returncode}, "
                  f"{infra}/{MAX_CONSECUTIVE_INFRA})", flush=True)
            if infra >= MAX_CONSECUTIVE_INFRA:
                print(f"[driver:{domain}] too many consecutive failures; stopping", flush=True)
                sys.exit(1)
        else:
            infra = 0
            b = json.loads(fr.read_text())["incumbent"]
            print(f"[driver:{domain}]   incumbent t={b['t']} {b['commit']} S={b['S']:.4f}",
                  flush=True)
    if settled_rounds(fr) < T:
        print(f"[driver:{domain}] only {settled_rounds(fr)}/{T} rounds settled", flush=True)
        sys.exit(1)
    print(f"[driver:{domain}] all rounds settled", flush=True)
    return "completed"
