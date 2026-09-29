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
"""Domain modules are package-qualified and isolated; `validate_domain` checks the contract.

    python3 -m pytest -q tests/test_domain_contract.py
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rrsi.config import RRSIConfig  # noqa: E402
from rrsi.doctor import doctor  # noqa: E402
from rrsi.domain import Domain, load_domain, validate_domain  # noqa: E402

DOMAINS = ("coding", "eng", "workspace")

# Runs in a fresh interpreter: argv = repo root, then the domains to load, in order.
LOAD_ORDER = r"""
import importlib, json, sys
from pathlib import Path

root, order = Path(sys.argv[1]), sys.argv[2:]
sys.path.insert(0, str(root))
from rrsi.domain import load_domain

loaded = {n: load_domain(n) for n in order}
renders, proposers = {}, {}
for n in order:                     # the brief each domain actually serves is its own
    briefs = importlib.import_module(f"domains.{n}.briefs")
    want = briefs.PROPOSER
    if n == "coding":
        cfg = json.loads((root / "domains" / "coding" / "rrsi.json").read_text())
        want = want.replace("{policy}", cfg.get("policy_label", "the frozen policy"))
    assert loaded[n].briefs["proposer"] == want, f"{n}: proposer brief is not its own"
    proposers[n] = want
for n in order:                     # and the modules behind it are the package-qualified ones
    briefs = importlib.import_module(f"domains.{n}.briefs")
    render = importlib.import_module(f"domains.{n}.render")
    adapter = sys.modules[f"domains.{n}.adapter"]
    assert adapter.briefs is briefs, f"{n}: adapter.briefs is {adapter.briefs.__name__}"
    assert adapter.render is render, f"{n}: adapter.render is {adapter.render.__name__}"
    assert render.__name__ == f"domains.{n}.render", render.__name__
    assert briefs.__name__ == f"domains.{n}.briefs", briefs.__name__
    assert Path(render.__file__).parent.name == n, render.__file__
    renders[n] = render
assert len({id(m) for m in renders.values()}) == len(order)
assert len(set(proposers.values())) == len(order), "the domains' proposer briefs must differ"
print("ok")
"""


# ---- isolation: one process, any load order ---------------------------------------------------
@pytest.mark.parametrize("order", list(itertools.permutations(DOMAINS)),
                         ids=lambda o: "-".join(o))
def test_every_load_order_keeps_each_domains_own_modules(order, tmp_path):
    r = subprocess.run([sys.executable, "-c", LOAD_ORDER, str(ROOT), *order], cwd=tmp_path,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-2000:]


@pytest.mark.parametrize("name", DOMAINS)
def test_loading_a_domain_twice_returns_a_working_domain(name):
    first, second = load_domain(name), load_domain(name)
    assert isinstance(second, Domain) and second.name == name
    assert second.root == ROOT / "domains" / name
    assert set(second.briefs) >= {"analyst", "digester", "proposer", "critic"}
    skill, patterns = second.constitution(ROOT)
    assert skill.strip() and patterns.strip()
    assert sys.modules[f"domains.{name}.adapter"].DOMAIN is second


def test_unknown_domain_is_still_a_clean_error():
    with pytest.raises(SystemExit, match=r"unknown domain 'nope' \(no .*nope.adapter\.py\)"):
        load_domain("nope")


# ---- eng's in-process diagnostics import is package-qualified too -----------------------------
ENG_TRIAL = r"""
import json, sys
from pathlib import Path

root, job = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
from rrsi.domain import load_domain

load_domain("eng")
render = sys.modules["domains.eng.render"]
rec = render.load_trial(job, {"id": "t1"}, 0)
assert rec["diag"], "diagnostics were swallowed"
assert "domains.eng.bench.diagnostics" in sys.modules
assert str(root / "domains" / "eng" / "bench") not in sys.path
print("ok")
"""


def test_eng_trial_diagnostics_load_without_a_bare_import(tmp_path):
    td = tmp_path / "jobs" / "j" / "t1" / "t0"
    td.mkdir(parents=True)
    (td / "meta.json").write_text("{}")
    (td / "verdict.json").write_text(json.dumps({"passed": False}))
    (td / "traj.json").write_text(json.dumps({"messages": []}))
    r = subprocess.run([sys.executable, "-c", ENG_TRIAL, str(ROOT), str(tmp_path / "jobs" / "j")],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-2000:]


# ---- validate_domain --------------------------------------------------------------------------
@pytest.mark.parametrize("name", DOMAINS)
def test_shipped_domains_satisfy_the_contract(name):
    assert validate_domain(load_domain(name)) == []


class _Complete(Domain):
    name = "complete"

    def __init__(self, root):
        self.root = root

    def evolve_ids(self): return ["t"]
    def smoke_ids(self, incumbent_per_task=None): return ["t"]
    def run(self, root, runs_dir, job, ids, k, log_prefix=""): pass
    def score(self, runs_dir, job, ids, k): return {}, {}
    def load_trial(self, runs_dir, job, task_id, trial): return None
    def render_trace(self, rec, detail=False): return ""
    def task_row(self, task_id, rec, tr): return ""
    def smoke(self, root, runs_dir, job, ids): return True, {}

    briefs = {"analyst": "a", "digester": "d", "proposer": "p", "critic": "c"}


def _constitution(tmp_path):
    (tmp_path / "SKILL.md").write_text("skill")
    (tmp_path / "PATTERNS.md").write_text("patterns")
    return tmp_path


def test_a_complete_subclass_is_valid(tmp_path):
    assert validate_domain(_Complete(_constitution(tmp_path))) == []


def test_an_incomplete_subclass_reports_every_gap(tmp_path):
    class Incomplete(Domain):
        name = "incomplete"
        briefs = {"analyst": "a", "digester": "  ", "proposer": "p"}

        def evolve_ids(self): return ["t"]
        def run(self, root, runs_dir, job, ids, k, log_prefix=""): pass

    d = Incomplete()
    d.root = tmp_path                                   # no SKILL.md / PATTERNS.md
    problems = validate_domain(d)
    text = "\n".join(problems)
    for m in ("smoke_ids", "score", "load_trial", "render_trace", "task_row", "smoke"):
        assert f"{m}() is not overridden" in text
    assert "evolve_ids() is not overridden" not in text and "run() is not overridden" not in text
    assert "briefs['digester']" in text and "briefs['critic']" in text
    assert "briefs['analyst']" not in text and "briefs['proposer']" not in text
    assert "SKILL.md is missing" in text and "PATTERNS.md is missing" in text
    assert validate_domain(Domain())[-1] == "root is not set"


def test_validation_never_loads_the_task_set(tmp_path):
    class Boom(_Complete):
        def evolve_ids(self): raise AssertionError("evolve_ids must not be called")
        def heldout_ids(self): raise AssertionError("heldout_ids must not be called")

    assert validate_domain(Boom(_constitution(tmp_path))) == []


# ---- doctor -----------------------------------------------------------------------------------
def _check(domain, tmp_path):
    results = doctor(domain, RRSIConfig(), ROOT, tmp_path / "runs", environ={})
    return {r.name: r for r in results}["domain contract"]


def test_doctor_reports_the_domain_contract(tmp_path):
    c = _check(load_domain("coding"), tmp_path)
    assert c.status == "ok"
    d = Domain()
    d.name, d.root = "coding", ROOT / "domains" / "coding"
    c = _check(d, tmp_path)
    assert c.status == "fail" and "run() is not overridden" in c.detail
    assert "briefs['proposer']" in c.detail and "domains/coding/adapter.py" in c.remedy
