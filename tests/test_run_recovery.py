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
from rrsi.history import History  # noqa: E402
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


def refs(fx) -> str:
    """Every ref of the repository and its worktree list."""
    return (git(fx.repo, "for-each-ref", "--format=%(refname) %(objectname)") + "\n"
            + git(fx.repo, "worktree", "list", "--porcelain"))


def attribution(fx) -> list[dict]:
    p = fx.run.attribution_path
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


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
    assert {k: v for k, v in st.items() if k not in ("decisions", "history", "attribution")} == {
        "t": 0, "kind": "round", "phase": "done", "old_commit": fx.base,
        "new_commit": tip(fx), "frontier": fr}
    assert {r["variant"]: r["outcome"] for r in records(fx, 0)} == {"A": "LOST", "B": "ACCEPTED"}
    assert records(fx, 0) == st["history"] and attribution(fx) == st["attribution"]
    assert json.loads((fx.run.runs / "r0" / "decisions.json").read_text()) == st["decisions"]
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


@pytest.mark.parametrize("legacy", [False, True])
def test_crash_before_settlement_resumes_with_the_same_decision(tmp_path, monkeypatch, legacy):
    """Decided, settlement never started: nothing of round 0 is on disk beyond drafts
    and evaluations, and the re-run reuses them. The round's own records must never
    count as incumbent machinery (here the winner is admitted only through its novelty
    term), including the ACCEPTED row an older version wrote before settling."""
    fx = make_run(tmp_path, monkeypatch, m=1, score_fn=lambda t, v, s: s)
    fx.extra_line = "Memory(store)"          # re-tagged `memory`: structural, novel
    fx.run.baseline()
    crash_once(monkeypatch, Run, "_settle")
    with pytest.raises(Crash):
        fx.run.round(0)
    assert records(fx, 0) == [] and attribution(fx) == []
    assert not fx.run.settlement_path.exists()
    assert not (fx.run.runs / "r0" / "decisions.json").exists()
    if legacy:                                # what the pre-record code left behind
        fx.run.history.append_candidate(0, "A", [{"id": "C1", "component": "memory"}],
                                        "ACCEPTED", 0.0, 0.0, True, 0.5, 100.0, None)
    runs, calls = fx.domain.n_runs, dict(fx.calls)
    fx.fresh().round(0)
    fr = fx.run.frontier()
    assert fr["incumbent"]["variant"] == "A" and tip(fx) == G.full_rev(fx.repo, "fake/r0A")
    assert [r["outcome"] for r in records(fx, 0)] == ["ACCEPTED"]
    assert records(fx, 0)[0]["component"] == "memory"
    assert fx.domain.n_runs == runs and fx.calls == calls


@pytest.mark.parametrize("owner,name", [(History, "rewrite_round"),     # before history
                                        (G, "update_ref_cas"),          # before the ref
                                        (Run, "save_frontier")])        # before the frontier
def test_interrupted_round_settlement_recovers_decisions_and_history(fx, monkeypatch,
                                                                     owner, name):
    crash_once(monkeypatch, owner, name)
    with pytest.raises(Crash):
        fx.run.round(0)
    st = settlement(fx)
    assert (st["kind"], st["phase"]) == ("round", "pending")
    if owner is History:                     # nothing of round 0 in history before the record
        assert records(fx, 0) == []
    assert fx.fresh().recover() == 0
    assert fx.fresh().recover() is None
    fr = fx.run.frontier()
    assert fr == st["frontier"] and settlement(fx)["phase"] == "done"
    assert records(fx, 0) == st["history"]
    assert json.loads((fx.run.runs / "r0" / "decisions.json").read_text()) == st["decisions"]
    assert [r["variant"] for r in records(fx, 0) if r["accepted"]] == [fr["incumbent"]["variant"]]
    assert fr["incumbent"]["variant"] == "B" and tip(fx) == G.full_rev(fx.repo, "fake/r0B")
    assert [r["variant"] for r in fx.run.history.records()] == ["-", "A", "B"]
    assert attribution(fx) == st["attribution"]                        # applied once, not twice
    assert Counter(r["variant"] for r in attribution(fx)) == {"A": 1, "B": 1}


def test_pending_round_record_without_rows_still_recovers(fx, monkeypatch):
    """A record written before round records carried rows (history written before it)."""
    crash_once(monkeypatch, G, "update_ref_cas")
    with pytest.raises(Crash):
        fx.run.round(0)
    st = settlement(fx)
    legacy = {k: v for k, v in st.items() if k not in ("decisions", "history", "attribution")}
    fx.run.settlement_path.write_text(json.dumps(legacy))
    hist, att = fx.run.history.path.read_bytes(), fx.run.attribution_path.read_bytes()
    assert fx.fresh().recover() == 0
    assert fx.run.frontier() == st["frontier"] and tip(fx) == st["new_commit"]
    assert fx.run.history.path.read_bytes() == hist and fx.run.attribution_path.read_bytes() == att


def test_rerun_of_a_crashed_round_under_a_new_delta_records_the_new_decision(fx, monkeypatch):
    crash_once(monkeypatch, Run, "_settle")
    with pytest.raises(Crash):
        fx.run.round(0)
    fx.cfg.delta, fx.cfg.w_s = 0.5, 0.0      # in-band gains now buy nothing
    runs, calls = fx.domain.n_runs, dict(fx.calls)
    fx.fresh().round(0)
    fr = fx.run.frontier()
    assert tip(fx) == fx.base and fr["incumbent"]["job"] == "base"
    assert {r["variant"]: r["outcome"] for r in records(fx, 0)} == {"A": "REJECTED",
                                                                    "B": "REJECTED"}
    assert not any(r["accepted"] for r in records(fx, 0))
    assert not any(d["admissible"] for d in
                   json.loads((fx.run.runs / "r0" / "decisions.json").read_text()))
    assert not any(fx.run.history.incumbent_component_counts().values())   # no phantom edit
    assert fx.domain.n_runs == runs and fx.calls == calls              # drafts and evals reused


def test_round_whose_branch_moved_meanwhile_writes_nothing(fx, monkeypatch):
    unrelated = git(fx.repo, "commit-tree", "-m", "unrelated", git(fx.repo, "rev-parse", "HEAD^{tree}"))
    real = Run._counts_before

    def moved(self, t):                      # another writer moves the branch mid-round
        git(fx.repo, "update-ref", f"refs/heads/{BRANCH}", unrelated)
        return real(self, t)
    monkeypatch.setattr(Run, "_counts_before", moved)
    fr_bytes = fx.run.frontier_path.read_bytes()
    with pytest.raises(SystemExit, match=r"round 0 not settled: .*moved"):
        fx.run.round(0)
    assert not fx.run.settlement_path.exists() and records(fx, 0) == [] and attribution(fx) == []
    assert not (fx.run.runs / "r0" / "decisions.json").exists()
    assert fx.run.frontier_path.read_bytes() == fr_bytes and tip(fx) == unrelated


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


def test_baseline_refuses_to_reset_a_run_with_settled_rounds(fx):
    fx.fresh().baseline()                    # one trajectory entry: may be re-baselined
    fx.run.round(0)
    before, rs, runs = snapshot(fx), refs(fx), fx.domain.n_runs
    with pytest.raises(SystemExit, match=r"trajectory reaches t=1.*new --runs"):
        fx.fresh().baseline()
    assert snapshot(fx) == before and refs(fx) == rs and fx.domain.n_runs == runs


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


def readjudicate_to_h0(fx) -> None:
    """After round 0 (winner B): in-band gains now buy nothing, so H_1 = H_0."""
    fx.run.round(0)
    fx.cfg.delta, fx.cfg.w_s = 0.5, 0.0


@pytest.mark.parametrize("owner,name", [(History, "rewrite_round"),     # before history
                                        (G, "update_ref_cas"),          # before the ref
                                        (Run, "save_frontier")])        # before the frontier
def test_interrupted_readjudicate_recovers_to_one_consistent_state(fx, monkeypatch, owner, name):
    readjudicate_to_h0(fx)
    crash_once(monkeypatch, owner, name)
    with pytest.raises(Crash):
        fx.fresh().readjudicate(0)
    st = settlement(fx)
    assert (st["kind"], st["phase"], st["new_commit"]) == ("readjudicate", "pending", fx.base)
    if owner is History:                     # crashed before the rewrite: history untouched
        assert {r["variant"]: r["outcome"] for r in records(fx, 0)} == {"A": "LOST",
                                                                        "B": "ACCEPTED"}
    assert fx.run.frontier()["incumbent"]["variant"] == "B"          # not yet H_0
    assert fx.fresh().recover() == 0
    assert fx.fresh().recover() is None                               # idempotent
    fr = fx.run.frontier()
    assert settlement(fx)["phase"] == "done" and fr == st["frontier"]
    assert tip(fx) == fx.base and fr["incumbent"]["job"] == "base"
    assert fr["trajectory"][1]["commit"] == G.rev(fx.repo, fx.base)
    assert records(fx, 0) == st["history"]
    assert {r["variant"]: r["outcome"] for r in st["history"]} == {"A": "REJECTED", "B": "REJECTED"}
    assert json.loads((fx.run.runs / "r0" / "decisions.json").read_text()) == st["decisions"]
    assert not any(x["admissible"] for x in st["decisions"])
    assert [r["variant"] for r in fx.run.history.records()] == ["-", "A", "B"]


@pytest.mark.parametrize("failure", ["unresolvable", "crash_before_record"])
def test_readjudicate_failing_before_its_record_changes_nothing(fx, monkeypatch, failure):
    readjudicate_to_h0(fx)
    before, ref = snapshot(fx), tip(fx)
    assert {"history.jsonl", "r0/decisions.json", "frontier.json"} <= set(before)
    if failure == "unresolvable":            # H_0's commit no longer resolves at settle time
        real = G.full_rev
        monkeypatch.setattr(G, "full_rev", lambda cwd, r: real(cwd, r) if r == BRANCH else "")
        with pytest.raises(SystemExit, match="cannot resolve"):
            fx.fresh().readjudicate(0)
    else:
        crash_once(monkeypatch, Run, "_settle")
        with pytest.raises(Crash):
            fx.fresh().readjudicate(0)
    assert snapshot(fx) == before and tip(fx) == ref
    assert fx.fresh().recover() is None


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
