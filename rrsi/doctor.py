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
"""Offline prerequisite checks (`rrsi.py --domain <name> doctor`).

Filesystem, PATH and environment only: no model call, no import of the `anthropic`
package, no run state created. Credentials are reported by presence (a project
count), never by value. A domain adds its own checks through Domain.doctor_checks().
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from rrsi.config import ConfigError, RRSIConfig
from rrsi.planning import failure_reason

MIN_PYTHON = (3, 10)


@dataclass
class CheckResult:
    name: str
    status: str            # "ok" | "warn" | "fail"
    detail: str
    remedy: str = ""


def _ok(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "ok", detail)


def _fail(name: str, detail: str, remedy: str = "") -> CheckResult:
    return CheckResult(name, "fail", detail, remedy)


def _python() -> CheckResult:
    v = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info >= MIN_PYTHON:
        return _ok("python", v)
    return _fail("python", f"{v} is older than {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
                 f"use Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer")


def _anthropic() -> CheckResult:
    try:
        found = importlib.util.find_spec("anthropic") is not None
    except (ImportError, ValueError):
        found = False
    if found:
        return _ok("anthropic", "package installed (not imported)")
    return _fail("anthropic", "package not installed (the search roles need it)",
                 'pip install "anthropic[vertex]>=0.40"   # or: pip install -e .')


def _vertex_projects(environ) -> CheckResult:
    n = len([p for p in environ.get("RRSI_VERTEX_PROJECTS", "").split(",") if p.strip()])
    if n:
        return _ok("vertex projects", f"RRSI_VERTEX_PROJECTS lists {n} project(s)")
    return _fail("vertex projects", "RRSI_VERTEX_PROJECTS is not set or empty",
                 'export RRSI_VERTEX_PROJECTS="<gcp-project>[,<gcp-project>...]"  '
                 "# projects with Claude on Vertex AI enabled")


def _git(repo: Path, environ) -> CheckResult:
    exe = shutil.which("git", path=environ.get("PATH"))
    if not exe:
        return _fail("git", "git is not on PATH", "install git (candidates live in git worktrees)")
    try:
        r = subprocess.run([exe, "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return _fail("git", failure_reason(e))
    if r.returncode == 0 and r.stdout.strip() == "true":
        return _ok("git", f"{repo} is a Git work tree")
    why = " ".join(r.stderr.split()) or "not a work tree"
    return _fail("git", f"{repo} is not a Git work tree ({why})",
                 "run rrsi from a clone of the repository")


def _config(cfg_or_error, domain) -> CheckResult:
    if isinstance(cfg_or_error, RRSIConfig):
        c = cfg_or_error
        return _ok("config", f"T={c.T} k={c.k} m={c.m} repair_rounds={c.repair_rounds}")
    if isinstance(cfg_or_error, ConfigError):
        return _fail("config", "; ".join(str(cfg_or_error).splitlines()),
                     f"fix domains/{domain.name}/rrsi.json or the command-line overrides")
    return _fail("config", failure_reason(cfg_or_error))


def _runs_dir(runs_dir: Path, domain) -> CheckResult:
    """Writable, or creatable under its nearest existing ancestor; creates nothing."""
    target = Path(runs_dir) / domain.name
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not probe.is_dir():
        return _fail("runs dir", f"{probe} exists but is not a directory",
                     "point --runs at a directory")
    if not os.access(probe, os.W_OK | os.X_OK):
        return _fail("runs dir", f"{probe} is not writable", "point --runs at a writable directory")
    return _ok("runs dir", f"{target} " + ("is writable" if probe == target
                                            else f"can be created under {probe}"))


def _constitution(domain, repo: Path) -> CheckResult:
    d = repo / "domains" / domain.name
    missing = [n for n in ("SKILL.md", "PATTERNS.md") if not (d / n).is_file()]
    if missing:
        return _fail("constitution", f"missing {', '.join(missing)} in {d}",
                     "restore the proposer constitution files from the repository")
    return _ok("constitution", "SKILL.md and PATTERNS.md present")


def _harness(domain, repo: Path) -> CheckResult:
    h = domain.harness_dir(repo)
    if h.is_dir():
        return _ok("harness", str(h))
    return _fail("harness", f"{h} is missing", "restore the harness directory from the repository")


def _evolve_tasks(domain) -> CheckResult:
    try:
        with contextlib.redirect_stdout(sys.stderr):        # adapters may print while loading
            n = len(domain.evolve_ids())
    except (Exception, SystemExit) as e:  # noqa: BLE001
        return _fail("evolve tasks", failure_reason(e),
                     f"see domains/{domain.name}/README.md for the benchmark checkout")
    return _ok("evolve tasks", f"{n} tasks")


def doctor(domain, cfg_or_error, repo: Path, runs_dir: Path, environ=os.environ) -> list[CheckResult]:
    """Core checks, then the domain's own. `cfg_or_error` is the loaded RRSIConfig or the
    ConfigError the CLI caught; nothing here calls a model. Whatever a domain prints while
    loading its task set goes to stderr, keeping stdout clean for `--json`."""
    repo, runs_dir = Path(repo), Path(runs_dir)
    out = [_python(), _anthropic(), _vertex_projects(environ), _git(repo, environ),
           _config(cfg_or_error, domain), _runs_dir(runs_dir, domain),
           _constitution(domain, repo), _harness(domain, repo), _evolve_tasks(domain)]
    try:
        with contextlib.redirect_stdout(sys.stderr):
            out += list(domain.doctor_checks())
    except (Exception, SystemExit) as e:  # noqa: BLE001
        out.append(_fail("domain checks", failure_reason(e)))
    return out


def render(results: list[CheckResult]) -> str:
    """One aligned line per check; a remedy is indented beneath a warning or failure."""
    w = max((len(r.name) for r in results), default=0)
    lines = []
    for r in results:
        lines.append(f"{r.status.upper():<4}  {r.name:<{w}}  {r.detail}")
        if r.remedy and r.status != "ok":
            lines.append(f"{'':<4}  {'':<{w}}  -> {r.remedy}")
    return "\n".join(lines)
