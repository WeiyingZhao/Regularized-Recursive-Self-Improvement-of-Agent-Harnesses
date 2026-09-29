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
"""Offline workload preview: upper-bound arithmetic and the `plan` command.

    python3 -m pytest -q tests/test_planning.py
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rrsi.config import RRSIConfig  # noqa: E402
from rrsi.planning import (  # noqa: E402
    estimate_workload, failure_reason, render_workload, task_counts)


def _shipped(domain: str) -> RRSIConfig:
    return RRSIConfig.load(ROOT / "domains" / domain / "rrsi.json")


@pytest.mark.parametrize("domain, n_tasks, baseline, candidate", [
    ("coding", 89, 178, 7_120),
    ("workspace", 120, 240, 9_600),
    ("eng", 61, 244, 19_520),
])
def test_shipped_configs_trial_counts(domain, n_tasks, baseline, candidate):
    est = estimate_workload(_shipped(domain), n_tasks, 2)
    assert (est.baseline_trials, est.candidate_trials_max) == (baseline, candidate)
    assert f"{candidate:,}" in render_workload(est)


def test_unknown_task_counts_are_none_never_zero():
    est = estimate_workload(RRSIConfig(), None, None,
                            {"n_tasks": "no checkout", "n_smoke": "no checkout"})
    assert est.n_tasks is None and est.n_smoke is None
    assert est.baseline_trials is None
    assert est.candidate_trials_max is None
    assert est.smoke_trials_max is None
    assert est.search_invocations_per_round > 0           # independent of the task set
    out = render_workload(est)
    assert out.count("unknown") >= 5 and "no checkout" in out
    assert json.loads(json.dumps(asdict(est)))["candidate_trials_max"] is None


def test_smoke_trials_use_one_trial_per_task():
    est = estimate_workload(RRSIConfig(T=3, k=5, m=2), 10, 4)
    assert est.smoke_trials_max == 3 * 2 * 4        # k does not enter
    assert est.n_smoke == 4


def test_search_call_bound_computed_by_hand():
    # T=3, m=2, repair_rounds=1: per round 1 analyst + 2*(1+1) proposer + 2*(1+1) critic = 9
    est = estimate_workload(RRSIConfig(T=3, k=1, m=2, repair_rounds=1), 10, 2)
    assert est.analyst_invocations_per_round == 1
    assert est.proposer_invocations_per_round == 4 and est.critic_invocations_per_round == 4
    assert est.search_invocations_per_round == 9
    assert est.search_invocations_total == 27
    # no repair attempts: 1 + 1 + 1 for m=1
    est = estimate_workload(RRSIConfig(T=5, m=1, repair_rounds=0, m_draft=0), 10, 2)
    assert est.search_invocations_per_round == 3 and est.search_invocations_total == 15


def test_render_states_bounds_and_exclusions():
    out = render_workload(estimate_workload(_shipped("coding"), 89, 2))
    assert "upper-bound" in out and "not spend or latency" in out
    for excluded in ("judge calls", "retries", "infrastructure reruns"):
        assert excluded in out


def test_search_role_figures_are_invocations_not_model_calls():
    est = estimate_workload(_shipped("coding"), 89, 2)
    out = render_workload(est)
    assert "search-role invocations per round, max" in out
    assert "search-role invocations over T rounds, max" in out
    assert "search-role calls" not in out
    assert "invocations, not model calls or tokens" in out
    assert "digester" in out and "more than one model call" in out
    fields = asdict(est)
    assert {"analyst_invocations_per_round", "proposer_invocations_per_round",
            "critic_invocations_per_round", "search_invocations_per_round",
            "search_invocations_total"} <= set(fields)
    assert not [f for f in fields if "calls" in f]


class _Domain:
    def __init__(self, evolve, smoke):
        self._evolve, self._smoke = evolve, smoke

    def evolve_ids(self):
        return self._evolve()

    def smoke_ids(self):
        return self._smoke()


def _exit(msg):
    def f():
        raise SystemExit(msg)
    return f


def test_task_counts_reports_failures_instead_of_raising():
    n, s, why = task_counts(_Domain(lambda: ["a", "b", "c"], lambda: ["a"]))
    assert (n, s, why) == (3, 1, {})
    n, s, why = task_counts(_Domain(_exit("no checkout at x"), _exit("no checkout at x")))
    assert n is None and s is None
    assert why == {"n_tasks": "no checkout at x", "n_smoke": "no checkout at x"}
    n, s, why = task_counts(_Domain(lambda: ["a"], lambda: 1 / 0))
    assert n == 1 and s is None and "ZeroDivisionError" in why["n_smoke"]


def test_failure_reason_is_one_line():
    assert failure_reason(SystemExit("a\nb  c")) == "a b c"
    assert failure_reason(FileNotFoundError("gone")) == "FileNotFoundError: gone"


class _Chatty:
    """Adapters may print while loading a task set (workspace split() does on first use)."""
    name = "chatty"

    def evolve_ids(self):
        print('{"meta": "printed while loading evolve tasks"}')
        return ["a", "b"]

    def smoke_ids(self):
        print("printed while loading smoke tasks")
        return ["a"]


def _cli_module():
    spec = importlib.util.spec_from_file_location("rrsi_cli", ROOT / "rrsi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_adapter_output_does_not_pollute_stdout(capsys):
    assert task_counts(_Chatty()) == (2, 1, {})
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "printed while loading evolve tasks" in cap.err
    assert "printed while loading smoke tasks" in cap.err


def test_plan_json_stays_parseable_when_the_adapter_prints(capsys):
    _cli_module().cmd_plan(_Chatty(), RRSIConfig(T=2, k=3, m=1), as_json=True)
    cap = capsys.readouterr()
    d = json.loads(cap.out)                            # nothing but the payload on stdout
    assert (d["n_tasks"], d["n_smoke"], d["baseline_trials"]) == (2, 1, 6)
    assert "printed while loading" in cap.err


def _cli(*args, runs: Path):
    return subprocess.run([sys.executable, str(ROOT / "rrsi.py"), "--runs", str(runs), *args],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=120)


def test_plan_command_prints_the_coding_figures_offline(tmp_path):
    r = _cli("--domain", "coding", "plan", runs=tmp_path / "runs")
    assert r.returncode == 0, r.stderr
    assert "178" in r.stdout and "7,120" in r.stdout
    assert not (tmp_path / "runs").exists()          # no run directories


def test_plan_help_names_invocations_not_calls(tmp_path):
    r = _cli("--domain", "coding", "--help", runs=tmp_path / "runs")
    assert r.returncode == 0 and "search-role invocation counts" in " ".join(r.stdout.split())
    assert "trial and call counts" not in r.stdout


def test_plan_json_and_config_error(tmp_path):
    r = _cli("--domain", "coding", "plan", "--json", runs=tmp_path / "runs")
    d = json.loads(r.stdout)
    assert (d["n_tasks"], d["baseline_trials"], d["candidate_trials_max"]) == (89, 178, 7120)
    assert d["upper_bound"] is True
    r = _cli("--domain", "coding", "--k", "0", "plan", runs=tmp_path / "runs")
    assert r.returncode != 0 and "invalid configuration" in r.stderr
    assert not (tmp_path / "runs").exists()
