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
"""Coding runtime resolution and engineering gateway identity: no docker, no gateway.

    python3 -m pytest -q tests/test_domain_runtime.py
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rrsi.domain import load_domain  # noqa: E402

CODING = ROOT / "domains" / "coding"
ENG = ROOT / "domains" / "eng"


@pytest.fixture(scope="module")
def coding():
    load_domain("coding")
    return sys.modules["domains.coding.adapter"]


@pytest.fixture(scope="module")
def eng():
    load_domain("eng")
    return sys.modules["domains.eng.adapter"]


# ---- coding: one resolution of venv and interpreter ----------------------------
def test_coding_runtime_defaults_to_the_domain_venv(coding):
    rt = coding.coding_runtime({})
    venv = (CODING / ".venv").resolve()
    assert rt == {"venv": str(venv), "python": str(venv / "bin" / "python")}


def test_coding_venv_alone_moves_the_interpreter(coding, tmp_path):
    rt = coding.coding_runtime({"RRSI_CODING_VENV": str(tmp_path / "v")})
    assert rt["venv"] == str((tmp_path / "v").resolve())
    assert rt["python"] == str((tmp_path / "v").resolve() / "bin" / "python")


def test_coding_python_override_wins_for_python_only(coding, tmp_path):
    rt = coding.coding_runtime({"RRSI_CODING_VENV": str(tmp_path / "v"),
                                "RRSI_CODING_PYTHON": "/opt/py/bin/python"})
    assert rt["python"] == "/opt/py/bin/python"
    assert rt["venv"] == str((tmp_path / "v").resolve())


def test_harbor_env_keeps_a_user_set_venv(coding, tmp_path):
    user = {"RRSI_CODING_VENV": str(tmp_path / "v"), "OTHER": "x"}
    env = coding.harbor_env(tmp_path / "wt", user)
    assert env["RRSI_CODING_VENV"] == str((tmp_path / "v").resolve())
    assert env["RRSI_CODING_ROOT"] == str(tmp_path / "wt" / "domains" / "coding")
    assert env["OTHER"] == "x" and env["MODEL"]
    assert user == {"RRSI_CODING_VENV": str(tmp_path / "v"), "OTHER": "x"}   # input untouched


def test_harbor_env_defaults_to_the_domain_venv_and_matches_smoke(coding, tmp_path):
    env = coding.harbor_env(tmp_path, {})
    assert env["RRSI_CODING_VENV"] == coding.coding_runtime({})["venv"]
    assert env["RRSI_CODING_VENV"] == str((CODING / ".venv").resolve())


# ---- eng: gateway identity -----------------------------------------------------
PORT = 8996


def _identity(ws: Path, **over) -> Path:
    info = {"service": "rrsi-eng-gateway", "pid": os.getpid(), "port": PORT,
            "workspace_base": str(ws)}
    info.update(over)
    f = ws / f".gateway-{PORT}.json"
    f.write_text(json.dumps(info))
    return f


def test_identity_valid_file_is_accepted(eng, tmp_path):
    _identity(tmp_path)
    assert eng.gateway_identity_issue(tmp_path, PORT) is None


def test_identity_missing_file(eng, tmp_path):
    assert "no gateway identity file" in eng.gateway_identity_issue(tmp_path, PORT)


def test_identity_wrong_port(eng, tmp_path):
    _identity(tmp_path)
    assert eng.gateway_identity_issue(tmp_path, PORT + 1)            # no file for that port
    _identity(tmp_path, port=PORT + 1)
    assert "records port" in eng.gateway_identity_issue(tmp_path, PORT)


def test_identity_wrong_workspace_root(eng, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _identity(tmp_path, workspace_base=str(other))
    assert "workspace base" in eng.gateway_identity_issue(tmp_path, PORT)
    _identity(tmp_path, workspace_base="")
    assert "workspace base" in eng.gateway_identity_issue(tmp_path, PORT)


def test_identity_wrong_service(eng, tmp_path):
    _identity(tmp_path, service="something-else")
    assert "not an rrsi-eng-gateway" in eng.gateway_identity_issue(tmp_path, PORT)


def test_identity_corrupt_file(eng, tmp_path):
    (tmp_path / f".gateway-{PORT}.json").write_text("{not json")
    assert "unreadable" in eng.gateway_identity_issue(tmp_path, PORT)


def test_identity_dead_pid(eng, tmp_path):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    _identity(tmp_path, pid=p.pid)
    assert "not running" in eng.gateway_identity_issue(tmp_path, PORT)
    _identity(tmp_path, pid=None)
    assert "no valid pid" in eng.gateway_identity_issue(tmp_path, PORT)


def _listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_ensure_gateway_refuses_a_foreign_listener(eng, tmp_path, monkeypatch):
    s, port = _listener()
    monkeypatch.setattr(eng, "GATEWAY_PORT", port)
    try:
        with pytest.raises(RuntimeError) as e:
            eng.DOMAIN._ensure_gateway(ROOT, tmp_path)      # port is up: no script is started
    finally:
        s.close()
    msg = str(e.value)
    assert str(port) in msg and "no gateway identity file" in msg and "GATEWAY_PORT" in msg


def test_ensure_gateway_accepts_our_own_gateway(eng, tmp_path, monkeypatch):
    s, port = _listener()
    monkeypatch.setattr(eng, "GATEWAY_PORT", port)
    ws = tmp_path / "workspaces"
    ws.mkdir()
    (ws / f".gateway-{port}.json").write_text(json.dumps(
        {"service": "rrsi-eng-gateway", "pid": os.getpid(), "port": port,
         "workspace_base": str(ws)}))
    try:
        eng.DOMAIN._ensure_gateway(ROOT, tmp_path)
    finally:
        s.close()


# ---- eng: one default gateway port ---------------------------------------------
def _find(path: Path, pattern: str) -> int:
    m = re.search(pattern, path.read_text())
    assert m, f"no default port matching {pattern!r} in {path}"
    return int(m.group(1))


def test_default_gateway_port_is_the_same_everywhere():
    ports = {
        "rrsi.json": json.loads((ENG / "rrsi.json").read_text())["gateway_port"],
        "gateway.sh": _find(ENG / "scripts" / "gateway.sh", r'PORT="\$\{GATEWAY_PORT:-(\d+)\}"'),
        "final_eval.sh": _find(ENG / "scripts" / "final_eval.sh", r'GATEWAY_PORT="\$\{GATEWAY_PORT:-(\d+)\}"'),
        "gateway.py": _find(ENG / "mcp_gateway" / "gateway.py", r'"GATEWAY_PORT", "(\d+)"'),
        "config.py": _find(ENG / "config.py", r'"GATEWAY_PORT", "(\d+)"'),
    }
    assert len(set(ports.values())) == 1, ports
