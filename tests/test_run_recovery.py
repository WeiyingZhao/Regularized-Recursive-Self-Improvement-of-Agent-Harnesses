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
"""Recoverable settlement, round preconditions and the writer lock.

Rounds run end to end on the offline fake domain (tests/fake_domain.py) in a
temporary Git repository; crashes are injected at each settlement phase. No model
calls, no benchmark.

    python3 -m pytest -q tests/test_run_recovery.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_domain import ROOT, git, make_run  # noqa: E402

from rrsi import gitops as G  # noqa: E402
from rrsi.loop import Run  # noqa: E402
from rrsi.state import RunLock  # noqa: E402

BRANCH = "evolve/fake"


class Crash(Exception):
    """Injected process death."""


def crash_once(monkeypatch, owner, name: str) -> None:
    real = getattr(owner, name)
    armed = [True]

    def wrapper(*a, **kw):
        if armed[0]:
            armed[0] = False
            raise Crash(name)
        return real(*a, **kw)
    monkeypatch.setattr(owner, name, wrapper)


def tip(fx) -> str:
    return G.full_rev(fx.repo, BRANCH)


def settlement(fx) -> dict:
    return json.loads(fx.run.settlement_path.read_text())


def records(fx, t: int) -> list[dict]:
    return [r for r in fx.run.history.records() if r["t"] == t and r["variant"] != "-"]


def snapshot(fx) -> dict:
    """Bytes of every run-state file (worktrees aside)."""
    runs = fx.run.runs
    return {str(p.relative_to(runs)): p.read_bytes() for p in sorted(runs.rglob("*"))
            if p.is_file() and p.relative_to(runs).parts[0] != "wt"}


@pytest.fixture
def fx(tmp_path, monkeypatch):
    fx = make_run(tmp_path, monkeypatch)
    fx.run.baseline()
    fx.base = tip(fx)
    return fx


def test_round_settles_branch_frontier_and_history_together(fx):
    fx.run.round(0)
    fr, st = fx.run.frontier(), settlement(fx)
    assert [x["t"] for x in fr["trajectory"]] == [0, 1]
    assert fr["incumbent"]["variant"] == "B" and fr["incumbent"]["S"] == pytest.approx(0.6)
    assert tip(fx) == G.full_rev(fx.repo, "fake/r0B") == st["new_commit"]
    assert fr["incumbent"]["commit"] == G.rev(fx.repo, BRANCH)
    assert fr["incumbent"]["harness_tree"] == fx.run.harness_tree(BRANCH)
    assert st == {"t": 0, "kind": "round", "phase": "done", "old_commit": fx.base,
                  "new_commit": tip(fx), "frontier": fr}
    assert {r["variant"]: r["outcome"] for r in records(fx, 0)} == {"A": "LOST", "B": "ACCEPTED"}
    assert not any((fx.run.wt_root / f"r0{v}").exists() for v in "AB")


def test_crash_after_ref_update_is_recovered_by_the_next_round_call(fx, monkeypatch):
    crash_once(monkeypatch, Run, "save_frontier")
    with pytest.raises(Crash):
        fx.run.round(0)
    st = settlement(fx)
    assert st["phase"] == "pending" and st["old_commit"] == fx.base
    assert tip(fx) == st["new_commit"] == G.full_rev(fx.repo, "fake/r0B")   # branch moved,
    assert len(fx.run.frontier()["trajectory"]) == 1                        # frontier did not
    runs, calls = fx.domain.n_runs, dict(fx.calls)

    fx.fresh().round(0)                     # a new process: recovers and returns
    fr = fx.run.frontier()
    assert [x["t"] for x in fr["trajectory"]] == [0, 1] and fr == st["frontier"]
    assert tip(fx) == st["new_commit"] and fr["incumbent"]["commit"] == G.rev(fx.repo, BRANCH)
    assert settlement(fx)["phase"] == "done"
    assert Counter(r["variant"] for r in records(fx, 0)) == {"A": 1, "B": 1}
    assert fx.domain.n_runs == runs and fx.calls == calls                 # no paid work
    assert not (fx.run.wt_root / "r0B").exists()


def test_explicit_recover_then_round_is_refused_as_settled(fx, monkeypatch):
    crash_once(monkeypatch, Run, "save_frontier")
    with pytest.raises(Crash):
        fx.run.round(0)
    new = settlement(fx)["new_commit"]
    runs, calls = fx.domain.n_runs, dict(fx.calls)
    run = fx.fresh()
    assert run.recover() == 0
    assert run.recover() is None                                          # idempotent
    assert [x["t"] for x in run.frontier()["trajectory"]] == [0, 1] and tip(fx) == new
    with pytest.raises(SystemExit, match="round 0 is already settled"):
        run.round(0)
    assert fx.domain.n_runs == runs and fx.calls == calls


def test_crash_before_ref_update_recovery_moves_the_branch(fx, monkeypatch):
    crash_once(monkeypatch, G, "update_ref_cas")
    with pytest.raises(Crash):
        fx.run.round(0)
    st = settlement(fx)
    assert st["phase"] == "pending" and tip(fx) == fx.base != st["new_commit"]
    assert len(fx.run.frontier()["trajectory"]) == 1
    assert fx.fresh().recover() == 0
    assert tip(fx) == st["new_commit"] == G.full_rev(fx.repo, "fake/r0B")
    assert fx.run.frontier() == st["frontier"] and settlement(fx)["phase"] == "done"


def test_crash_before_settlement_resumes_with_the_same_decision(tmp_path, monkeypatch):
    """History written, settlement never started: the re-run reuses every draft and
    evaluation and must not count the round's own ACCEPTED record as incumbent
    machinery (here the winner is admitted only through its novelty term)."""
    fx = make_run(tmp_path, monkeypatch, m=1, score_fn=lambda t, v, s: s)
    fx.extra_line = "Memory(store)"          # re-tagged `memory`: structural, novel
    fx.run.baseline()
    crash_once(monkeypatch, Run, "_settle")
    with pytest.raises(Crash):
        fx.run.round(0)
    assert [r["outcome"] for r in records(fx, 0)] == ["ACCEPTED"]
    assert records(fx, 0)[0]["component"] == "memory"
    assert not fx.run.settlement_path.exists()
    runs, calls = fx.domain.n_runs, dict(fx.calls)
    fx.fresh().round(0)
    fr = fx.run.frontier()
    assert fr["incumbent"]["variant"] == "A" and tip(fx) == G.full_rev(fx.repo, "fake/r0A")
    assert [r["outcome"] for r in records(fx, 0)] == ["ACCEPTED"]
    assert fx.domain.n_runs == runs and fx.calls == calls


def test_recovery_refuses_a_branch_moved_elsewhere(fx, monkeypatch):
    crash_once(monkeypatch, G, "update_ref_cas")
    with pytest.raises(Crash):
        fx.run.round(0)
    unrelated = git(fx.repo, "commit-tree", "-m", "unrelated", git(fx.repo, "rev-parse", "HEAD^{tree}"))
    git(fx.repo, "update-ref", f"refs/heads/{BRANCH}", unrelated)
    before, runs = snapshot(fx), fx.domain.n_runs
    with pytest.raises(SystemExit, match="moved unexpectedly"):
        fx.fresh().recover()
    with pytest.raises(SystemExit, match="moved unexpectedly"):
        fx.fresh().round(0)
    assert snapshot(fx) == before and tip(fx) == unrelated and fx.domain.n_runs == runs
    assert settlement(fx)["phase"] == "pending"


def test_settled_round_is_refused_without_changing_anything(fx):
    fx.run.round(0)
    fx.run.round(1)
    fr = fx.run.frontier()
    assert [x["t"] for x in fr["trajectory"]] == [0, 1, 2] and fr["incumbent"]["S"] == pytest.approx(0.7)
    before, ref, runs, calls = snapshot(fx), tip(fx), fx.domain.n_runs, dict(fx.calls)
    assert {"frontier.json", "history.jsonl", "r0/A/eval.json", "r0/B/eval.json",
            "r1/A/eval.json", "r1/B/eval.json", "jobs/base/eval.json"} <= set(before)
    with pytest.raises(SystemExit, match=r"round 0 is already settled.*readjudicate"):
        fx.fresh().round(0)
    assert snapshot(fx) == before and tip(fx) == ref
    assert fx.domain.n_runs == runs and fx.calls == calls


def test_reevaluate_with_a_later_round_keeps_every_evaluation(fx):
    fx.run.round(0)
    fx.run.round(1)
    before, ref, runs = snapshot(fx), tip(fx), fx.domain.n_runs
    for op in (fx.fresh().reevaluate, fx.fresh().readjudicate):
        with pytest.raises(SystemExit, match="rounds after 0 exist"):
            op(0)
    assert all((fx.run.runs / "r0" / v / "eval.json").exists() for v in "AB")
    assert snapshot(fx) == before and tip(fx) == ref and fx.domain.n_runs == runs


def test_round_with_every_candidate_invalid_is_not_settled(fx):
    fr_bytes = fx.run.frontier_path.read_bytes()
    fx.domain.all_missing = True
    with pytest.raises(SystemExit, match="every screened candidate .* failed evaluation"):
        fx.run.round(0)
    assert fx.run.frontier_path.read_bytes() == fr_bytes and tip(fx) == fx.base
    assert records(fx, 0) == [] and not fx.run.settlement_path.exists()
    assert not (fx.run.runs / "r0" / "decisions.json").exists()
    # the evaluator works again: the re-run reuses analysis and drafts, then settles
    fx.domain.all_missing = False
    calls = dict(fx.calls)
    fx.fresh().round(0)
    assert fx.calls == calls
    assert fx.run.frontier()["incumbent"]["variant"] == "B"


def test_round_with_some_candidates_invalid_still_settles(fx):
    fx.domain.missing_jobs = {"r0B"}
    fx.run.round(0)
    assert {r["variant"]: r["outcome"] for r in records(fx, 0)} == {"A": "ACCEPTED",
                                                                    "B": "eval_invalid"}
    assert fx.run.frontier()["incumbent"]["variant"] == "A"


def test_readjudicate_settles_through_the_settlement_record(fx, monkeypatch):
    fx.run.round(0)
    fx.cfg.delta, fx.cfg.w_s = 0.5, 0.0      # in-band gains now buy nothing: H_1 = H_0
    crash_once(monkeypatch, Run, "save_frontier")
    with pytest.raises(Crash):
        fx.fresh().readjudicate(0)
    st = settlement(fx)
    assert (st["kind"], st["phase"], st["new_commit"]) == ("readjudicate", "pending", fx.base)
    assert tip(fx) == fx.base                # moved back; the frontier still says B
    assert fx.run.frontier()["incumbent"]["variant"] == "B"
    assert fx.fresh().recover() == 0
    fr = fx.run.frontier()
    assert fr["incumbent"]["job"] == "base" and fr["trajectory"][1]["commit"] == G.rev(fx.repo, fx.base)
    assert {r["variant"]: r["outcome"] for r in records(fx, 0)} == {"A": "REJECTED",
                                                                    "B": "REJECTED"}


# ---- the writer lock ------------------------------------------------------------------
def test_second_writer_is_refused(tmp_path):
    lock = tmp_path / "runs" / "fake" / ".lock"
    with RunLock(lock):
        assert lock.read_text().strip() == str(os.getpid())
        with pytest.raises(SystemExit, match=rf"domain fake: .*\(pid {os.getpid()}\)"):
            with RunLock(lock):
                pass
    with RunLock(lock):                       # released on exit
        pass


def _cli(runs: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "rrsi.py"), "--domain", "coding",
                           "--runs", str(runs), *args], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=120)


def test_cli_mutating_commands_take_the_lock_and_status_does_not(tmp_path):
    runs = tmp_path / "runs"
    with RunLock(runs / "coding" / ".lock"):
        r = _cli(runs, "round", "--t", "0")
        assert r.returncode == 1 and f"(pid {os.getpid()})" in r.stderr
        r = _cli(runs, "status")                # no lock: fails on the missing frontier instead
        assert r.returncode == 1 and "another rrsi writer" not in r.stderr
        assert "frontier.json" in r.stderr
