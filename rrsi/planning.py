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
"""Offline workload preview: how many trials and search-role invocations a run implies.

Upper-bound counts from the hyperparameters and task-set sizes (T rounds, k
trials per task, m candidates per round); no model call, no run state. Judge
calls, retries and infrastructure reruns are not counted, and search-role
invocations are not model calls (one invocation may make several).
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field

from rrsi.config import RRSIConfig


@dataclass
class WorkloadEstimate:
    T: int
    k: int
    m: int
    repair_rounds: int
    n_tasks: int | None                  # evolve tasks (None: could not be resolved)
    n_smoke: int | None                  # smoke tasks
    baseline_trials: int | None          # Evaluate(H_0): n * k
    candidate_trials_max: int | None     # T * m * n * k: full screening passes, before retries
    smoke_trials_max: int | None         # T * m * n_smoke: one trial per smoke task
    analyst_invocations_per_round: int
    proposer_invocations_per_round: int  # m * (1 + repair_rounds)
    critic_invocations_per_round: int    # m * (1 + repair_rounds)
    search_invocations_per_round: int
    search_invocations_total: int
    unknown: dict = field(default_factory=dict)      # field -> why it is unknown


def failure_reason(exc: BaseException) -> str:
    """One line for a task set that could not be loaded (adapters may SystemExit)."""
    msg = str(exc.code) if isinstance(exc, SystemExit) else f"{type(exc).__name__}: {exc}"
    return " ".join(msg.split())


def task_counts(domain) -> tuple[int | None, int | None, dict]:
    """(evolve tasks, smoke tasks, {field: reason}); a task set that cannot be loaded
    is reported as unknown instead of raising. Adapter output goes to stderr, so
    stdout stays clean for `--json`."""
    out, unknown = {}, {}
    for key, ids in (("n_tasks", domain.evolve_ids), ("n_smoke", domain.smoke_ids)):
        try:
            with contextlib.redirect_stdout(sys.stderr):    # adapters may print while loading
                out[key] = len(ids())
        except (Exception, SystemExit) as e:  # noqa: BLE001
            out[key], unknown[key] = None, failure_reason(e)
    return out["n_tasks"], out["n_smoke"], unknown


def estimate_workload(cfg: RRSIConfig, n_tasks: int | None, n_smoke: int | None,
                      unknown: dict | None = None) -> WorkloadEstimate:
    """Upper-bound counts for `cfg`; an unknown task count gives None, never zero.
    `unknown` maps n_tasks / n_smoke to the reason it could not be resolved."""
    per_role = cfg.m * (1 + cfg.repair_rounds)
    per_round = 1 + 2 * per_role
    return WorkloadEstimate(
        T=cfg.T, k=cfg.k, m=cfg.m, repair_rounds=cfg.repair_rounds,
        n_tasks=n_tasks, n_smoke=n_smoke,
        baseline_trials=None if n_tasks is None else n_tasks * cfg.k,
        candidate_trials_max=None if n_tasks is None else cfg.T * cfg.m * n_tasks * cfg.k,
        smoke_trials_max=None if n_smoke is None else cfg.T * cfg.m * n_smoke,
        analyst_invocations_per_round=1, proposer_invocations_per_round=per_role,
        critic_invocations_per_round=per_role, search_invocations_per_round=per_round,
        search_invocations_total=cfg.T * per_round, unknown=dict(unknown or {}))


def render_workload(est: WorkloadEstimate) -> str:
    def num(v: int | None) -> str:
        return "unknown" if v is None else f"{v:,}"

    rows = [
        ("evolve tasks", num(est.n_tasks), ""),
        ("smoke tasks", num(est.n_smoke), ""),
        ("baseline trials (H_0)", num(est.baseline_trials), "n * k"),
        ("candidate trials, max", num(est.candidate_trials_max),
         "T * m * n * k, full screening passes"),
        ("smoke trials, max", num(est.smoke_trials_max), "T * m * n_smoke, one trial per task"),
        ("search-role invocations per round, max", f"{est.search_invocations_per_round:,}",
         f"{est.analyst_invocations_per_round} analyst + {est.proposer_invocations_per_round} "
         f"proposer + {est.critic_invocations_per_round} critic"),
        ("search-role invocations over T rounds, max", f"{est.search_invocations_total:,}",
         "T * invocations per round"),
    ]
    w0, w1 = max(len(r[0]) for r in rows), max(len(r[1]) for r in rows)
    lines = [f"Workload preview: T={est.T} rounds, k={est.k} trials/task, m={est.m} "
             f"candidates/round, repair_rounds={est.repair_rounds}"]
    lines += [f"  {label.ljust(w0)}  {value.rjust(w1)}" + (f"   [{how}]" if how else "")
              for label, value, how in rows]
    for key, label in (("n_tasks", "evolve tasks"), ("n_smoke", "smoke tasks")):
        if key in est.unknown:
            lines.append(f"  unknown {label}: {est.unknown[key]}")
    lines.append("These are upper-bound counts, not spend or latency. They exclude judge calls, "
                 "retries and infrastructure reruns.")
    lines.append("Search-role figures count invocations, not model calls or tokens: the analyst and "
                 "its digester subagents run multiple model turns per invocation, and each proposer "
                 "or critic invocation may make more than one model call.")
    return "\n".join(lines)
