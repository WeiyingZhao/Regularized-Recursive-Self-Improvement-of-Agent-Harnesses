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
"""The Domain interface: everything RRSI needs from a benchmark environment.

RRSI itself never runs an agent, grades a deliverable or reads a trajectory
format. A domain adapter (domains/<name>/adapter.py) supplies those, and the
texts that make the three search roles speak the benchmark's language.

    run / score        Evaluate(H', D, k)              rrsi.evaluate
    load_trial         trajectories for Analyze        rrsi.loop.build_traces
    render_trace       what the analyst/digester/proposer read
    task_row           one-line summary per trace in the task tables
    smoke              liveness check before evaluation (not a selection rule)
    doctor_checks      offline prerequisite checks for `doctor` (rrsi.doctor)
    critic_patterns    deterministic leakage denylist   rrsi.critic
    component_signals  diff regexes -> component tag    rrsi.components
    guards             non-compensatory domain checks   rrsi.selection (Sec. 3.3)
    briefs             domain paragraphs for analyst / digester / proposer / critic

Each domain is the package `domains.<name>` (adapter, briefs, render, ...), imported by
its qualified name so two domains loaded in one process never share a sibling module.
`validate_domain` checks an adapter against this contract without loading a task set.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOMAINS = ROOT / "domains"


class Domain:
    name: str = "base"
    harness_path: str = "harness"        # relative to domains/<name>/
    root: Path                            # domains/<name> in the MAIN checkout

    # ---- task sets ---------------------------------------------------------
    def evolve_ids(self) -> list[str]: raise NotImplementedError
    def heldout_ids(self) -> list[str]: return []
    def smoke_ids(self, incumbent_per_task: dict | None = None) -> list[str]:
        raise NotImplementedError

    # ---- Evaluate ----------------------------------------------------------
    def run(self, root: Path, runs_dir: Path, job: str, ids: list[str], k: int,
            log_prefix: str = "") -> None:
        """Run the harness checked out under `root` (a worktree) on `ids`
        with k trials, writing to runs_dir/jobs/<job>. Must be resume-safe."""
        raise NotImplementedError

    def score(self, runs_dir: Path, job: str, ids: list[str], k: int
              ) -> tuple[dict, dict]:
        """-> ({task_id: TaskResult}, extra aggregates)."""
        raise NotImplementedError

    def guards(self, incumbent, candidate) -> list[str]:
        """Violated non-compensatory domain criteria (empty = none)."""
        return []

    def regression_threshold(self, k: int) -> float:
        """Per-task mean drop that counts as a regression in attribution."""
        return 1.0 / max(1, k)

    # ---- evidence ----------------------------------------------------------
    def load_trial(self, runs_dir: Path, job: str, task_id: str, trial: int):
        raise NotImplementedError

    def render_trace(self, rec, detail: bool = False) -> str:
        raise NotImplementedError

    def task_row(self, task_id: str, rec, tr) -> str:
        raise NotImplementedError

    # ---- gates and texts -----------------------------------------------------
    def smoke(self, root: Path, runs_dir: Path, job: str, ids: list[str]
              ) -> tuple[bool, dict]:
        raise NotImplementedError

    critic_patterns: list = []
    component_signals: list = []
    briefs: dict = {}                    # analyst / digester / proposer / critic
    source_exts: set = {".py", ".txt", ".md", ".json"}

    def doctor_checks(self) -> list:
        """Cheap filesystem / PATH checks (rrsi.doctor.CheckResult) for this domain's
        external prerequisites. No model call, no benchmark run."""
        return []

    def harness_dir(self, root: Path) -> Path:
        return (root / "domains" / self.name / self.harness_path).resolve()

    def constitution(self, root: Path) -> tuple[str, str]:
        d = root / "domains" / self.name
        return (d / "SKILL.md").read_text(), (d / "PATTERNS.md").read_text()


# Overridden by every adapter; the base class raises NotImplementedError for each.
REQUIRED_METHODS = ("evolve_ids", "smoke_ids", "run", "score", "load_trial",
                    "render_trace", "task_row", "smoke")
REQUIRED_BRIEFS = ("analyst", "digester", "proposer", "critic")
CONSTITUTION_FILES = ("SKILL.md", "PATTERNS.md")


def load_domain(name: str) -> Domain:
    p = DOMAINS / name / "adapter.py"
    if not p.is_file():
        raise SystemExit(f"unknown domain {name!r} (no {p})")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    mod = importlib.import_module(f"domains.{name}.adapter")
    dom = mod.DOMAIN
    dom.root = DOMAINS / name
    return dom


def validate_domain(domain: Domain) -> list[str]:
    """Contract problems of an adapter, empty if it is complete: the methods the base class
    leaves abstract are overridden, all four briefs are non-empty text, and the constitution
    files exist under `domain.root`. Cheap and offline: it never calls evolve_ids() or reads a
    benchmark checkout."""
    out = []
    cls = type(domain)
    for m in REQUIRED_METHODS:
        if getattr(cls, m, None) is getattr(Domain, m):
            out.append(f"{m}() is not overridden")
    briefs = getattr(domain, "briefs", None)
    if not isinstance(briefs, dict):
        out.append("briefs is not a dict")
        briefs = {}
    for k in REQUIRED_BRIEFS:
        v = briefs.get(k)
        if not (isinstance(v, str) and v.strip()):
            out.append(f"briefs[{k!r}] is missing or empty")
    root = getattr(domain, "root", None)
    if root is None:
        out.append("root is not set")
    else:
        out += [f"{f} is missing under {root}" for f in CONSTITUTION_FILES
                if not (Path(root) / f).is_file()]
    return out
