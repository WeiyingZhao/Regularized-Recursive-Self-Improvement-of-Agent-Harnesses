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
"""A deterministic offline domain for driving rrsi.loop.Run end to end in tests.

The harness is one file, domains/fake/harness/policy.txt ("score=0.5000"), in a
temporary Git repository. Every trial of every task scores that value with a fixed
token count; run() calls are counted. The three search roles are replaced by fakes:
the analyst returns an empty report, the critic accepts, and the proposer writes
`score_fn(t, variant, current)` (plus `extra_line`, if set) into the variant's
worktree. No model call, no benchmark, no credentials.

    from fake_domain import make_run
    fx = make_run(tmp_path, monkeypatch)       # repo + Run + patched roles
    fx.run.baseline(); fx.run.round(0)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import rrsi.loop  # noqa: E402
from rrsi.config import VARIANT_LABELS, RRSIConfig  # noqa: E402
from rrsi.domain import Domain  # noqa: E402
from rrsi.evaluate import TaskResult  # noqa: E402
from rrsi.loop import Run  # noqa: E402

IDS = ["t1", "t2", "t3"]
TOKENS = 100
POLICY = Path("domains/fake/harness/policy.txt")


def read_score(path: Path) -> float:
    return float(re.search(r"score=([0-9.]+)", Path(path).read_text()).group(1))


def write_score(path: Path, score: float) -> None:
    Path(path).write_text(f"score={score:.4f}\n")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True,
                          text=True).stdout.strip()


class FakeDomain(Domain):
    """Scores every trial with the harness's policy.txt value. Scoring reports every
    trial missing (an infrastructure failure) for jobs in `missing_jobs`, or for
    all jobs when `all_missing` is set."""
    name = "fake"
    harness_path = "harness"

    def __init__(self, repo: Path, ids=IDS, tokens: int = TOKENS):
        self.root = Path(repo) / "domains" / "fake"
        self.ids, self.tokens = list(ids), tokens
        self.calls: list[str] = []          # job of every run() call
        self.all_missing = False
        self.missing_jobs: set[str] = set()

    @property
    def n_runs(self) -> int:
        return len(self.calls)

    def evolve_ids(self):
        return list(self.ids)

    def smoke_ids(self, incumbent_per_task=None):
        return self.ids[:1]

    def run(self, root, runs_dir, job, ids, k, log_prefix=""):
        self.calls.append(job)
        out = Path(runs_dir) / "jobs" / job
        out.mkdir(parents=True, exist_ok=True)
        (out / "result.json").write_text(json.dumps({"score": read_score(Path(root) / POLICY)}))

    def score(self, runs_dir, job, ids, k):
        s = json.loads((Path(runs_dir) / "jobs" / job / "result.json").read_text())["score"]
        if self.all_missing or job in self.missing_jobs:
            return {t: TaskResult([0.0] * k, missing=k) for t in ids}, {}
        return {t: TaskResult([s] * k, tokens=[self.tokens] * k) for t in ids}, {}

    def load_trial(self, runs_dir, job, task_id, trial):
        return {"job": job, "task": task_id, "trial": trial}

    def render_trace(self, rec, detail=False):
        return f"{rec['task']} trial {rec['trial']} ({rec['job']})"

    def task_row(self, task_id, rec, tr):
        return f"{task_id}: mean {tr.mean:.3f}"

    def smoke(self, root, runs_dir, job, ids):
        return True, {}


def make_repo(path: Path, score: float = 0.5) -> Path:
    """Git repo with domains/fake/{SKILL.md,PATTERNS.md,harness/policy.txt}, one commit."""
    d = path / "domains" / "fake"
    (d / "harness").mkdir(parents=True)
    (d / "SKILL.md").write_text("fake skill\n")
    (d / "PATTERNS.md").write_text("fake patterns\n")
    write_score(path / POLICY, score)
    git(path, "init", "-q")
    git(path, "config", "user.name", "rrsi-test")
    git(path, "config", "user.email", "rrsi-test@localhost")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "fake domain")
    return path


def default_score(t: int, variant: str, current: float) -> float:
    """Variant v of any round adds 0.05 * (index of v + 1): B beats A, deterministically."""
    return min(1.0, current + 0.05 * (VARIANT_LABELS.index(variant) + 1))


def patch_roles(monkeypatch, fx: SimpleNamespace) -> None:
    """Replace analyst / proposer / critic / digests on rrsi.loop; count calls in fx.calls."""
    def analyze(domain, traces, per_task, round_dir, **kw):
        fx.calls["analyze"] += 1
        return {"failure_modes": [], "success_habits": []}

    def propose(domain, hdir, *a, variant_brief="", **kw):
        fx.calls["propose"] += 1
        vid, t = re.search(r"variant (\w) of round (\d+)", variant_brief).groups()
        policy = Path(hdir) / "policy.txt"
        write_score(policy, fx.score_fn(int(t), vid, read_score(policy)))
        if fx.extra_line:
            policy.write_text(policy.read_text() + fx.extra_line + "\n")
        return {"status": "done", "n_edits": 1, "mechanism": "m",
                "edits": [{"id": "C1", "component": "prompt", "hypothesis": "h"}]}

    def review(*a, **kw):
        fx.calls["review"] += 1
        return {"verdict": "accept"}

    monkeypatch.setattr(rrsi.loop, "analyze", analyze)
    monkeypatch.setattr(rrsi.loop, "propose", propose)
    monkeypatch.setattr(rrsi.loop, "review", review)
    monkeypatch.setattr(rrsi.loop, "load_digests", lambda round_dir: [])


def make_run(tmp_path: Path, monkeypatch, score: float = 0.5, score_fn=default_score,
             **cfg_overrides) -> SimpleNamespace:
    """-> fx with .repo, .domain, .run, .runs_root, .cfg, .calls, .score_fn,
    .extra_line and .fresh() (a new Run on the same repo, domain and runs dir, as a
    new process would build). Defaults: T=3, k=2, m=2, delta=0.01, small trace counts."""
    repo = make_repo(tmp_path / "repo", score)
    cfg = RRSIConfig(**{"T": 3, "k": 2, "m": 2, "delta": 0.01, "n_fail_traces": 2,
                        "n_success_traces": 1, **cfg_overrides})
    cfg.validate()
    fx = SimpleNamespace(repo=repo, domain=FakeDomain(repo), cfg=cfg,
                         runs_root=tmp_path / "runs", score_fn=score_fn, extra_line="",
                         calls={"analyze": 0, "propose": 0, "review": 0})
    fx.fresh = lambda: Run(fx.domain, fx.cfg, fx.repo, fx.runs_root)
    fx.run = fx.fresh()
    patch_roles(monkeypatch, fx)
    return fx
