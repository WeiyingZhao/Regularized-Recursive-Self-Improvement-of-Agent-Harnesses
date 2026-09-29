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
"""Complete score and cost evidence: a shortfall of trials is missing (never an absent
slot), malformed evidence is rejected, and an unknown Delta C is never admitted as 0.

Offline: no model call, no benchmark.

    python3 -m pytest -q tests/test_evidence_integrity.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_domain import make_run  # noqa: E402

from rrsi.config import ConfigError, RRSIConfig  # noqa: E402
from rrsi.evaluate import (EvalResult, EvaluationError, TaskResult, aggregate,  # noqa: E402
                           evaluate, relative_cost_change)
from rrsi.loop import _fmt_cov, _fmt_dc  # noqa: E402
from rrsi.selection import Candidate, Decision, select_round  # noqa: E402


def T(rewards, **kw) -> TaskResult:
    return TaskResult(rewards=list(rewards), **kw)


# ---- cardinality: a shortfall is missing trials, with the full denominator ----------

def test_one_success_at_k2_is_half_with_one_missing():
    ev = aggregate("j", 2, {"a": T([1.0])})            # the review's probe
    assert ev.S == 0.5 and ev.missing == 1 and ev.n_expected == 2
    tr = ev.per_task["a"]
    assert tr.rewards == [1.0, 0.0] and tr.weights == [1.0, 1.0] and tr.tokens == [None, None]
    assert tr.missing == 1


def test_padding_adds_to_the_adapters_own_missing_count():
    ev = aggregate("j", 3, {"a": T([1.0, 0.0], tokens=[10, None], missing=1)})
    assert ev.missing == 2 and ev.per_task["a"].rewards == [1.0, 0.0, 0.0]
    assert abs(ev.S - 1 / 3) < 1e-12


def test_padding_keeps_the_criteria_weighted_denominator():
    # workspace shape: weight = criteria total. b lost a trial; without padding the
    # denominator would shrink to 10 + 10 + 90 and S would rise from 15/200 to 15/110.
    per = {"a": T([0.5, 1.0], weights=[10, 10]), "b": T([0.0], weights=[90])}
    ev = aggregate("w", 2, per)
    assert ev.per_task["b"].weights == [90.0, 90.0] and ev.missing == 1
    assert abs(ev.S - 15 / 200) < 1e-12
    assert aggregate("w", 2, {"c": T([1.0], weights=[7])}).per_task["c"].weights == [7.0, 7.0]


def test_padding_does_not_mutate_the_adapters_objects():
    tr = T([1.0])
    aggregate("j", 2, {"a": tr})
    assert tr.rewards == [1.0] and tr.missing == 0


def test_more_than_k_rewards_is_an_error():
    with pytest.raises(EvaluationError, match="3 trials reported for k=2"):
        aggregate("j", 2, {"a": T([1.0, 0.0, 1.0])})


def test_missing_cannot_exceed_k_after_padding():
    with pytest.raises(EvaluationError, match="missing"):
        aggregate("j", 2, {"a": T([1.0], missing=2)})          # 1 pad + 2 reported > k
    with pytest.raises(EvaluationError, match="missing"):
        aggregate("j", 2, {"a": T([0.0, 0.0], missing=-1)})
    assert aggregate("j", 2, {"a": T([0.0, 0.0], missing=2)}).missing == 2


# ---- value checks ---------------------------------------------------------------------

@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, "1", None, True])
def test_non_finite_or_non_numeric_reward_is_rejected(bad):
    with pytest.raises(EvaluationError, match="not a finite number"):
        aggregate("j", 2, {"a": T([1.0, bad])})


@pytest.mark.parametrize("bad", [1.5, -0.1])
def test_reward_outside_unit_interval_is_rejected(bad):
    with pytest.raises(EvaluationError, match=r"outside \[0, 1\]"):
        aggregate("j", 2, {"a": T([1.0, bad])})
    aggregate("j", 2, {"a": T([0.0, 1.0])})                    # the closed ends are valid


def test_negative_or_non_finite_weight_is_rejected():
    with pytest.raises(EvaluationError, match="negative weight"):
        aggregate("j", 2, {"a": T([1.0, 0.0], weights=[1.0, -1.0])})
    with pytest.raises(EvaluationError, match="not a finite number"):
        aggregate("j", 2, {"a": T([1.0, 0.0], weights=[1.0, math.nan])})
    assert aggregate("j", 1, {"a": T([1.0], weights=[0.0]), "b": T([1.0])}).S == 1.0


def test_all_zero_weights_leave_S_undefined():
    with pytest.raises(EvaluationError, match="weight is zero"):
        aggregate("j", 1, {"a": T([1.0], weights=[0.0])})


def test_negative_or_non_numeric_tokens_are_rejected():
    for bad in (-5, math.inf, "12"):
        with pytest.raises(EvaluationError, match="token count"):
            aggregate("j", 1, {"a": T([1.0], tokens=[bad])})


def test_misaligned_weights_or_tokens_are_rejected():
    with pytest.raises(EvaluationError, match="must align"):
        aggregate("j", 2, {"a": T([1.0, 0.0], weights=[1.0])})
    with pytest.raises(EvaluationError, match="must align"):
        aggregate("j", 2, {"a": T([1.0, 0.0], tokens=[10, 20, 30])})
    with pytest.raises(EvaluationError, match="must align"):
        aggregate("j", 2, {"a": T([1.0, 0.0], tokens=[10])})


def test_empty_per_task_and_bad_k_are_errors():
    with pytest.raises(EvaluationError, match="no tasks"):
        aggregate("j", 2, {})
    for k in (0, -1, 1.5, True):
        with pytest.raises(EvaluationError, match="positive integer"):
            aggregate("j", k, {"a": T([1.0])})


def test_evaluation_error_is_a_value_error():
    assert issubclass(EvaluationError, ValueError)


# ---- expected_ids ---------------------------------------------------------------------

def test_expected_ids_reject_duplicates_and_empty():
    with pytest.raises(EvaluationError, match="duplicate"):
        aggregate("j", 1, {"a": T([1.0])}, expected_ids=["a", "a"])
    with pytest.raises(EvaluationError, match="no tasks were requested"):
        aggregate("j", 1, {"a": T([1.0])}, expected_ids=[])


def test_requested_but_unscored_task_is_k_missing_trials():
    ev = aggregate("j", 2, {"a": T([1.0, 1.0])}, expected_ids=["a", "b"])
    assert ev.n_expected == 4 and ev.missing == 2 and ev.S == 0.5
    b = ev.per_task["b"]
    assert b.rewards == [0.0, 0.0] and b.weights == [1.0, 1.0] and b.tokens == [None, None]
    assert b.missing == 2


def test_no_task_scored_at_all_is_all_missing_not_empty():
    ev = aggregate("j", 2, {}, expected_ids=["a", "b"])
    assert ev.S == 0.0 and ev.missing == 4 and ev.n_expected == 4 and ev.C is None


def test_unrequested_task_is_an_error():
    with pytest.raises(EvaluationError, match=r"not requested: \['z'\]"):
        aggregate("j", 1, {"a": T([1.0]), "z": T([1.0])}, expected_ids=["a"])


def test_result_follows_the_requested_order():
    ev = aggregate("j", 1, {"b": T([1.0]), "a": T([0.0])}, expected_ids=["a", "b"])
    assert list(ev.per_task) == ["a", "b"]


class _Domain:
    def __init__(self, per):
        self.per = per

    def run(self, *a, **kw):
        pass

    def score(self, runs_dir, job, ids, k):
        return self.per, {"n": 1}


def test_evaluate_passes_the_requested_ids(tmp_path):
    ev = evaluate(_Domain({"a": T([1.0, 1.0], tokens=[5, 5])}), tmp_path, tmp_path, "j",
                  ["a", "b"], 2)
    assert ev.n_expected == 4 and ev.missing == 2 and ev.extra == {"n": 1}
    with pytest.raises(EvaluationError, match="not requested"):
        evaluate(_Domain({"a": T([1.0]), "z": T([1.0])}), tmp_path, tmp_path, "j", ["a"], 1)


def test_shapes_the_domain_adapters_produce_are_accepted():
    # coding: missing trials already padded, tokens aligned with rewards
    coding = {"t": T([1.0, 0.0], tokens=[1200, None], missing=1, extra={"passes": 1})}
    # workspace: weights = criteria totals, a missing trial weighs its criteria count
    workspace = {"t": T([0.5, 0.0], weights=[8.0, 8.0], tokens=[900, None], missing=1)}
    # eng: rewards 0/1, tokens int or None
    eng = {"t": T([1.0, 0.0], tokens=[0, 300])}
    for per in (coding, workspace, eng):
        ev = aggregate("j", 2, per, expected_ids=["t"])
        assert ev.missing == per["t"].missing and ev.n_expected == 2
    assert aggregate("j", 2, coding).per_task["t"].extra == {"passes": 1}


# ---- token coverage ---------------------------------------------------------------------

def test_token_coverage_is_the_share_of_slots_C_averages():
    per = {"a": T([1.0, 0.0], tokens=[100, None]), "b": T([1.0, 1.0], tokens=[200, 0])}
    ev = aggregate("j", 2, per)
    assert ev.C == 150.0 and ev.token_coverage == 0.5           # 2 of 4 slots observed
    ev = aggregate("j", 2, {"a": T([1.0], tokens=[100])})       # the padded slot has no tokens
    assert ev.C == 100.0 and ev.token_coverage == 0.5
    ev = aggregate("j", 2, {"a": T([1.0, 1.0], tokens=[7, 9])})
    assert ev.token_coverage == 1.0
    ev = aggregate("j", 2, {"a": T([1.0, 1.0])})
    assert ev.C is None and ev.token_coverage == 0.0


def test_token_coverage_round_trips_and_old_artifacts_load_with_none(tmp_path):
    ev = aggregate("j", 2, {"a": T([1.0, 0.0], tokens=[10, None])})
    assert ev.to_json()["token_coverage"] == 0.5
    ev.save(tmp_path / "eval.json")
    assert EvalResult.load(tmp_path / "eval.json").token_coverage == 0.5
    old = ev.to_json()
    del old["token_coverage"]                                   # written before this field existed
    got = EvalResult.from_json(json.loads(json.dumps(old)))
    assert got.token_coverage is None and got.S == ev.S and got.C == ev.C
    assert got.per_task["a"].rewards == [1.0, 0.0]
    (tmp_path / "old.json").write_text(json.dumps(old))
    assert EvalResult.load(tmp_path / "old.json").token_coverage is None


def test_loading_old_evidence_is_not_revalidated():
    # an artifact written when aggregate trusted cardinality (one reward at k=2) still loads as is
    old = {"job": "j", "k": 2, "S": 1.0, "C": None, "n_expected": 2, "missing": 0,
           "per_task": {"a": {"rewards": [1.0], "weights": [1.0], "tokens": [None],
                              "missing": 0, "extra": {}}}}
    ev = EvalResult.from_json(old)
    assert ev.S == 1.0 and ev.missing == 0 and ev.per_task["a"].rewards == [1.0]


# ---- Delta C ----------------------------------------------------------------------------

def test_relative_cost_change_is_none_when_unknown():
    assert relative_cost_change(None, 100.0) is None
    assert relative_cost_change(100.0, None) is None
    assert relative_cost_change(None, None) is None
    assert relative_cost_change(100.0, 0.0) is None             # incumbent cost not positive
    assert relative_cost_change(100.0, -5.0) is None
    assert relative_cost_change(math.nan, 100.0) is None
    assert relative_cost_change(150.0, 100.0) == 0.5
    assert relative_cost_change(50.0, 100.0) == -0.5
    assert relative_cost_change(100.0, 100.0) == 0.0


def _ev(job, score, tokens, k=2, n=10):
    per = {f"t{i}": TaskResult(rewards=[score] * k, tokens=[tokens] * k) for i in range(n)}
    return aggregate(job, k, per)


def _cand(variant, ev, component="prompt"):
    return Candidate(variant, [{"id": "C1", "component": component}], ev=ev)


def _cfg(**kw):
    return RRSIConfig(beta0=0.1, beta1=40.0, w_s=100.0, w_c=15.0, w_n=0.5, **kw)


def test_unknown_candidate_cost_blocks_a_gaining_candidate_by_default():
    inc, gain = _ev("inc", 0.5, 1000.0), _ev("A", 0.7, None)
    win, decs = select_round([_cand("A", gain)], inc, 0.5, 0.05, _cfg(), {})
    d = decs[0]
    assert win is None and not d.admissible and d.delta_C is None and d.C is None
    assert d.reason.startswith("cost evidence incomplete")
    assert "candidate" in d.reason and "incumbent" not in d.reason
    assert "allow_unknown_cost" in d.reason
    d.to_json()                                                 # serializable with delta_C None
    json.dumps(d.to_json())


def test_unknown_incumbent_cost_is_named_and_blocks():
    inc, gain = _ev("inc", 0.5, None), _ev("A", 0.7, 900.0)
    win, decs = select_round([_cand("A", gain)], inc, 0.5, 0.05, _cfg(), {})
    assert win is None and "the incumbent" in decs[0].reason and "candidate" not in decs[0].reason
    win, decs = select_round([_cand("A", _ev("A", 0.7, None))], inc, 0.5, 0.05, _cfg(), {})
    assert "candidate and incumbent" in decs[0].reason


def test_allow_unknown_cost_admits_on_score_and_keeps_delta_C_unknown():
    inc, gain = _ev("inc", 0.5, 1000.0), _ev("A", 0.7, None)
    win, decs = select_round([_cand("A", gain)], inc, 0.5, 0.05,
                             _cfg(allow_unknown_cost=True), {})
    d = decs[0]
    assert win is not None and win.variant == "A" and d.admissible
    assert d.delta_C is None                                    # unknown is never a saving
    assert d.reason.endswith("(cost unknown; allow_unknown_cost override)")


def test_override_still_applies_the_floor_and_the_cost_rule():
    cfg = _cfg(allow_unknown_cost=True)
    inc = _ev("inc", 0.5, 1000.0)
    # below the noise-adjusted floor: rejected on score, not on cost
    _, decs = select_round([_cand("A", _ev("A", 0.3, None))], inc, 0.5, 0.05, cfg, {})
    assert decs[0].reason.startswith("below noise-adjusted floor")
    # inside the band with no structural novelty: the shaped rule at Delta C = 0 is not > 0
    _, decs = select_round([_cand("A", _ev("A", 0.5, None))], inc, 0.5, 0.05, cfg, {})
    assert not decs[0].admissible and decs[0].delta_C is None
    assert decs[0].reason.startswith("cost rule failed")
    assert decs[0].reason.endswith("(cost unknown; allow_unknown_cost override)")


def test_known_cost_is_judged_as_before_and_is_unaffected_by_the_override():
    inc = _ev("inc", 0.5, 1000.0)
    for allow in (False, True):
        win, decs = select_round([_cand("A", _ev("A", 0.7, 1100.0))], inc, 0.5, 0.05,
                                 _cfg(allow_unknown_cost=allow), {})
        d = decs[0]
        assert win is not None and d.admissible and abs(d.delta_C - 0.1) < 1e-12
        assert "cost unknown" not in d.reason


def test_a_gate_failure_still_has_no_cost():
    win, decs = select_round([Candidate("D", [], gate_failure="critic_reject")],
                             _ev("inc", 0.5, 1000.0), 0.5, 0.05, _cfg(), {})
    assert win is None and decs[0].reason == "critic_reject" and decs[0].delta_C is None


def test_partial_token_coverage_is_admitted_and_disclosed_in_the_decision():
    """No coverage threshold yet (a method decision left open): a candidate whose C
    averages half its trial slots is admitted, and the decision says so."""
    inc = _ev("inc", 0.5, 1000.0)
    half = aggregate("A", 2, {f"t{i}": TaskResult([0.7, 0.7], tokens=[900, None])
                              for i in range(10)})
    win, decs = select_round([_cand("A", half)], inc, 0.5, 0.05, _cfg(), {})
    d = decs[0]
    assert win is not None and d.admissible and d.token_coverage == 0.5
    assert json.loads(json.dumps(d.to_json()))["token_coverage"] == 0.5
    _, decs = select_round([Candidate("D", [], gate_failure="critic_reject")], inc, 0.5,
                           0.05, _cfg(), {})
    assert decs[0].token_coverage is None                      # nothing measured
    old = {k: v for k, v in d.to_json().items() if k != "token_coverage"}
    assert Decision(**old).token_coverage is None               # older decisions.json rows


def test_log_formatting_of_token_coverage():
    assert _fmt_cov(None) == "n/a" and _fmt_cov(1.0) == "1.00" and _fmt_cov(0.456) == "0.46"


# ---- config ----------------------------------------------------------------------------

def test_allow_unknown_cost_config_field():
    assert RRSIConfig().allow_unknown_cost is False
    RRSIConfig(allow_unknown_cost=True).validate()
    for bad in (1, "true", None):
        with pytest.raises(ConfigError, match="allow_unknown_cost"):
            RRSIConfig(allow_unknown_cost=bad).validate()


def test_allow_unknown_cost_loads_from_json(tmp_path):
    p = tmp_path / "rrsi.json"
    p.write_text(json.dumps({"allow_unknown_cost": True}))
    cfg = RRSIConfig.load(p)
    assert cfg.allow_unknown_cost is True and "allow_unknown_cost" not in cfg.notes
    p.write_text(json.dumps({"allow_unknown_cost": "yes"}))
    with pytest.raises(ConfigError, match="allow_unknown_cost"):
        RRSIConfig.load(p)


# ---- the loop --------------------------------------------------------------------------

def test_log_formatting_of_an_unknown_cost_change():
    assert _fmt_dc(None) == "n/a"
    assert _fmt_dc(0.1234) == "+0.123" and _fmt_dc(-0.5) == "-0.500"


def test_round_records_and_logs_token_coverage(tmp_path, monkeypatch, capsys):
    fx = make_run(tmp_path, monkeypatch)
    fx.run.baseline()
    fx.run.round(0)
    out = capsys.readouterr().out
    decs = json.loads((fx.run.runs / "r0" / "decisions.json").read_text())
    assert [d["token_coverage"] for d in decs] == [1.0, 1.0]
    assert out.count("cov=1.00") == 2


def test_no_success_traces_selects_only_the_failures(tmp_path, monkeypatch):
    fx = make_run(tmp_path, monkeypatch, n_fail_traces=1, n_success_traces=0)
    fx.run.baseline()
    per_task = fx.run.incumbent_eval(fx.run.frontier()).per_task
    assert len(fx.run.build_traces("base", per_task)) == 1
    fx.cfg.n_success_traces = 1
    assert len(fx.run.build_traces("base", per_task)) == 2


def _round_without_token_counts(tmp_path, monkeypatch, capsys, **cfg):
    fx = make_run(tmp_path, monkeypatch, **cfg)
    fx.domain.tokens = None                                     # the domain reports no token counts
    fx.run.baseline()
    fx.run.round(0)
    return fx, capsys.readouterr().out


def test_round_without_token_counts_rejects_by_default(tmp_path, monkeypatch, capsys):
    fx, out = _round_without_token_counts(tmp_path, monkeypatch, capsys)
    inc = fx.run.frontier()["incumbent"]
    assert inc["t"] == 0 and inc["C"] is None                    # nothing was admitted
    recs = [r for r in fx.run.history.records() if r["t"] == 0 and r["variant"] != "-"]
    assert recs and all(r["outcome"] == "REJECTED" and r["delta_C"] is None for r in recs)
    assert all(r["detail"].startswith("cost evidence incomplete") for r in recs)
    decs = json.loads((fx.run.runs / "r0" / "decisions.json").read_text())
    assert decs and all(d["delta_C"] is None and not d["admissible"] for d in decs)
    assert "dC=n/a" in out and "dC=None" not in out
    fx.run.readjudicate(0)                                      # its log line formats dC the same way
    assert "dC=n/a" in capsys.readouterr().out


def test_round_without_token_counts_admits_under_the_override(tmp_path, monkeypatch, capsys):
    fx, out = _round_without_token_counts(tmp_path, monkeypatch, capsys, allow_unknown_cost=True)
    assert fx.run.frontier()["incumbent"]["t"] == 1
    recs = [r for r in fx.run.history.records() if r["t"] == 0 and r["variant"] != "-"]
    assert any(r["outcome"] == "ACCEPTED" for r in recs)
    assert all(r["delta_C"] is None for r in recs)
    assert "dC=n/a" in out and "allow_unknown_cost override" in out
