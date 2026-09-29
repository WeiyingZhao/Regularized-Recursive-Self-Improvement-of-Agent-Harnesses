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
"""Identity of an evaluation: which harness, on which tasks, with how many trials.

An evaluation of H' on D with k trials is only reusable for the same (H', D, k).
The fingerprint pins all three: the commit (and its harness tree), k, and the
ordered task ids. It is written as a manifest before the runner starts, at
runs/<domain>/manifests/<job>.json, outside jobs/<job>/ (runners own that
directory and may rename it), and stored in eval.json. A later evaluation under
the same job name must present the same fingerprint or it is refused, never
silently answered with the earlier harness's results.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import gitops as G

SCHEMA = 1


class ProvenanceError(RuntimeError):
    """A job name that already belongs to a different (commit, k, task set)."""


def fingerprint(repo, ref: str, harness_rel: str, ids: list[str], k: int) -> dict:
    """Identity of evaluating `ref`'s harness on `ids` in order with k trials."""
    commit = G.full_rev(repo, ref)
    if not commit:
        raise ProvenanceError(f"cannot resolve {ref!r} to a commit in {repo}")
    return {"schema": SCHEMA, "commit": commit,
            "harness_tree": G.tree_hash(repo, commit, harness_rel),
            "k": k, "n_tasks": len(ids),
            "task_ids_sha256": hashlib.sha256("\n".join(ids).encode()).hexdigest()}


def mismatches(expected: dict, recorded: dict) -> list[str]:
    """One `field: recorded X != expected Y` per differing field of `expected`.
    `schema` counts only when both sides carry it and they differ."""
    out = []
    if "schema" in expected and "schema" in recorded and expected["schema"] != recorded["schema"]:
        out.append(f"schema: recorded {recorded['schema']} != expected {expected['schema']}")
    for f, want in expected.items():
        if f != "schema" and recorded.get(f) != want:
            out.append(f"{f}: recorded {recorded.get(f)} != expected {want}")
    return out


def manifest_path(runs_dir: Path | str, job: str) -> Path:
    return Path(runs_dir) / "manifests" / f"{job}.json"


def read_manifest(runs_dir: Path | str, job: str) -> dict | None:
    """The recorded fingerprint of `job`; None when there is none. An unreadable
    manifest is an error (the job's identity is unknown), never overwritten."""
    p = manifest_path(runs_dir, job)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise ProvenanceError(f"{p}: unreadable evaluation manifest ({e}); it is kept, "
                              f"inspect it or use a new job name") from None
    if not isinstance(rec, dict):
        raise ProvenanceError(f"{p}: evaluation manifest is not a JSON object; it is kept, "
                              f"inspect it or use a new job name")
    return rec
