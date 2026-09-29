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
"""Evaluate(H', D_evolve, k): the empirical score and cost of Eq. (estimate).

    S_hat(H) = 1/(k|D|) sum_x sum_j r(x, tau_x^(j))
    C_hat(H) = 1/(k|D|) sum_x sum_j c(tau_x^(j))

with r in [0,1] and c the policy tokens of a trajectory. A missing trial (crash,
timeout, infrastructure) contributes r = 0 with the full denominator, never an
absent slot, so a candidate cannot look better by destroying the trials it
finds hard.

Rewards may carry weights. Every coding / engineering trial has weight 1; a
Harvey LAB trial has reward = criteria passed / criteria total and weight =
criteria total, so S_hat is the fraction of criteria passed over all tasks, the
benchmark's own metric (Appendix, Harvey LAB).

`aggregate` does not trust adapter cardinality: it checks every task against the
requested set, pads any task with fewer than k trials as missing (never as an
absent slot), and rejects non-finite or out-of-range evidence. Delta C is unknown
(None), never 0, when either side lacks a positive token count.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from numbers import Real
from pathlib import Path

from .provenance import ProvenanceError, manifest_path, mismatches, move_aside, read_manifest
from .state import atomic_write_json


class EvaluationError(ValueError):
    """Trial evidence that cannot be folded into S_hat and C_hat."""


@dataclass
class TaskResult:
    rewards: list[float]                  # one per trial, missing trials -> 0.0
    weights: list[float] = field(default_factory=list)   # default 1.0 each
    tokens: list = field(default_factory=list)           # int or None per trial
    missing: int = 0
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.weights:
            self.weights = [1.0] * len(self.rewards)
        if not self.tokens:
            self.tokens = [None] * len(self.rewards)

    @property
    def mean(self) -> float:
        w = sum(self.weights)
        return (sum(r * x for r, x in zip(self.rewards, self.weights)) / w) if w else 0.0


@dataclass
class EvalResult:
    job: str
    k: int
    per_task: dict                        # task_id -> TaskResult
    S: float                              # S_hat
    C: float | None                       # C_hat (None if no token counts)
    n_expected: int
    missing: int
    extra: dict = field(default_factory=dict)   # domain aggregates (pass counts, ...)
    token_coverage: float | None = None   # share of the n_expected slots that C_hat averages
    provenance: dict | None = None        # rrsi.provenance.fingerprint; None on legacy files

    def to_json(self) -> dict:
        d = {"job": self.job, "k": self.k, "S": self.S, "C": self.C,
             "n_expected": self.n_expected, "missing": self.missing,
             "extra": self.extra, "token_coverage": self.token_coverage,
             "provenance": self.provenance,
             "per_task": {t: asdict(r) for t, r in self.per_task.items()}}
        return d

    @classmethod
    def from_json(cls, d: dict) -> "EvalResult":
        per = {t: TaskResult(**r) for t, r in d["per_task"].items()}
        return cls(job=d["job"], k=d["k"], per_task=per, S=d["S"], C=d["C"],
                   n_expected=d["n_expected"], missing=d["missing"],
                   extra=d.get("extra") or {}, token_coverage=d.get("token_coverage"),
                   provenance=d.get("provenance"))

    def save(self, path: Path | str) -> None:
        atomic_write_json(path, self.to_json())

    @classmethod
    def load(cls, path: Path | str) -> "EvalResult":
        return cls.from_json(json.loads(Path(path).read_text()))


def _real(x) -> bool:
    return isinstance(x, Real) and not isinstance(x, bool) and math.isfinite(x)


def _checked(tid, tr: TaskResult, k: int) -> TaskResult:
    """A validated copy of `tr` padded to k trials; every padded trial is missing."""
    n = len(tr.rewards)
    if len(tr.weights) != n or len(tr.tokens) != n:
        raise EvaluationError(f"task {tid}: {n} rewards but {len(tr.weights)} weights and "
                              f"{len(tr.tokens)} token entries (must align)")
    if n > k:
        raise EvaluationError(f"task {tid}: {n} trials reported for k={k}")
    for name, xs in (("reward", tr.rewards), ("weight", tr.weights)):
        for x in xs:
            if not _real(x):
                raise EvaluationError(f"task {tid}: {name} {x!r} is not a finite number")
    for x in tr.rewards:
        if not 0.0 <= x <= 1.0:
            raise EvaluationError(f"task {tid}: reward {x!r} outside [0, 1]")
    for x in tr.weights:
        if x < 0:
            raise EvaluationError(f"task {tid}: negative weight {x!r}")
    for x in tr.tokens:
        if x is not None and (not _real(x) or x < 0):
            raise EvaluationError(f"task {tid}: token count {x!r} is not a non-negative number or None")
    if not _real(tr.missing) or int(tr.missing) != tr.missing or tr.missing < 0:
        raise EvaluationError(f"task {tid}: missing={tr.missing!r} is not a non-negative integer")
    pad = k - n
    w = max((float(x) for x in tr.weights), default=1.0)
    missing = int(tr.missing) + pad
    if missing > k:
        raise EvaluationError(f"task {tid}: {missing} missing trials for k={k}")
    return TaskResult(rewards=[float(x) for x in tr.rewards] + [0.0] * pad,
                      weights=[float(x) for x in tr.weights] + [w] * pad,
                      tokens=list(tr.tokens) + [None] * pad, missing=missing,
                      extra=tr.extra)


def aggregate(job: str, k: int, per_task: dict, extra: dict | None = None,
              expected_ids: list[str] | None = None) -> EvalResult:
    """Fold per-task trial records into S_hat and C_hat.

    With `expected_ids` the task set is exactly the requested one: a requested task
    the adapter did not report is k missing trials (reward 0, weight 1), an unrequested
    one is an error. A task with fewer than k trials is padded to k with missing
    trials (reward 0, the task's largest weight), so a shortfall keeps the full
    denominator instead of shrinking it."""
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise EvaluationError(f"k must be a positive integer (got {k!r})")
    if expected_ids is None:
        if not per_task:
            raise EvaluationError("no tasks were scored")
        ids = list(per_task)
    else:
        ids = list(expected_ids)
        if not ids:
            raise EvaluationError("no tasks were requested")
        dup = sorted({t for t in ids if ids.count(t) > 1})
        if dup:
            raise EvaluationError(f"duplicate requested task ids: {dup}")
        extra_ids = sorted(set(per_task) - set(ids))
        if extra_ids:
            raise EvaluationError(f"scored tasks that were not requested: {extra_ids}")
    per = {t: (_checked(t, per_task[t], k) if t in per_task else
               TaskResult([0.0] * k, [1.0] * k, [None] * k, missing=k)) for t in ids}
    num = den = 0.0
    toks = []
    missing = 0
    for tr in per.values():
        for r, w in zip(tr.rewards, tr.weights):
            num += r * w
            den += w
        toks += [x for x in tr.tokens if x is not None and x > 0]     # observed counts
        missing += tr.missing
    if not den > 0:
        raise EvaluationError("total trial weight is zero; S_hat is undefined")
    n_expected = len(per) * k
    return EvalResult(job=job, k=k, per_task=per, S=num / den,
                      C=(sum(toks) / len(toks)) if toks else None,
                      n_expected=n_expected, missing=missing,
                      extra=dict(extra or {}), token_coverage=len(toks) / n_expected)


def evaluate(domain, root: Path, runs_dir: Path, job: str, ids: list[str],
             k: int, log_prefix: str = "", provenance: dict | None = None) -> EvalResult:
    """Run H' (the harness checked out under `root`) on `ids` with k trials and
    score it. Resume-safe: the domain runner fills only missing trials.

    With `provenance` (rrsi.provenance.fingerprint) the job name is bound to that
    identity: a manifest recorded under a different one raises ProvenanceError before
    the runner starts, and a fresh job's manifest is written before it starts (with a
    WARNING when jobs/<job>/ already exists: its trials are adopted under it)."""
    if provenance is not None:
        recorded = read_manifest(runs_dir, job)
        if recorded is None:
            jdir = Path(runs_dir) / "jobs" / job
            if jdir.exists():
                from .loop import log            # loop imports this module
                log(domain.name, f"{job}: WARNING {jdir} already exists without an evaluation "
                    f"manifest (legacy or unknown origin); its existing trials are adopted "
                    f"under this evaluation's identity, which the new manifest records")
            mp = manifest_path(runs_dir, job)
            mp.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(mp, provenance)
        else:
            bad = mismatches(provenance, recorded)
            if bad:
                raise ProvenanceError(
                    f"job {job}: {manifest_path(runs_dir, job)} records a different evaluation "
                    f"({'; '.join(bad)}); nothing was run and the recorded results are kept. "
                    + move_aside(runs_dir, job))
    domain.run(root, runs_dir, job, ids, k, log_prefix=log_prefix)
    per_task, extra = domain.score(runs_dir, job, ids, k)
    ev = aggregate(job, k, per_task, extra, expected_ids=ids)
    ev.provenance = dict(provenance) if provenance is not None else None
    return ev


def cost_known(C: float | None) -> bool:
    """True when C is an observed mean token count (a finite positive number)."""
    return isinstance(C, (int, float)) and not isinstance(C, bool) and math.isfinite(C) and C > 0


def relative_cost_change(C_cand: float | None, C_inc: float | None) -> float | None:
    """Delta C = (C' - C_t) / C_t; None (unknown, never 0) when either side has no
    positive token count."""
    if not (cost_known(C_cand) and cost_known(C_inc)):
        return None
    return (C_cand - C_inc) / C_inc
