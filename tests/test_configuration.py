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
"""Configuration validation, typo warnings, and the empty-Vertex-project guard.

    python3 -m pytest -q tests/test_configuration.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import rrsi.llm                                             # noqa: E402
from rrsi.config import ConfigError, RRSIConfig, misspelled_keys  # noqa: E402


def _cfg(**kw) -> RRSIConfig:
    return RRSIConfig(**kw)


@pytest.mark.parametrize("field, kw", [
    ("T", {"T": 0}),
    ("k", {"k": 0}),
    ("k", {"k": True}),
    ("m", {"m": 9}),
    ("m", {"m": 0}),
    ("b_min", {"b_min": 3, "b_max": 2}),
    ("b_min", {"b_min": 0}),
    ("b_max", {"b_max": 0}),
    ("w", {"w": 0}),
    ("m_draft", {"m_draft": 3, "m": 2}),
    ("m_draft", {"m_draft": -1}),
    ("n_prune", {"n_prune": 0}),
    ("repair_rounds", {"repair_rounds": -1}),
    ("n_fail_traces", {"n_fail_traces": -1}),
    ("n_success_traces", {"n_success_traces": -1}),
    ("eval_parallel", {"eval_parallel": 0}),
    ("delta", {"delta": -0.1}),
    ("delta", {"delta": float("nan")}),
    ("delta_z", {"delta_z": 0.0}),
    ("beta0", {"beta0": -1.0}),
    ("beta1", {"beta1": float("nan")}),
    ("beta1", {"beta1": float("inf")}),
    ("w_s", {"w_s": True}),
    ("w_c", {"w_c": -0.5}),
    ("w_n", {"w_n": "1"}),
    ("invalid_missing_frac", {"invalid_missing_frac": 1.5}),
    ("invalid_missing_frac", {"invalid_missing_frac": -0.1}),
    ("proposer_model", {"proposer_model": ""}),
    ("analyst_model", {"analyst_model": "  "}),
    ("critic_model", {"critic_model": None}),
])
def test_invalid_value_names_the_field(field, kw):
    with pytest.raises(ConfigError) as e:
        _cfg(**kw).validate()
    assert any(line.startswith(field + ": ") for line in str(e.value).splitlines())


def test_all_problems_reported_in_one_error():
    with pytest.raises(ConfigError) as e:
        _cfg(k=0, m=9, beta1=-1.0).validate()
    lines = str(e.value).splitlines()
    assert [line.split(":")[0] for line in lines] == ["k", "m", "beta1"]


def test_defaults_and_boundaries_validate():
    _cfg().validate()
    _cfg(m=8, m_draft=8, b_min=2, b_max=2, delta=0.0, invalid_missing_frac=1.0,
         repair_rounds=0, n_fail_traces=0, n_success_traces=0, beta0=0, w_s=0.0).validate()
    _cfg(delta=None, w_s=3).validate()


@pytest.mark.parametrize("domain", sorted(p.parent.name for p in (ROOT / "domains").glob("*/rrsi.json")))
def test_shipped_domain_configs_load_without_warnings(domain, capsys):
    cfg = RRSIConfig.load(ROOT / "domains" / domain / "rrsi.json")
    cfg.validate()
    assert capsys.readouterr().err == ""


def test_shipped_domains_are_found():
    assert {"coding", "eng", "workspace"} <= {p.parent.name for p in (ROOT / "domains").glob("*/rrsi.json")}


def test_misspelled_keys_suggests_core_field():
    assert misspelled_keys({"bta1": 1, "policy_label": "x"}) == [("bta1", "beta1")]
    assert misspelled_keys({"beta1": 1, "notes": {}, "_doc": "x"}) == []


def test_load_warns_and_keeps_unknown_keys_in_notes(tmp_path, capsys):
    p = tmp_path / "rrsi.json"
    p.write_text(json.dumps({"k": 3, "bta1": 5.0, "policy_label": "x"}))
    cfg = RRSIConfig.load(p)
    err = capsys.readouterr().err
    assert err == ("[rrsi] warning: unknown config key 'bta1' (did you mean 'beta1'?); "
                   "kept in notes\n")
    assert cfg.k == 3 and cfg.beta1 == RRSIConfig().beta1
    assert cfg.notes == {"bta1": 5.0, "policy_label": "x"}


def test_overrides_are_validated(tmp_path):
    p = tmp_path / "rrsi.json"
    p.write_text("{}")
    assert RRSIConfig.load(p, k=3, delta=None).k == 3
    with pytest.raises(ConfigError, match="k: "):
        RRSIConfig.load(p, k=0)


def test_invalid_file_value_is_rejected(tmp_path):
    p = tmp_path / "rrsi.json"
    p.write_text(json.dumps({"m": 9, "b_min": 3, "b_max": 2}))
    with pytest.raises(ConfigError) as e:
        RRSIConfig.load(p)
    assert "m: " in str(e.value) and "b_min: " in str(e.value)


def test_config_error_is_a_value_error():
    assert issubclass(ConfigError, ValueError)


def test_generate_without_projects_fails_fast(monkeypatch):
    def no_sleep(_):
        raise AssertionError("generate must not sleep without projects")

    monkeypatch.setattr(rrsi.llm, "_PROJECTS", [])
    monkeypatch.setattr(time, "sleep", no_sleep)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="RRSI_VERTEX_PROJECTS"):
        rrsi.llm.generate("hi")
    assert time.monotonic() - t0 < 1.0
