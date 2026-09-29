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
"""Evaluation provenance: cached reuse and held-out labels are bound to (commit, k, tasks).

Runs on the offline fake domain (tests/fake_domain.py) in a temporary Git repository;
no model calls, no benchmark.

    python3 -m pytest -q tests/test_evaluation_provenance.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_domain import IDS, POLICY, ROOT, git, make_run, write_score  # noqa: E402

from rrsi import gitops as G  # noqa: E402
from rrsi.evaluate import EvalResult, TaskResult, aggregate, evaluate  # noqa: E402
from rrsi.provenance import (SCHEMA, ProvenanceError, fingerprint, manifest_path,  # noqa: E402
                             mismatches)
from rrsi.selection import Candidate  # noqa: E402

REL = "domains/fake/harness"


@pytest.fixture
def fx(tmp_path, monkeypatch):
    fx = make_run(tmp_path, monkeypatch)
    fx.commit_a = git(fx.repo, "rev-parse", "HEAD")
    write_score(fx.repo / POLICY, 0.7)                  # a second, different harness
    git(fx.repo, "commit", "-q", "-am", "harness B")
    fx.commit_b = git(fx.repo, "rev-parse", "HEAD")
    fx.ids = list(IDS)
    return fx


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


def fp(fx, ref=None, ids=None, k=2):
    return fingerprint(fx.repo, ref or fx.commit_a, REL, ids or fx.ids, k)


# ------------------------------------------------------------- fingerprint --
def test_fingerprint_is_stable_and_sensitive_to_every_identity_field(fx):
    base = fp(fx)
    assert base == fp(fx) == fp(fx, ref=fx.commit_a[:10])       # a short ref resolves to the same commit
    assert base["schema"] == SCHEMA and base["commit"] == fx.commit_a and base["k"] == 2
    assert base["n_tasks"] == 3
    assert base["task_ids_sha256"] == hashlib.sha256(b"t1\nt2\nt3").hexdigest()
    assert base["harness_tree"] == G.tree_hash(fx.repo, fx.commit_a, REL)

    other = {"commit": fp(fx, ref=fx.commit_b), "k": fp(fx, k=3),
             "order": fp(fx, ids=["t2", "t1", "t3"]), "subset": fp(fx, ids=["t1", "t2"]),
             "member": fp(fx, ids=["t1", "t2", "t4"])}
    assert len({json.dumps(v, sort_keys=True) for v in [base, *other.values()]}) == 6
    assert other["commit"]["harness_tree"] != base["harness_tree"]
    assert other["order"]["n_tasks"] == 3 and other["order"]["task_ids_sha256"] != base["task_ids_sha256"]


def test_fingerprint_of_an_unresolvable_ref_is_an_error(fx):
    with pytest.raises(ProvenanceError, match="no-such-ref"):
        fp(fx, ref="no-such-ref")


def test_mismatches_names_each_differing_field():
    a = {"schema": 1, "commit": "c1", "k": 2, "n_tasks": 3}
    assert mismatches(a, dict(a)) == []
    assert mismatches(a, {**a, "k": 3, "commit": "c0"}) == [
        "commit: recorded c0 != expected c1", "k: recorded 3 != expected 2"]
    assert mismatches(a, {k: v for k, v in a.items() if k != "schema"}) == []   # schema: one side only
    assert mismatches(a, {**a, "schema": 2}) == ["schema: recorded 2 != expected 1"]
    assert mismatches(a, {"commit": "c1", "k": 2}) == ["n_tasks: recorded None != expected 3"]


def test_manifest_lives_outside_the_job_directory(tmp_path):
    p = manifest_path(tmp_path / "runs" / "d", "r0A")
    assert p == tmp_path / "runs" / "d" / "manifests" / "r0A.json"
    assert "jobs" not in p.parts


# ----------------------------------------------------------------- evaluate --
def test_evaluate_writes_the_manifest_before_the_runner_starts(fx, tmp_path):
    seen = []
    real = fx.domain.run

    def run(root, runs_dir, job, ids, k, log_prefix=""):
        mp = manifest_path(runs_dir, job)
        seen.append(json.loads(mp.read_text()) if mp.exists() else None)
        real(root, runs_dir, job, ids, k, log_prefix=log_prefix)
    fx.domain.run = run
    prov = fp(fx)
    ev = evaluate(fx.domain, fx.repo, tmp_path, "j", fx.ids, 2, provenance=prov)
    assert seen == [prov]                                       # already on disk inside run()
    assert json.loads(manifest_path(tmp_path, "j").read_text()) == prov
    assert ev.provenance == prov
    ev.save(tmp_path / "eval.json")
    assert EvalResult.load(tmp_path / "eval.json").provenance == prov
    assert not any(p.name.endswith(".tmp") for p in (tmp_path / "manifests").iterdir())


def test_evaluate_warns_when_it_adopts_trials_of_a_job_without_a_manifest(fx, tmp_path, capsys):
    evaluate(fx.domain, fx.repo, tmp_path, "fresh", fx.ids, 2, provenance=fp(fx))
    assert "WARNING" not in capsys.readouterr().out                    # a new job: nothing to adopt
    (tmp_path / "jobs" / "old").mkdir(parents=True)                     # legacy or unknown origin
    evaluate(fx.domain, fx.repo, tmp_path, "old", fx.ids, 2, provenance=fp(fx))
    out = capsys.readouterr().out
    assert "WARNING" in out and "adopt" in out and str(tmp_path / "jobs" / "old") in out
    assert json.loads(manifest_path(tmp_path, "old").read_text()) == fp(fx)


def test_evalresult_save_replaces_the_file_atomically(tmp_path, monkeypatch):
    p = tmp_path / "eval.json"
    aggregate("j", 2, {"a": TaskResult([1.0, 1.0])}).save(p)
    old = p.read_bytes()

    def dies(*a, **kw):
        raise OSError("killed before the rename")
    with monkeypatch.context() as m:
        m.setattr(os, "replace", dies)
        with pytest.raises(OSError):
            aggregate("j", 2, {"a": TaskResult([0.0, 0.0])}).save(p)
    assert p.read_bytes() == old and sorted(os.listdir(tmp_path)) == ["eval.json"]
    assert EvalResult.load(p).S == 1.0


def test_evaluate_without_provenance_writes_no_manifest(fx, tmp_path):
    ev = evaluate(fx.domain, fx.repo, tmp_path, "j", fx.ids, 2)
    assert ev.provenance is None and not (tmp_path / "manifests").exists()


def test_evalresult_loads_a_legacy_file_without_provenance(fx, tmp_path):
    ev = evaluate(fx.domain, fx.repo, tmp_path, "j", fx.ids, 2)
    d = ev.to_json()
    d.pop("provenance")
    assert EvalResult.from_json(d).provenance is None


def test_evaluate_refuses_a_job_recorded_under_a_different_k(fx, tmp_path):
    prov = fp(fx)
    evaluate(fx.domain, fx.repo, tmp_path, "j", fx.ids, 2, provenance=prov)
    before, calls = snapshot(tmp_path), fx.domain.n_runs
    with pytest.raises(ProvenanceError, match=r"k: recorded 2 != expected 3") as e:
        evaluate(fx.domain, fx.repo, tmp_path, "j", fx.ids, 3, provenance=fp(fx, k=3))
    assert fx.domain.n_runs == calls and snapshot(tmp_path) == before   # runner untouched, evidence kept
    msg = str(e.value)                                                  # a remedy that works:
    assert str(manifest_path(tmp_path, "j")) in msg and str(tmp_path / "jobs" / "j") in msg
    assert "eval.json" in msg and "together" in msg and "only some" in msg
    assert "new job name" not in msg                                    # candidate names are fixed
    ev = evaluate(fx.domain, fx.repo, tmp_path, "j", fx.ids, 2, provenance=prov)   # same identity: resumes
    assert fx.domain.n_runs == calls + 1 and ev.provenance == prov


def test_evaluate_refuses_a_job_recorded_for_another_commit_or_task_set(fx, tmp_path):
    evaluate(fx.domain, fx.repo, tmp_path, "j", fx.ids, 2, provenance=fp(fx))
    calls = fx.domain.n_runs
    with pytest.raises(ProvenanceError, match="commit: recorded"):
        evaluate(fx.domain, fx.repo, tmp_path, "j", fx.ids, 2, provenance=fp(fx, ref=fx.commit_b))
    with pytest.raises(ProvenanceError, match="task_ids_sha256"):
        evaluate(fx.domain, fx.repo, tmp_path, "j", fx.ids[::-1], 2,
                 provenance=fp(fx, ids=fx.ids[::-1]))
    assert fx.domain.n_runs == calls


def test_evaluate_keeps_an_unreadable_manifest_and_does_not_run(fx, tmp_path):
    mp = manifest_path(tmp_path, "j")
    mp.parent.mkdir(parents=True)
    mp.write_text("{not json")
    with pytest.raises(ProvenanceError, match="unreadable"):
        evaluate(fx.domain, fx.repo, tmp_path, "j", fx.ids, 2, provenance=fp(fx))
    assert mp.read_text() == "{not json" and fx.domain.n_runs == 0


# ------------------------------------------------------------ held-out labels --
def test_heldout_label_reused_for_another_ref_is_refused(fx):
    fx.run.heldout("x", fx.ids, ref=fx.commit_a)
    calls, before = fx.domain.n_runs, snapshot(fx.run.runs / "jobs")
    manifest = manifest_path(fx.run.runs, "heldout_x").read_bytes()
    with pytest.raises(SystemExit) as e:
        fx.run.heldout("x", fx.ids, ref=fx.commit_b)
    assert "heldout x" in str(e.value) and fx.commit_a in str(e.value) and fx.commit_b in str(e.value)
    assert fx.domain.n_runs == calls                                  # the runner was not invoked
    assert snapshot(fx.run.runs / "jobs") == before                   # the old results are intact
    assert manifest_path(fx.run.runs, "heldout_x").read_bytes() == manifest


def test_heldout_label_reused_for_another_k_or_task_set_is_refused(fx):
    fx.run.heldout("x", fx.ids, ref=fx.commit_a)
    calls = fx.domain.n_runs
    with pytest.raises(SystemExit, match=r"k: recorded 2 != expected 5"):
        fx.run.heldout("x", fx.ids, ref=fx.commit_a, k=5)
    with pytest.raises(SystemExit, match="n_tasks: recorded 3 != expected 2"):
        fx.run.heldout("x", fx.ids[:2], ref=fx.commit_a)
    assert fx.domain.n_runs == calls


def test_heldout_same_label_and_inputs_reuses_the_job(fx):
    first = fx.run.heldout("x", fx.ids, ref=fx.commit_a)
    manifest = manifest_path(fx.run.runs, "heldout_x").read_bytes()
    again = fx.fresh().heldout("x", fx.ids, ref=fx.commit_a[:12])     # same commit, other spelling
    assert (again.S, again.C, again.n_expected) == (first.S, first.C, first.n_expected)
    assert again.provenance == first.provenance == fp(fx)
    assert manifest_path(fx.run.runs, "heldout_x").read_bytes() == manifest
    assert EvalResult.load(fx.run.eval_path("heldout_x")).provenance == first.provenance


def test_heldout_job_directory_without_a_manifest_needs_a_new_label(fx):
    job = fx.run.jobs / "heldout_old"
    job.mkdir()
    (job / "eval.json").write_text('{"legacy": true}')
    before = snapshot(fx.run.runs / "jobs")
    with pytest.raises(SystemExit, match=r"no evaluation manifest.*--label"):
        fx.run.heldout("old", fx.ids, ref=fx.commit_a)
    assert fx.domain.n_runs == 0 and snapshot(fx.run.runs / "jobs") == before
    assert not (fx.run.runs / "manifests").exists()


def test_heldout_defaults_to_the_branch_tip_and_rejects_an_unknown_ref(fx):
    fx.run.ensure_branch()
    ev = fx.run.heldout("tip", fx.ids)
    assert ev.provenance["commit"] == G.full_rev(fx.repo, fx.run.branch)
    with pytest.raises(SystemExit, match="no-such-ref"):
        fx.run.heldout("bad", fx.ids, ref="no-such-ref")
    assert not (fx.run.jobs / "heldout_bad").exists()


# ------------------------------------------------- baseline and candidate jobs --
def test_baseline_records_provenance_and_refuses_a_changed_k(fx):
    fx.run.ensure_branch()
    ev = fx.run.baseline()
    tip = G.full_rev(fx.repo, fx.run.branch)
    assert ev.provenance == fingerprint(fx.repo, tip, REL, fx.ids, 2)
    assert EvalResult.load(fx.run.eval_path("base")).provenance == ev.provenance
    fx.cfg.k = 3
    calls = fx.domain.n_runs
    with pytest.raises(SystemExit, match=r"baseline: .*k: recorded 2 != expected 3"):
        fx.fresh().baseline()
    assert fx.domain.n_runs == calls


def candidates(fx, t=0):
    out = {}
    for v in "AB":
        prep = json.loads((fx.run.runs / f"r{t}" / v / "prep.json").read_text())
        out[v] = Candidate(v, prep["edits"], branch=prep["branch"], commit=prep["commit"])
    return out


@pytest.fixture
def played(fx):
    """Baseline and round 0 done; every candidate has a cached eval.json with provenance."""
    fx.run.baseline()
    fx.run.round(0)
    fx.rdir = fx.run.runs / "r0"
    return fx


def test_round_records_each_candidates_identity(played):
    fx = played
    for v, c in candidates(fx).items():
        ev = EvalResult.load(fx.rdir / v / "eval.json")
        assert ev.provenance == fingerprint(fx.repo, c.commit, REL, fx.ids, 2)
        assert ev.provenance["commit"] == G.full_rev(fx.repo, c.branch)
        assert json.loads(manifest_path(fx.run.runs, f"r0{v}").read_text()) == ev.provenance


def test_cached_candidate_eval_with_matching_provenance_is_reused(played):
    fx = played
    c, calls = candidates(fx)["A"], fx.domain.n_runs
    fx.run._evaluate(0, c, fx.rdir, fx.ids)
    assert c.ev is not None and c.gate_failure is None and fx.domain.n_runs == calls


def test_cached_candidate_eval_with_mismatched_provenance_is_refused(played):
    fx = played
    ep = fx.rdir / "A" / "eval.json"
    d = json.loads(ep.read_text())
    d["provenance"]["k"] = 9
    ep.write_text(json.dumps(d))
    before, calls, c = ep.read_bytes(), fx.domain.n_runs, candidates(fx)["A"]
    with pytest.raises(SystemExit) as e:
        fx.run._evaluate(0, c, fx.rdir, fx.ids)
    assert str(ep) in str(e.value) and "k: recorded 9 != expected 2" in str(e.value)
    assert ep.read_bytes() == before and fx.domain.n_runs == calls and c.ev is None
    msg = str(e.value)                              # name all three artifacts, moved together
    assert str(manifest_path(fx.run.runs, "r0A")) in msg and str(fx.run.jobs / "r0A") in msg
    assert "together" in msg and "only some" in msg and "Remove it deliberately" not in msg


def test_cached_candidate_eval_for_another_commit_is_refused(played):
    fx = played
    a, b = candidates(fx)["A"], candidates(fx)["B"]
    wrong = Candidate("A", a.edits, branch=b.branch, commit=b.commit)   # A's eval.json, B's commit
    with pytest.raises(SystemExit, match="commit: recorded"):
        fx.run._evaluate(0, wrong, fx.rdir, fx.ids)
    assert wrong.ev is None


def test_legacy_candidate_eval_without_provenance_is_reused_with_a_warning(played, capsys):
    fx = played
    ep = fx.rdir / "A" / "eval.json"
    d = json.loads(ep.read_text())
    del d["provenance"]
    ep.write_text(json.dumps(d))
    c, calls = candidates(fx)["A"], fx.domain.n_runs
    capsys.readouterr()
    fx.run._evaluate(0, c, fx.rdir, fx.ids)
    out = capsys.readouterr().out
    assert c.ev is not None and c.ev.provenance is None and fx.domain.n_runs == calls
    assert "WARNING" in out and str(ep) in out and "provenance" in out


def test_a_reused_job_name_propagates_out_of_evaluate_and_is_not_an_eval_failure(played):
    fx = played
    (fx.rdir / "A" / "eval.json").unlink()
    mp = manifest_path(fx.run.runs, "r0A")
    rec = json.loads(mp.read_text())
    rec["k"] = 9
    mp.write_text(json.dumps(rec))
    c, calls = candidates(fx)["A"], fx.domain.n_runs
    with pytest.raises(ProvenanceError, match="k: recorded 9 != expected 2"):
        fx.run._evaluate(0, c, fx.rdir, fx.ids)
    assert c.gate_failure is None and fx.domain.n_runs == calls
    assert json.loads(mp.read_text())["k"] == 9                        # the manifest is not rewritten


def test_a_manifest_mismatch_aborts_the_round_without_settling_it(fx):
    fx.run.baseline()
    mp = manifest_path(fx.run.runs, "r0A")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"schema": SCHEMA, "commit": "0" * 40, "harness_tree": "x",
                              "k": 2, "n_tasks": 3, "task_ids_sha256": "y"}))
    with pytest.raises(ProvenanceError, match="commit: recorded " + "0" * 40):
        fx.run.round(0)
    assert [x["t"] for x in fx.run.frontier()["trajectory"]] == [0]   # round 0 is not settled
    assert not (fx.run.runs / "r0" / "A" / "eval.json").exists()
    assert json.loads(mp.read_text())["commit"] == "0" * 40


# ------------------------------------------------------------- CLI boundary --
def _cli_module():
    spec = importlib.util.spec_from_file_location("rrsi_cli", ROOT / "rrsi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_reports_a_provenance_conflict_as_a_clean_exit(tmp_path, monkeypatch):
    """A ProvenanceError out of a mutating command is an operator error: one message and
    exit status 1, not a traceback."""
    cli = _cli_module()
    (tmp_path / "rrsi.json").write_text("{}")

    class ConflictingRun:
        def __init__(self, domain, cfg, repo, runs_root):
            self.runs = Path(runs_root) / domain.name

        def round(self, t, dry_run=False):
            raise ProvenanceError("job r0A: manifests/r0A.json records a different evaluation")
    monkeypatch.setattr(cli, "load_domain", lambda name: SimpleNamespace(name=name, root=tmp_path))
    monkeypatch.setattr(cli, "Run", ConflictingRun)
    monkeypatch.setattr(sys, "argv", ["rrsi.py", "--domain", "fake", "--runs",
                                      str(tmp_path / "runs"), "round", "--t", "0"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert isinstance(e.value.code, str) and "\n" not in e.value.code
    assert e.value.code.startswith("round: ") and "job r0A" in e.value.code
