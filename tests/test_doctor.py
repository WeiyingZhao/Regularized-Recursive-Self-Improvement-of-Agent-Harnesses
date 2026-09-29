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
"""Offline `doctor`: checks, rendering, secrets never printed, no run state created.

    python3 -m pytest -q tests/test_doctor.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rrsi.config import ConfigError, RRSIConfig  # noqa: E402
from rrsi.doctor import CheckResult, doctor, render  # noqa: E402
from rrsi.domain import Domain, load_domain  # noqa: E402

SENTINEL = "sentinel-project-7f3a9c"


class FakeDomain(Domain):
    """A minimal domain: constitution and harness live under <repo>/domains/fake/."""
    name = "fake"
    harness_path = "harness"

    def __init__(self, evolve=lambda: ["t1", "t2"], extra=()):
        self._evolve, self._extra = evolve, list(extra)

    def evolve_ids(self):
        return self._evolve()

    def doctor_checks(self):
        return list(self._extra)


def _repo(tmp_path: Path, skill=True, patterns=True, harness=True) -> Path:
    d = tmp_path / "repo" / "domains" / "fake"
    d.mkdir(parents=True, exist_ok=True)
    if skill:
        (d / "SKILL.md").write_text("skill")
    if patterns:
        (d / "PATTERNS.md").write_text("patterns")
    if harness:
        (d / "harness").mkdir(exist_ok=True)
    return tmp_path / "repo"


def _run(tmp_path, domain=None, cfg=None, repo=None, environ=None, runs=None):
    return doctor(domain or FakeDomain(), cfg or RRSIConfig(), repo or _repo(tmp_path),
                  runs or tmp_path / "runs", environ={} if environ is None else environ)


def _by_name(results) -> dict:
    return {r.name: r for r in results}


# ---- credentials: presence and count only ----------------------------------------
def test_missing_vertex_projects_fails(monkeypatch, tmp_path):
    monkeypatch.delenv("RRSI_VERTEX_PROJECTS", raising=False)
    c = _by_name(doctor(FakeDomain(), RRSIConfig(), _repo(tmp_path), tmp_path / "runs"))
    assert c["vertex projects"].status == "fail"
    assert "RRSI_VERTEX_PROJECTS" in c["vertex projects"].remedy


@pytest.mark.parametrize("value", ["", " , ,"])
def test_blank_vertex_projects_fails(tmp_path, value):
    c = _by_name(_run(tmp_path, environ={"RRSI_VERTEX_PROJECTS": value}))
    assert c["vertex projects"].status == "fail"


def test_project_values_are_never_printed(tmp_path):
    env = {"RRSI_VERTEX_PROJECTS": f"{SENTINEL}, {SENTINEL}-b"}
    results = _run(tmp_path, environ=env)
    c = _by_name(results)["vertex projects"]
    assert c.status == "ok" and "2 project" in c.detail
    assert SENTINEL not in render(results)
    assert SENTINEL not in json.dumps([asdict(r) for r in results])


# ---- python / anthropic / git ------------------------------------------------------
def test_python_is_ok_on_a_supported_interpreter(tmp_path):
    assert _by_name(_run(tmp_path))["python"].status == "ok"      # 3.10+ is what runs the tests


def test_anthropic_is_checked_with_find_spec_not_imported(tmp_path, monkeypatch):
    seen = []

    def fake(name, *a, **k):
        seen.append(name)
        return object() if present else None

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    imported_before = "anthropic" in sys.modules
    present = True
    assert _by_name(_run(tmp_path))["anthropic"].status == "ok"
    present = False
    c = _by_name(_run(tmp_path))["anthropic"]
    assert c.status == "fail" and "anthropic[vertex]" in c.remedy
    assert set(seen) == {"anthropic"}
    assert ("anthropic" in sys.modules) == imported_before


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_git_checks_worktree(tmp_path):
    repo = _repo(tmp_path)
    assert _by_name(_run(tmp_path, repo=repo))["git"].status == "fail"     # not a work tree yet
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert _by_name(_run(tmp_path, repo=repo))["git"].status == "ok"


def test_git_missing_from_path_fails(tmp_path):
    empty = tmp_path / "bin"
    empty.mkdir()
    c = _by_name(_run(tmp_path, environ={"PATH": str(empty)}))["git"]
    assert c.status == "fail" and "not on PATH" in c.detail


# ---- config ----------------------------------------------------------------------
def test_valid_config_is_ok(tmp_path):
    assert _by_name(_run(tmp_path, cfg=RRSIConfig(T=7, k=3, m=2)))["config"].status == "ok"


def test_invalid_config_surfaces_as_a_failed_check(tmp_path):
    with pytest.raises(ConfigError) as e:
        RRSIConfig(k=0, m=9).validate()
    c = _by_name(_run(tmp_path, cfg=e.value))["config"]
    assert c.status == "fail"
    assert "k: " in c.detail and "m: " in c.detail and "\n" not in c.detail
    assert "rrsi.json" in c.remedy


# ---- runs dir: writable, without creating run state --------------------------------
def test_runs_dir_is_not_created(tmp_path):
    runs = tmp_path / "does" / "not" / "exist"
    c = _by_name(_run(tmp_path, runs=runs))["runs dir"]
    assert c.status == "ok" and "can be created" in c.detail
    assert not (tmp_path / "does").exists()


def test_runs_dir_blocked_by_a_file_fails(tmp_path):
    (tmp_path / "runs").write_text("not a dir")
    assert _by_name(_run(tmp_path))["runs dir"].status == "fail"


@pytest.mark.skipif(os.name != "posix" or getattr(os, "geteuid", lambda: 1)() == 0,
                    reason="needs POSIX permissions and a non-root user")
def test_unwritable_runs_dir_fails(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    runs.chmod(0o500)
    try:
        assert _by_name(_run(tmp_path, runs=runs))["runs dir"].status == "fail"
    finally:
        runs.chmod(0o700)


def test_existing_runs_dir_is_ok(tmp_path):
    (tmp_path / "runs" / "fake").mkdir(parents=True)
    c = _by_name(_run(tmp_path))["runs dir"]
    assert c.status == "ok" and "is writable" in c.detail


# ---- constitution / harness / task set ------------------------------------------------
def test_complete_domain_passes_every_core_check(tmp_path):
    c = _by_name(_run(tmp_path))
    assert (c["constitution"].status, c["harness"].status, c["evolve tasks"].status) == ("ok",) * 3
    assert c["evolve tasks"].detail == "2 tasks"


def test_missing_skill_md_fails_the_constitution_check(tmp_path):
    c = _by_name(_run(tmp_path, repo=_repo(tmp_path, skill=False)))["constitution"]
    assert c.status == "fail" and "SKILL.md" in c.detail and "PATTERNS.md" not in c.detail


def test_missing_patterns_md_and_harness(tmp_path):
    c = _by_name(_run(tmp_path, repo=_repo(tmp_path, patterns=False, harness=False)))
    assert c["constitution"].status == "fail" and "PATTERNS.md" in c["constitution"].detail
    assert c["harness"].status == "fail"


def test_evolve_ids_failure_is_reported_not_raised(tmp_path):
    def boom():
        raise FileNotFoundError("no benchmark checkout at /x")

    def bye():
        raise SystemExit("no eligible tasks under /y")

    c = _by_name(_run(tmp_path, domain=FakeDomain(evolve=boom)))["evolve tasks"]
    assert c.status == "fail" and "no benchmark checkout at /x" in c.detail
    c = _by_name(_run(tmp_path, domain=FakeDomain(evolve=bye)))["evolve tasks"]
    assert c.status == "fail" and "no eligible tasks under /y" in c.detail


# ---- adapter output must not reach stdout (`--json` must stay parseable) -----------------
def _cli_module():
    spec = importlib.util.spec_from_file_location("rrsi_cli", ROOT / "rrsi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ChattyDomain(FakeDomain):
    def evolve_ids(self):
        print('{"meta": "printed while loading evolve tasks"}')
        return ["t1"]

    def doctor_checks(self):
        print("printed by a domain check")
        return [CheckResult("chatty", "ok", "fine")]


def test_adapter_output_is_kept_off_stdout(tmp_path, capsys):
    res = _run(tmp_path, domain=ChattyDomain())
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "printed while loading evolve tasks" in cap.err and "printed by a domain check" in cap.err
    c = _by_name(res)
    assert c["evolve tasks"].status == "ok" and c["chatty"].status == "ok"


def test_doctor_json_stays_parseable_when_the_adapter_prints(tmp_path, capsys):
    cli = _cli_module()
    rc = cli.cmd_doctor(ChattyDomain(), RRSIConfig(), tmp_path / "runs", as_json=True)
    cap = capsys.readouterr()
    payload = json.loads(cap.out)                      # nothing but the payload on stdout
    assert payload["domain"] == "fake" and rc == (0 if payload["ok"] else 1)
    assert "printed while loading" in cap.err
    assert not (tmp_path / "runs").exists()


# ---- domain hook ------------------------------------------------------------------------
def test_base_domain_hook_is_empty():
    assert Domain().doctor_checks() == []


def test_domain_checks_are_appended_last(tmp_path):
    extra = [CheckResult("thing", "warn", "meh", "do x")]
    res = _run(tmp_path, domain=FakeDomain(extra=extra))
    assert res[-1] == extra[0]


def test_a_raising_domain_hook_is_a_failed_check(tmp_path):
    class Bad(FakeDomain):
        def doctor_checks(self):
            raise RuntimeError("kaboom")

    c = _by_name(_run(tmp_path, domain=Bad()))["domain checks"]
    assert c.status == "fail" and "kaboom" in c.detail


# ---- rendering ----------------------------------------------------------------------------
def test_render_aligns_and_shows_remedies_only_for_problems():
    out = render([CheckResult("a", "ok", "fine", "ignored"),
                  CheckResult("longer name", "warn", "hmm", "fix warn"),
                  CheckResult("b", "fail", "bad", "fix fail")]).splitlines()
    assert len(out) == 5                                   # 3 checks + 2 remedies
    assert out[0].startswith("OK    a            fine")
    assert out[1].startswith("WARN  longer name  hmm")
    assert out[2].strip() == "-> fix warn" and out[4].strip() == "-> fix fail"
    assert "ignored" not in "\n".join(out)
    assert render([]) == ""


# ---- domain-specific checks (filesystem / PATH only) -----------------------------------------
@pytest.fixture(scope="module")
def coding():
    load_domain("coding")
    return sys.modules["domains.coding.adapter"]


@pytest.fixture(scope="module")
def eng():
    load_domain("eng")
    return sys.modules["domains.eng.adapter"]


def test_coding_checks_python_and_docker(coding, monkeypatch, tmp_path):
    monkeypatch.setenv("RRSI_CODING_PYTHON", sys.executable)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/bin/docker")
    c = _by_name(coding.DOMAIN.doctor_checks())
    assert c["coding python"].status == "ok" and c["docker"].status == "ok"
    monkeypatch.setenv("RRSI_CODING_PYTHON", str(tmp_path / "nope" / "python"))
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    c = _by_name(coding.DOMAIN.doctor_checks())
    assert c["coding python"].status == "fail" and "RRSI_CODING_VENV" in c["coding python"].remedy
    assert c["docker"].status == "fail"


def test_eng_checks_paths_and_bwrap(eng, monkeypatch, tmp_path):
    bench, grader = tmp_path / "bench", tmp_path / "py"
    bench.mkdir()
    grader.write_text("")
    monkeypatch.setattr(eng, "AGENT_PY", sys.executable)
    monkeypatch.setattr(eng, "BENCH_ROOT", bench)
    monkeypatch.setattr(eng, "GRADING_PY", grader)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    c = _by_name(eng.DOMAIN.doctor_checks())
    assert [c[n].status for n in ("eng agent python", "eng benchmark root", "eng grading python")] \
        == ["ok"] * 3
    monkeypatch.setattr(eng, "AGENT_PY", str(tmp_path / "gone"))
    assert _by_name(eng.DOMAIN.doctor_checks())["eng agent python"].status == "fail"
    assert c["bwrap"].status == "fail"
    monkeypatch.setattr(eng, "BENCH_ROOT", tmp_path / "missing")
    monkeypatch.setattr(eng, "GRADING_PY", tmp_path / "missing-py")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/bwrap")
    c = _by_name(eng.DOMAIN.doctor_checks())
    assert c["bwrap"].status == "ok"
    assert c["eng benchmark root"].status == "fail" and c["eng grading python"].status == "fail"
    monkeypatch.setattr(sys, "platform", "darwin")
    c = _by_name(eng.DOMAIN.doctor_checks())["bwrap"]
    assert c.status == "warn" and "Linux" in c.detail


# ---- the CLI ----------------------------------------------------------------------------------
def _cli(*args, runs: Path, env=None):
    return subprocess.run([sys.executable, str(ROOT / "rrsi.py"), *args[:2], "--runs", str(runs),
                           *args[2:]], cwd=str(ROOT), capture_output=True, text=True, timeout=120,
                          env={**os.environ, **(env or {})})


def test_doctor_json_is_valid_and_creates_no_run_state(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    r = _cli("--domain", "coding", "doctor", "--json", runs=runs,
             env={"RRSI_VERTEX_PROJECTS": SENTINEL})
    payload = json.loads(r.stdout)                    # valid JSON whatever the machine has
    assert payload["domain"] == "coding"
    assert r.returncode == (0 if payload["ok"] else 1)
    assert payload["ok"] == all(c["status"] != "fail" for c in payload["checks"])
    names = {c["name"] for c in payload["checks"]}
    assert {"python", "anthropic", "vertex projects", "git", "config", "runs dir", "constitution",
            "harness", "evolve tasks", "coding python", "docker"} <= names
    assert not (runs / "coding").exists() and list(runs.iterdir()) == []
    assert SENTINEL not in r.stdout + r.stderr


def test_doctor_text_output_and_exit_status(tmp_path):
    r = _cli("--domain", "coding", "doctor", runs=tmp_path / "runs",
             env={"RRSI_VERTEX_PROJECTS": ""})
    assert r.returncode == 1                          # no credentials: a failed check
    assert "FAIL  vertex projects" in r.stdout and "checks:" in r.stdout
    assert not (tmp_path / "runs").exists()


def test_doctor_reports_a_config_error_instead_of_exiting(tmp_path):
    r = _cli("--domain", "coding", "--k", "0", "doctor", "--json", runs=tmp_path / "runs")
    payload = json.loads(r.stdout)
    cfg = {c["name"]: c for c in payload["checks"]}["config"]
    assert cfg["status"] == "fail" and "k: " in cfg["detail"]
    assert payload["ok"] is False and r.returncode == 1
    assert not (tmp_path / "runs").exists()


def test_doctor_workspace_survives_a_missing_checkout(tmp_path):
    r = _cli("--domain", "workspace", "doctor", "--json", runs=tmp_path / "runs",
             env={"HARVEY_LAB_ROOT": str(tmp_path / "no-harvey-labs")})
    checks = {c["name"]: c for c in json.loads(r.stdout)["checks"]}
    assert checks["harvey lab"]["status"] == "fail"
    assert checks["evolve tasks"]["status"] == "fail"       # reported, not a traceback
    assert not (tmp_path / "runs").exists()
    (tmp_path / "labs").mkdir()
    r = _cli("--domain", "workspace", "doctor", "--json", runs=tmp_path / "runs",
             env={"HARVEY_LAB_ROOT": str(tmp_path / "labs")})
    assert {c["name"]: c for c in json.loads(r.stdout)["checks"]}["harvey lab"]["status"] == "ok"
