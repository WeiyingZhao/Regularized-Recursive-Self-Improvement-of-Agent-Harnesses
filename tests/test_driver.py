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
"""Driver: retries the earliest unsettled round, never skips, reports truthfully.

A fake entry script mimics rrsi.py's CLI (baseline, calibrate, round --t N); a JSON
scenario (path in $FAKE_RRSI_SCENARIO) scripts each invocation's outcome. No model
calls, no benchmark.

    python3 -m pytest -q tests/test_driver.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rrsi.driver import drive  # noqa: E402

DOMAIN = "fake"

# Outcomes per invocation key ("baseline", "calibrate", "round:N"), by attempt number;
# the last outcome repeats. ok: succeed; fail: exit 1 with no effect; noop: exit 0 with
# no effect. {"stop_after": N} makes round N drop the STOP file once it settles.
FAKE_ENTRY = '''
import argparse, json, os, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--domain")
ap.add_argument("--runs")
ap.add_argument("cmd")
ap.add_argument("--t", type=int)
a = ap.parse_args()

sc = json.loads(Path(os.environ["FAKE_RRSI_SCENARIO"]).read_text())
key = a.cmd if a.t is None else f"{a.cmd}:{a.t}"
log = Path(sc["log"])
seen = sum(1 for line in log.read_text().splitlines() if line == key) if log.exists() else 0
with open(log, "a") as f:
    f.write(key + "\\n")
print(f"fake {key} attempt {seen + 1}", flush=True)

outs = sc.get(key, ["ok"])
out = outs[min(seen, len(outs) - 1)]
if out == "fail":
    sys.exit(1)
if out == "noop":
    sys.exit(0)

run_dir = Path(a.runs) / a.domain
fr = run_dir / "frontier.json"
entry = lambda t: {"t": t, "S": 0.5 + 0.1 * t, "C": 1.0, "commit": f"c{t}", "job": f"j{t}"}
if a.cmd == "baseline":
    run_dir.mkdir(parents=True, exist_ok=True)
    fr.write_text(json.dumps({"trajectory": [entry(0)], "incumbent": entry(0)}))
    (run_dir / "calibration.json").write_text("{}")
elif a.cmd == "calibrate":
    (run_dir / "calibration.json").write_text("{}")
elif a.cmd == "round":
    d = json.loads(fr.read_text())
    if len(d["trajectory"]) != a.t + 1:
        sys.exit("precondition: round %d needs exactly %d settled entries" % (a.t, a.t + 1))
    d["trajectory"].append(entry(a.t + 1))
    d["incumbent"] = entry(a.t + 1)
    fr.write_text(json.dumps(d))
    if sc.get("stop_after") == a.t:
        (run_dir / "STOP").write_text("")
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    entry = tmp_path / "fake_rrsi.py"
    entry.write_text(FAKE_ENTRY)
    runs = tmp_path / "runs"
    scenario = tmp_path / "scenario.json"
    log = tmp_path / "invocations.log"
    monkeypatch.setenv("FAKE_RRSI_SCENARIO", str(scenario))

    class Env:
        run_dir = runs / DOMAIN

        @staticmethod
        def script(**outcomes):
            scenario.write_text(json.dumps({"log": str(log), **outcomes}))

        @staticmethod
        def seed(settled, calibrated=True):
            """A run dir with `settled` rounds already settled (baseline done)."""
            Env.run_dir.mkdir(parents=True)
            traj = [{"t": t, "S": 0.5, "C": 1.0, "commit": f"c{t}", "job": f"j{t}"}
                    for t in range(settled + 1)]
            (Env.run_dir / "frontier.json").write_text(
                json.dumps({"trajectory": traj, "incumbent": traj[-1]}))
            if calibrated:
                (Env.run_dir / "calibration.json").write_text("{}")

        @staticmethod
        def drive(T, start=0):
            return drive(entry, DOMAIN, runs, T, start)

        @staticmethod
        def calls():
            return log.read_text().splitlines() if log.exists() else []

        @staticmethod
        def rounds():
            return [int(c.split(":")[1]) for c in Env.calls() if c.startswith("round:")]

        @staticmethod
        def settled():
            return len(json.loads((Env.run_dir / "frontier.json").read_text())["trajectory"]) - 1

    Env.script()
    return Env


@pytest.mark.parametrize("bad", ["fail", "noop"])
def test_transient_failure_retries_same_round(env, capsys, bad):
    env.script(**{"round:0": [bad, "ok"]})
    assert env.drive(T=2) == "completed"
    assert env.rounds() == [0, 0, 1]
    assert env.settled() == 2
    assert "all rounds settled" in capsys.readouterr().out
    assert len((env.run_dir / "logs" / "r0.log").read_text().splitlines()) == 2   # appended


@pytest.mark.parametrize("bad", ["fail", "noop"])
def test_permanent_failure_stops_without_advancing(env, capsys, bad):
    env.script(**{"round:0": [bad]})
    with pytest.raises(SystemExit) as e:
        env.drive(T=2)
    assert e.value.code == 1
    assert env.rounds() == [0, 0, 0]
    assert env.settled() == 0
    out = capsys.readouterr().out
    assert "all rounds settled" not in out
    assert "too many consecutive failures" in out


def test_rc0_without_trajectory_entry_is_failure(env, capsys):
    env.script(**{"round:0": ["noop", "ok"]})
    assert env.drive(T=1) == "completed"
    out = capsys.readouterr().out
    assert "round 0 did not settle (rc=0, 1/3)" in out


def test_failure_counter_resets_after_success(env):
    env.script(**{"round:0": ["fail", "fail", "ok"], "round:1": ["fail", "fail", "ok"]})
    assert env.drive(T=3) == "completed"
    assert env.rounds() == [0, 0, 0, 1, 1, 1, 2]


def test_stop_file_stops_before_any_round(env, capsys):
    env.seed(0)
    (env.run_dir / "STOP").write_text("")
    assert env.drive(T=2) == "stopped"
    assert env.rounds() == []
    out = capsys.readouterr().out
    assert "STOP present" in out and "before round 0" in out
    assert "all rounds settled" not in out


def test_stop_file_created_mid_run_names_next_round(env, capsys):
    env.script(stop_after=0)
    assert env.drive(T=3) == "stopped"
    assert env.rounds() == [0]
    out = capsys.readouterr().out
    assert "before round 1" in out
    assert "all rounds settled" not in out


def test_settled_rounds_are_skipped(env):
    env.seed(1)
    assert env.drive(T=3) == "completed"
    assert env.rounds() == [1, 2]


def test_everything_settled_runs_nothing(env, capsys):
    env.seed(2)
    assert env.drive(T=2) == "completed"
    assert env.calls() == []
    assert "all rounds settled" in capsys.readouterr().out


def test_start_beyond_settled_rounds_exits(env, capsys):
    env.seed(1)
    with pytest.raises(SystemExit) as e:
        env.drive(T=3, start=2)
    assert e.value.code == 1
    assert env.rounds() == []
    out = capsys.readouterr().out
    assert "round 1 is unsettled" in out
    assert "all rounds settled" not in out


def test_start_at_or_below_settled_rounds_is_fine(env):
    env.seed(1)
    assert env.drive(T=2, start=1) == "completed"
    assert env.rounds() == [1]


@pytest.mark.parametrize("bad", ["fail", "noop"])
def test_calibration_failure_exits_before_any_round(env, capsys, bad):
    env.seed(0, calibrated=False)
    env.script(calibrate=[bad])
    with pytest.raises(SystemExit) as e:
        env.drive(T=2)
    assert e.value.code == 1
    assert env.calls() == ["calibrate"]
    out = capsys.readouterr().out
    assert "calibration failed" in out
    assert "all rounds settled" not in out


def test_missing_calibration_is_run_then_rounds_proceed(env):
    env.seed(0, calibrated=False)
    assert env.drive(T=1) == "completed"
    assert env.calls() == ["calibrate", "round:0"]


def test_fresh_run_baselines_first(env):
    assert env.drive(T=1) == "completed"
    assert env.calls() == ["baseline", "round:0"]
