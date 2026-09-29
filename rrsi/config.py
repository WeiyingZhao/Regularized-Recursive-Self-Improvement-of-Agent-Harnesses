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
"""Hyperparameters of RRSI (paper Appendix "Hyperparameters") plus the
engineering knobs a run needs that are not part of the method.

Symbols follow the paper:

  T          number of rounds, t = 0..T-1
  k          trials per task inside Evaluate (Eq. estimate)
  m          candidate harnesses drawn per round, |C_t| before screening
  b_min/b_max  bounds of the annealed L0 edit budget b_t (Eq. anneal)
  w          stall window of the exploration flag sigma_t (Eq. explore)
  m_draft    candidate slots reserved for exploratory edits when stalled
  delta      empirical noise band (Sec. stability-aware acceptance)
  beta0/beta1  L1 cost rule for gaining candidates (Eq. tokenbudget)
  w_s/w_c/w_n  shaped rule for candidates inside the noise band (Alg. 2 l.5)
  n_prune    window of the recent-yield summary g_t (Eq. yield / prune)

Units: scores S are fractions in [0,1] (Eq. estimate), cost C is mean policy
tokens per trial, Delta C is RELATIVE (Eq. tokenbudget), so beta0 = 0.10 means
"10% more tokens for free" and beta1 = 40 means "each +1pp of S buys +40%".
"""

from __future__ import annotations

import difflib
import json
import math
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

VARIANT_LABELS = "ABCDEFGH"     # variant ids A.. ; m <= len(VARIANT_LABELS)


class ConfigError(ValueError):
    """One or more invalid hyperparameters (message: one `<field>: <problem>` per line)."""


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def misspelled_keys(raw: dict) -> list[tuple[str, str]]:
    """(unknown key, closest core field) for keys that look like typos of a core
    hyperparameter; other unknown keys are legitimate domain extensions."""
    core = [f.name for f in fields(RRSIConfig) if f.name != "notes"]
    out = []
    for key in raw:
        if key in core or key == "notes":
            continue
        near = difflib.get_close_matches(key, core, n=1, cutoff=0.8)
        if near:
            out.append((key, near[0]))
    return out


@dataclass
class RRSIConfig:
    # ---- horizon and estimator -------------------------------------------
    T: int = 20
    k: int = 2
    m: int = 2
    # ---- proposal side (Algorithm 1) -------------------------------------
    b_min: int = 1
    b_max: int = 4
    w: int = 3
    m_draft: int = 1
    # ---- selection side (Algorithm 2) ------------------------------------
    delta: float | None = None      # None -> runs/<domain>/calibration.json
    delta_z: float = 2.0            # calibration: delta = z * sd(null Delta S)
    beta0: float = 0.10
    beta1: float = 40.0
    w_s: float = 100.0
    w_c: float = 15.0
    w_n: float = 0.5
    n_prune: int = 4
    # ---- engineering knobs (not part of the method) ----------------------
    repair_rounds: int = 5          # critic -> proposer repair attempts
    invalid_missing_frac: float = 0.15
    n_fail_traces: int = 22
    n_success_traces: int = 6
    eval_parallel: int = 1          # candidates evaluated concurrently
    proposer_model: str = "claude-opus-4-8"
    analyst_model: str = "claude-opus-4-8"
    critic_model: str = "claude-opus-4-8"
    notes: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str, **overrides) -> "RRSIConfig":
        raw = json.loads(Path(path).read_text())
        for key, near in misspelled_keys(raw):
            print(f"[rrsi] warning: unknown config key '{key}' (did you mean "
                  f"'{near}'?); kept in notes", file=sys.stderr)
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in raw.items() if k in known}
        kw["notes"] = {k: v for k, v in raw.items() if k not in known}
        kw.update({k: v for k, v in overrides.items() if v is not None})
        cfg = cls(**kw)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Raise one ConfigError listing every invalid field (ranges of the paper's
        hyperparameters; m is bounded by the variant labels A..H)."""
        errs: list[str] = []

        def integer(name: str, lo: int, hi: int | None = None) -> bool:
            v = getattr(self, name)
            if not _is_int(v):
                errs.append(f"{name}: must be an integer (got {v!r})")
            elif v < lo or (hi is not None and v > hi):
                rng = f">= {lo}" if hi is None else f"between {lo} and {hi}"
                errs.append(f"{name}: must be {rng} (got {v})")
            else:
                return True
            return False

        def number(name: str, lo: float, hi: float | None = None, strict: bool = False) -> None:
            v = getattr(self, name)
            if not _is_num(v):
                errs.append(f"{name}: must be a finite number (got {v!r})")
            elif v < lo or (strict and v == lo) or (hi is not None and v > hi):
                rng = (f"> {lo}" if strict else f">= {lo}") if hi is None else f"between {lo} and {hi}"
                errs.append(f"{name}: must be {rng} (got {v})")

        integer("T", 1)
        integer("k", 1)
        integer("m", 1, len(VARIANT_LABELS))
        lo_ok, hi_ok = integer("b_min", 1), integer("b_max", 1)
        if lo_ok and hi_ok and self.b_min > self.b_max:
            errs.append(f"b_min: must be <= b_max (got {self.b_min} > {self.b_max})")
        integer("w", 1)
        if not _is_int(self.m_draft) or self.m_draft < 0 or (_is_int(self.m) and self.m_draft > self.m):
            errs.append(f"m_draft: must be an integer between 0 and m (got {self.m_draft!r}, "
                        f"m={self.m!r})")
        for name in ("n_prune", "eval_parallel"):
            integer(name, 1)
        for name in ("repair_rounds", "n_fail_traces", "n_success_traces"):
            integer(name, 0)
        if self.delta is not None:
            number("delta", 0.0)
        number("delta_z", 0.0, strict=True)
        for name in ("beta0", "beta1", "w_s", "w_c", "w_n"):
            number(name, 0.0)
        number("invalid_missing_frac", 0.0, 1.0)
        for name in ("proposer_model", "analyst_model", "critic_model"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v.strip():
                errs.append(f"{name}: must be a non-empty string (got {v!r})")
        if errs:
            raise ConfigError("\n".join(errs))

    def dump(self) -> dict:
        return asdict(self)
