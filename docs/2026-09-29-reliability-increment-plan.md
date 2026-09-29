# RRSI Reliability Increment — Implementation Plan

> Derived from [the product review](2026-09-29-product-review-and-improvement-plans.md) (the spec). This increment implements the review's first rollout slice: it fixes every defect the review reproduced or traced in source, and adds the small offline tooling those fixes need. It uses the review's recommended order: **3 → 1 → 2**, then the Plan 6 adapter fix and CI. Plans 4–5 and 7–11 are only partly covered or deferred (see "Out of scope").

**Goal:** A user can validate setup offline, preview workload, run/interrupt/resume an experiment without manual state repair, and trust that admitted measurements are complete and identifiable.

**Architecture:** Keep the Python CLI, domain adapters, Git-backed candidates and the regularized selection method. Add validation at the boundaries (config load, aggregation, cost evidence, cached-evaluation reuse), make round progression and settlement recoverable, and isolate adapter module loading.

**Tech stack:** Python ≥3.10 stdlib only for new code (no new runtime dependencies), pytest for tests, Git plumbing via `rrsi/gitops.py`.

## Global Constraints

- Keep core support at Python **3.10+**: `from __future__ import annotations`, no `tomllib`, `ExceptionGroup`, `typing.Self`, or `match`-only idioms.
- Preserve the regularized search algorithm and frozen evaluation boundaries; changes to evidence admissibility must be explicit (config-visible) and documented.
- Existing experiment artifacts (old `frontier.json`, `eval.json`, `decisions.json`, `history.jsonl`) must still load. Never silently reinterpret old evidence or destroy it during recovery.
- Offline diagnostics, previews and **all tests** make zero model calls and need no cloud credentials, Docker, or benchmark checkouts. Tests must pass without the `anthropic` package installed. Credential checks report presence only, never values.
- Keep held-out/OOD results outside proposer context and selection.
- New source files start with the repository's Apache-2.0 license header, copied from `pyproject.toml`'s `Copyright 2026 Google LLC` block (as `#` comments).
- Match surrounding code style: terse docstrings that cite the paper's symbols where relevant, compact helpers, `log(domain, msg)` for run logging.
- Test command (from repo root): `python3 -m pytest -q`. The existing `tests/test_core.py` (8 tests) must keep passing.
- Commit per task with a conventional message (`fix:`, `feat:`, `test:`, `docs:`, `ci:`). Do not push.

## Out of scope for this increment (deferred, per the spec's own sequencing)

Plan 4 spending limits and usage accounting; Plan 5 report command; Plan 6 toy domain; Plan 7 cleanup/OOD export ownership; Plans 8–11 (graphs, archive, evidence memory, experiment policy); per-trial `resume|rescore|fresh` attempt modes; workspace split revalidation.

---

## Task 1: Configuration validation and empty-project guard (spec Plan 3)

**Problem (reproduced by the review):** `RRSIConfig.load` accepts `k=0`, `m=9`, `b_min > b_max`, etc. `rrsi/llm.py::generate` computes `(start + attempt) % n` with `n = len(_PROJECTS)` before checking for an empty project list, so an unset `RRSI_VERTEX_PROJECTS` raises `ZeroDivisionError` instead of the intended message.

**Files:** modify `rrsi/config.py`, `rrsi/llm.py`, `rrsi/loop.py` (import the label constant), `rrsi.py`; create `tests/test_configuration.py`.

**Interfaces:**
- `rrsi/config.py`: `VARIANT_LABELS = "ABCDEFGH"` moves here (loop imports it from config); `class ConfigError(ValueError)`; `RRSIConfig.validate() -> None` collects **all** problems and raises one `ConfigError` whose message lists each as `<field>: <problem>`; `misspelled_keys(raw: dict) -> list[tuple[str, str]]` returns `(unknown_key, suggested_core_field)` pairs using `difflib.get_close_matches(key, core_fields, n=1, cutoff=0.8)`.
- `RRSIConfig.load(...)` calls `validate()` after applying overrides, and prints one stderr line per suspicious key: `[rrsi] warning: unknown config key '<key>' (did you mean '<field>'?); kept in notes`. Unknown keys still go to `notes` (domain extension fields are legitimate).
- Validation rules: ints (reject `bool`) — `T >= 1`, `k >= 1`, `1 <= m <= len(VARIANT_LABELS)`, `1 <= b_min <= b_max`, `w >= 1`, `0 <= m_draft <= m`, `n_prune >= 1`, `repair_rounds >= 0`, `n_fail_traces >= 0`, `n_success_traces >= 0`, `eval_parallel >= 1`. Floats (int or float, not bool, finite): `delta` is `None` or `>= 0`; `delta_z > 0`; `beta0, beta1, w_s, w_c, w_n >= 0`; `0 <= invalid_missing_frac <= 1`. Model names are non-empty strings.
- `rrsi/llm.py::generate`: if `_PROJECTS` is empty, raise `RuntimeError` with the same guidance text `_client_for` uses (mention `RRSI_VERTEX_PROJECTS`) **before** any modulo, sleep or client construction.
- `rrsi.py`: catch `ConfigError` from `RRSIConfig.load` and exit via `sys.exit(f"invalid configuration in {path}:\n{err}")`.

**Tests (`tests/test_configuration.py`, pytest style):**
- Each invalid value above (at least: `k=0`, `m=9`, `m=0`, `b_min=3,b_max=2`, `m_draft=3,m=2`, `T=0`, `invalid_missing_frac=1.5`, `beta1=float('nan')`, `delta=-0.1`, `k=True`) raises `ConfigError` naming the field; a config with two bad fields reports both in one error.
- Defaults validate; all three shipped `domains/*/rrsi.json` load and validate with **zero** misspelling warnings (use `capsys`).
- `misspelled_keys({"bta1": 1, "policy_label": "x"})` → `[("bta1", "beta1")]`.
- Overrides are validated (`RRSIConfig.load(p, k=0)` raises).
- `generate` with `monkeypatch.setattr(rrsi.llm, "_PROJECTS", [])` raises `RuntimeError` mentioning `RRSI_VERTEX_PROJECTS`, not `ZeroDivisionError`, and returns quickly (no sleep; monkeypatch `time.sleep` to fail if called).

**Acceptance:** new tests + `tests/test_core.py` pass; `python3 rrsi.py --domain coding --k 0 status` prints the field-specific error and exits nonzero.

---

## Task 2: Consistent coding runtime and engineering gateway identity (spec Plan 3)

**Problem (source-traced by the review):** the coding adapter sets `RRSI_CODING_VENV` to `domains/coding/.venv` unconditionally in the harbor environment, overriding a user's documented override, while `smoke` uses `RRSI_CODING_PYTHON` — so smoke and evaluation can use different interpreters. The engineering adapter/`rrsi.json`/README use gateway port **8996**, while `scripts/gateway.sh`, `mcp_gateway/gateway.py` and `scripts/final_eval.sh` default to **8994**; manual `gateway.sh start` defaults `WORKSPACE_BASE` to `domains/eng/workspaces` while the adapter uses `runs/eng/workspaces`; and the adapter accepts any listening socket as a ready gateway.

**Files:** modify `domains/coding/adapter.py`, `domains/eng/adapter.py`, `domains/eng/scripts/gateway.sh`, `domains/eng/mcp_gateway/gateway.py` (default port only), `domains/eng/scripts/final_eval.sh` (default port only), `domains/eng/README.md`, `domains/coding/README.md` (only if it documents the runtime variables); create `tests/test_domain_runtime.py`.

**Before editing:** read `domains/coding/scripts/run_eval.sh` and `domains/coding/README.md` to learn which variables (`RRSI_CODING_VENV`, `RRSI_CODING_PYTHON`, others) are documented and how `run_eval.sh` uses them. Preserve documented behavior; the fix is that user-set values win and both smoke and evaluation derive from one resolution.

**Interfaces:**
- `domains/coding/adapter.py`: pure `coding_runtime(environ) -> dict` returning `{"venv": <abs path>, "python": <path>}`: `venv = environ.get("RRSI_CODING_VENV") or HERE/.venv` (resolved absolute); `python = environ.get("RRSI_CODING_PYTHON") or venv/bin/python`. Module-level `PYBIN` derives from it. Pure `harbor_env(root, environ) -> dict` builds the harbor subprocess env using the same resolution (never clobbering a user-set `RRSI_CODING_VENV`); `_harbor` uses it.
- Engineering default port becomes **8996** everywhere (`gateway.sh`, `gateway.py`, `final_eval.sh`), matching `rrsi.json`'s `gateway_port`.
- `gateway.sh`: default `WORKSPACE_BASE` becomes the repo's `runs/eng/workspaces` (absolute, computed from the script location), matching the adapter with the default `--runs`. On successful `start` it writes an identity file `$WORKSPACE_BASE/.gateway-$PORT.json` containing `{"service": "rrsi-eng-gateway", "pid": <gateway pid>, "port": <port>, "workspace_base": "<abs path>"}`. If the port is already listening, `start` succeeds only when that identity file exists in the current `WORKSPACE_BASE` with a matching port; otherwise it prints an actionable error (port in use by a service not started for this workspace root; stop it or set `GATEWAY_PORT`) and exits 1.
- `domains/eng/adapter.py`: pure `gateway_identity_issue(workspace_base: Path, port: int) -> str | None` — `None` when `.gateway-<port>.json` exists in `workspace_base`, has `service == "rrsi-eng-gateway"`, matching `port`, a `workspace_base` that resolves to the same path, and a live `pid` (`os.kill(pid, 0)`; treat `PermissionError` as alive). Otherwise a one-line reason. `_ensure_gateway` refuses (raises `RuntimeError` with the reason plus a remedy) when the port is up but the identity check fails, both before and after starting the script.
- `domains/eng/README.md`: commands use the unified default port and explain `WORKSPACE_BASE` must match `runs/eng/workspaces` (or the `--runs` directory) when starting the gateway manually; remove any 8994/8996 inconsistency.

**Tests (`tests/test_domain_runtime.py`):** load adapters via `rrsi.domain.load_domain` (offline-safe) or import the pure functions from the loaded module.
- `coding_runtime({})` → venv `domains/coding/.venv`, python inside it; `RRSI_CODING_VENV` alone → python inside that venv; `RRSI_CODING_PYTHON` wins for python; `harbor_env` keeps a user-set `RRSI_CODING_VENV`.
- `gateway_identity_issue`: missing file; wrong port; wrong workspace root; wrong service; dead pid (use a pid from a finished `subprocess.Popen([sys.executable, "-c", "pass"])`); valid file with `os.getpid()` → `None`.
- Port consistency: parse the default port from `gateway.sh`, `gateway.py`, `final_eval.sh`, and `domains/eng/rrsi.json`; all equal.

**Acceptance:** new tests + existing suite pass; `bash -n domains/eng/scripts/gateway.sh` passes.

---

## Task 3: Offline `doctor` and `plan` commands (spec Plans 3 and 4, preview only)

**Problem:** there is no zero-cost way to check prerequisites or see how much work a run implies. The existing `round --dry-run` runs the analyst (paid model calls).

**Files:** create `rrsi/doctor.py`, `rrsi/planning.py`, `tests/test_doctor.py`, `tests/test_planning.py`; modify `rrsi.py`, `rrsi/domain.py` (optional hook), the three `domains/*/adapter.py` (domain checks), `README.md` (short section).

**Interfaces:**
- `rrsi/doctor.py`: `@dataclass CheckResult(name: str, status: str, detail: str, remedy: str = "")` with `status in {"ok", "warn", "fail"}`; `doctor(domain, cfg_or_error, repo: Path, runs_dir: Path, environ=os.environ) -> list[CheckResult]`; `render(results) -> str` (one aligned line per check, remedies indented beneath failures/warnings). Core checks: Python ≥ 3.10; `anthropic` importable via `importlib.util.find_spec` (do **not** import it); `RRSI_VERTEX_PROJECTS` set (report the **count** of projects only); `git` on PATH and `repo` is a Git work tree; config validity (`cfg_or_error` is an `RRSIConfig` or the `ConfigError` caught by the CLI); runs dir creatable/writable (without creating run state); `SKILL.md`/`PATTERNS.md` present; harness directory present; `evolve_ids()` loads (catch exceptions → `fail` with the message); then the domain's own `doctor_checks()`.
- `rrsi/domain.py`: `Domain.doctor_checks(self) -> list` returning `[]` by default.
- Domain checks (cheap, filesystem/PATH only): coding — resolved coding python exists, `docker` on PATH; eng — `AGENT_PY`, `BENCH_ROOT`, `GRADING_PY` exist, and on Linux `bwrap` on PATH (on other platforms a `warn` that the jailed `code_exec` needs Linux); workspace — `HARVEY_LAB_ROOT` exists.
- `rrsi/planning.py`: `@dataclass WorkloadEstimate` and `estimate_workload(cfg, n_tasks: int | None, n_smoke: int | None) -> WorkloadEstimate` with `baseline_trials = n_tasks * k`, `candidate_trials_max = T * m * n_tasks * k` (full screening passes, before retries), `smoke_trials_max = T * m * n_smoke` (smoke uses one trial per task), and search-role calls per round as an upper bound: `1` analyst + `m * (1 + repair_rounds)` proposer + `m * (1 + repair_rounds)` critic; totals over `T`. Unknown `n_tasks`/`n_smoke` yield `None` fields, never zero. `render_workload(est) -> str` states these are upper-bound counts, not spend or latency, and excludes judge calls, retries and infrastructure reruns.
- `rrsi.py`: `doctor [--json]` and `plan [--json]` subcommands, dispatched **before** `Run(...)` is constructed (no run directories created). `doctor` exits 1 if any check is `fail`. `plan` resolves `n_tasks` from `domain.evolve_ids()` and `n_smoke` from `len(domain.smoke_ids())`, catching failures (reported as unknown with the reason). Config errors: `doctor` reports them as a failed check instead of exiting; other commands keep Task 1's behavior.
- Update the CLI module docstring usage lines. Add a short README section "Check setup and preview workload" and note that `round --dry-run` makes paid analyst calls.

**Tests:**
- `tests/test_planning.py`: default arithmetic with shipped configs — coding (`n_tasks=89`) → 178 baseline / 7,120 candidate trials; workspace (`n_tasks=120`) → 240 / 9,600; engineering (`n_tasks=61`) → 244 / 19,520. `n_tasks=None` → `None` trial counts. Search-call bound for a small config computed by hand.
- `tests/test_doctor.py`: with `monkeypatch.delenv("RRSI_VERTEX_PROJECTS", raising=False)` the credential check fails and the output never contains a project value when set (set it to a sentinel string and assert the sentinel is absent from `render()` and JSON); invalid config surfaces as a failed check; a fake minimal domain object with a missing `SKILL.md` fails that check; `doctor --json` via `subprocess.run([sys.executable, "rrsi.py", "--domain", "coding", "--runs", <tmp>, "doctor", "--json"])` emits valid JSON and does not create `<tmp>/coding`.

**Acceptance:** tests pass; `python3 rrsi.py --domain coding plan` prints the 178 / 7,120 figures offline.

---

## Task 4: Driver round progression and truthful completion (spec Plan 1)

**Problem (reproduced by the review):** `rrsi/driver.py::drive` moves on to round `t+1` after round `t` fails to settle (the next round then fails its precondition), prints "all rounds settled" unconditionally, and ignores a failed calibration.

**Files:** modify `rrsi/driver.py`; create `tests/test_driver.py`.

**Interfaces / behavior:**
- `drive(...)` returns `"completed"` or `"stopped"`; failures raise `SystemExit(1)` (keep `sys.exit(1)` style).
- Each iteration: honor `STOP` (print a resumable message naming the next round, return `"stopped"`); compute `t = settled_rounds(fr)` (the earliest unsettled round); if `t >= T` break; if `start > t` exit 1 with a message that rounds before `start` are unsettled; otherwise run `round --t t`. On failure (nonzero rc or no new trajectory entry) increment the consecutive-failure counter and **retry the same round**; stop with exit 1 after `MAX_CONSECUTIVE_INFRA` consecutive failures. On success reset the counter.
- Calibration: when `calibration.json` is missing, run `calibrate`; nonzero rc or still-missing file → exit 1 with a message.
- Print `all rounds settled` only after verifying `settled_rounds(fr) >= T`; otherwise exit 1.
- Keep the log file layout (`logs/r{t}.log`, appended across retries) and the per-round incumbent line.

**Tests (`tests/test_driver.py`):** a fake entry script written into `tmp_path` that mimics `rrsi.py`'s CLI (`--domain`, `--runs`, subcommands `baseline`, `calibrate`, `round --t N`), appends each invocation to a log, and reads a JSON scenario (path via an environment variable) saying which invocations fail. Successful `round --t N` appends `{"t": N+1, ...}` to `frontier.json["trajectory"]` and updates `incumbent`. Cases:
- transient failure: round 0 fails once, `T=2` → round invocation order `0, 0, 1`, result `"completed"`.
- permanent failure: round 0 always fails → `SystemExit` with code 1 after exactly 3 attempts, round 1 never invoked, `all rounds settled` not printed (`capsys`).
- rc 0 but no trajectory entry counts as failure.
- `STOP` present → `"stopped"`, no round invoked.
- already-settled rounds are skipped; `start` beyond the settled count exits 1.
- calibration failure → exit 1 and no round invoked.

**Acceptance:** new tests + existing suite pass.

---

## Task 5: Recoverable settlement, round preconditions, writer lock (spec Plan 1)

**Problem (reproduced / source-traced by the review):** `Run.round` writes history, updates the branch, then writes the frontier in separate non-atomic steps; a crash after the branch update leaves a tree mismatch that the next invocation rejects. `round(t)` accepts an already-settled `t` and truncates later trajectory entries. `reevaluate` deletes `eval.json` before `readjudicate` checks that no later rounds exist. A round whose every screened candidate failed evaluation for infrastructure reasons still settles (H_{t+1} = H_t) and resets the driver's failure counter. Two concurrent writers are not prevented.

**Files:** create `rrsi/state.py`, `tests/fake_domain.py` (reusable offline test domain), `tests/test_run_recovery.py`; modify `rrsi/loop.py`, `rrsi/gitops.py` (compare-and-swap ref update helper), `rrsi.py` (lock around mutating commands).

**Interfaces / behavior:**
- `rrsi/state.py`: `atomic_write_json(path, obj)` (write a temp file in the same directory, flush + `os.fsync`, `os.replace`); `class RunLock` context manager using `fcntl.flock(LOCK_EX | LOCK_NB)` on `runs/<domain>/.lock`, writing the holder pid; on contention raise `SystemExit` naming the domain and holder pid.
- `rrsi/gitops.py`: `update_ref_cas(repo, branch, new, old)` → `git update-ref refs/heads/<branch> <new> <old>`, raising on failure.
- `Run.save_frontier` uses `atomic_write_json`.
- Settlement record `runs/<domain>/settlement.json` (atomic): `{"t", "kind": "round"|"readjudicate", "phase": "pending"|"done", "old_commit", "new_commit", "frontier": <complete new frontier dict>}`. `Run._settle(t, kind, new_commit, fr_new)`: write the record as `pending` → `update_ref_cas(old→new)` (for `round`, verify `new` descends from `old` first; skip the ref update when `new == old`) → `save_frontier(fr_new)` → mark `done`. Both `round` and `readjudicate` settle through it.
- `Run.recover() -> int | None`: if a `pending` record exists: branch tip == `old_commit` → CAS to `new_commit`; tip == `new_commit` → nothing; anything else → `SystemExit` explaining that the branch moved unexpectedly (no files changed). Then write the recorded frontier, mark `done`, log it, and return the record's `t`. Idempotent.
- `round(t)`: call `recover()` first; if it just settled round `t`, log and return (exit 0). Before any analysis or paid work: if the trajectory already has an entry beyond `t` (`len(trajectory) > t + 1`), `SystemExit` saying round `t` is already settled and pointing to `readjudicate`/`reevaluate` — no files, refs or evaluations change. Keep the existing "run earlier rounds" and harness-tree checks.
- After step 5 (evaluation) and before any decision/history writes: if at least one candidate reached evaluation and **all** of them have `gate_failure == "eval_invalid"`, `SystemExit` saying the round was not settled because every screened candidate failed evaluation (infrastructure); completed drafts/evaluations are reused on re-run.
- `reevaluate(t)`: validate the same preconditions as `readjudicate` (no rounds after `t`, round directory exists) **before** deleting or re-running anything. `readjudicate` and `reevaluate` also call `recover()` first.
- `rrsi.py`: wrap `baseline`, `calibrate`, `round`, `readjudicate`, `reevaluate`, `heldout` and `smoke` in `RunLock`. `run` (the driver) must not hold the lock (its child processes take it). `status`, `doctor`, `plan` take no lock.
- `tests/fake_domain.py`: a deterministic `Domain` subclass for a temp Git repo containing `domains/fake/{SKILL.md,PATTERNS.md,harness/policy.txt}`; score of every trial is read from `policy.txt` (e.g. `score=0.6`), tokens fixed; counts `run()` calls; `smoke` returns `(True, {})`; `load_trial` returns a small dict; `render_trace`/`task_row` return strings. A fixture helper builds the repo (initial commit), a `Run`, and monkeypatches `rrsi.loop.analyze`, `rrsi.loop.propose` (writes a new score into the variant worktree's `policy.txt` and returns `{"status": "done", "n_edits": 1, "edits": [{"id": "C1", "component": "prompt", "hypothesis": "h"}], "mechanism": "m"}`), `rrsi.loop.review` (`{"verdict": "accept"}`) and `rrsi.loop.load_digests`. Use `cfg.delta` set explicitly.

**Tests (`tests/test_run_recovery.py`):**
- happy path: baseline + round 0 settles; frontier, branch tip and history agree; `settlement.json` phase `done`.
- crash after the ref update (monkeypatch `Run.save_frontier` to raise once during settlement): a fresh `Run` → `recover()` settles; frontier trajectory has `t=1`; branch tip equals the recorded winner; history has exactly one record per variant for round 0; the fake domain's `run()` count does not increase when `round(0)` is re-invoked after recovery (it returns early).
- crash after writing the pending record but before the ref update: recovery CASes the ref and writes the frontier.
- branch moved to an unrelated commit with a pending record → `SystemExit`, nothing changed.
- `round(0)` after rounds 0 and 1 settled → `SystemExit`; bytes of `frontier.json`, `history.jsonl`, all `eval.json`, and the branch ref unchanged.
- `reevaluate(0)` while round 1 exists → `SystemExit` and every round-0 `eval.json` still present.
- all-invalid: fake scoring returns all trials missing → `round(0)` raises `SystemExit`, frontier unchanged, no round-0 history records.
- lock: holding `RunLock` then entering a second `RunLock` on the same path raises `SystemExit`.

**Acceptance:** new tests + existing suite pass.

---

## Task 6: Complete score and cost evidence (spec Plan 2, validity subset)

**Problem (reproduced by the review):** `aggregate` trusts adapter cardinality — one supplied success at `k=2` yields `S=1.0`, `n_expected=2`, `missing=0`. Rewards are not checked for NaN/range, arrays for alignment, or task sets for emptiness. `relative_cost_change` returns `0.0` when either side lacks token counts, so a candidate with unknown cost is admitted as if cost were unchanged.

**Files:** modify `rrsi/evaluate.py`, `rrsi/selection.py`, `rrsi/config.py` (one field), `rrsi/loop.py` (log formatting), and any other caller of `relative_cost_change`/`aggregate` found by grep; create `tests/test_evidence_integrity.py`; add a README note.

**Interfaces / behavior:**
- `rrsi/evaluate.py`: `class EvaluationError(ValueError)`. `aggregate(job, k, per_task, extra=None, expected_ids=None)`:
  - `expected_ids`: duplicates → error; empty → error; a requested task absent from `per_task` is filled with `k` missing trials (reward 0.0, weight 1.0, tokens `None`, `missing=k`); a task in `per_task` not requested → error. Without `expected_ids`, `per_task` must be nonempty.
  - per task: `weights`/`tokens` lengths must equal `rewards` length; more than `k` rewards → error; fewer than `k` → pad to `k` with reward 0.0, weight `max(existing weights)` (or 1.0 if none), tokens `None`, and add the deficit to `missing` (missing trials keep the full denominator — the module docstring's contract); rewards finite and in `[0, 1]`; weights finite and `>= 0`; `0 <= missing <= k` after padding.
  - `EvalResult` gains `token_coverage: float | None = None` = fraction of the `n_expected` trial slots with an observed positive token count (the same set the unchanged `C` estimator averages). `to_json` writes it; `from_json` tolerates its absence (old artifacts load with `None`).
  - `evaluate()` passes `expected_ids=ids`.
- `relative_cost_change(C_cand, C_inc) -> float | None`: `None` when either side is `None` or the incumbent cost is not positive.
- `rrsi/config.py`: `allow_unknown_cost: bool = False` (validate as bool), documented as a research override.
- `rrsi/selection.py::judge`: after the floor check, if `delta_C` is `None`: default → not admissible, reason `cost evidence incomplete: ...` (name which side lacks tokens and mention `allow_unknown_cost`); with the override → run the cost rule with `0.0`, keep `Decision.delta_C = None` (unknown is never reported as a saving), and append `(cost unknown; allow_unknown_cost override)` to the reason.
- `rrsi/loop.py`: every log line formatting `delta_C` handles `None` (prints `n/a`).
- README: one short paragraph on the admissibility change and the override.

**Tests (`tests/test_evidence_integrity.py`):** the review's probe (`rewards=[1.0]`, `k=2` → `S=0.5`, `missing=1`, `n_expected=2`); NaN reward; reward 1.5; negative weight; mismatched weights/tokens lengths; more than `k` rewards; empty `per_task`; `expected_ids` duplicate/missing/unexpected cases; token coverage value; `EvalResult.from_json` on a dict without `token_coverage`; `relative_cost_change` `None` cases; `select_round` blocks a gaining candidate whose `C` is `None` by default and admits it with `allow_unknown_cost=True` while `delta_C` stays `None`; weighted padding keeps the criteria-weighted denominator.

**Acceptance:** new tests + existing suite pass (existing `test_core.py` expectations unchanged).

---

## Task 7: Evaluation provenance for cached reuse and held-out labels (spec Plan 2, identity subset)

**Problem (source-traced by the review):** cached candidate evaluations are reused because a file exists; evaluation records carry no harness/task/`k` identity; held-out job names derive only from the user's label while runners treat a job as complete by result count, so reusing a label for a different ref silently returns the old harness's results.

**Files:** create `rrsi/provenance.py`, `tests/test_evaluation_provenance.py`; modify `rrsi/evaluate.py`, `rrsi/loop.py`; README note. Reuse `tests/fake_domain.py` from Task 5.

**Interfaces / behavior:**
- `rrsi/provenance.py`: `SCHEMA = 1`; `class ProvenanceError(RuntimeError)`; `fingerprint(repo, ref, harness_rel, ids, k) -> dict` = `{"schema", "commit" (full SHA of ref), "harness_tree", "k", "n_tasks", "task_ids_sha256" (SHA-256 of the newline-joined ids in order)}`; `mismatches(expected, recorded) -> list[str]` naming each differing field as `field: recorded X != expected Y` (ignoring `schema` unless both present and different); `manifest_path(runs_dir, job) -> Path` = `runs_dir/manifests/<job>.json` (**outside** `jobs/<job>/`, which runners own and may rename).
- `EvalResult` gains `provenance: dict | None = None` (tolerant `from_json`).
- `evaluate(domain, root, runs_dir, job, ids, k, log_prefix="", provenance=None)`: when `provenance` is given and a manifest exists, `mismatches` → raise `ProvenanceError` **before** invoking the runner; when absent, write it atomically (use `rrsi.state.atomic_write_json`) **before** invoking the runner; attach `provenance` to the returned result.
- `Run.baseline`, `Run._evaluate` and `Run.heldout` pass a fingerprint for the ref they evaluate (branch tip, candidate commit, resolved held-out ref).
- `Run._evaluate` cached reuse: if the cached `eval.json` has provenance that mismatches the candidate's fingerprint → `SystemExit` naming the file and the mismatched fields (no deletion); if it has none (legacy) → reuse with a logged warning.
- `Run.heldout`: if the job directory already exists and there is no manifest (legacy or unknown origin) → `SystemExit` asking for a new `--label`; a manifest mismatch (different ref, `k`, or task set) → `SystemExit` naming the label, the recorded commit and the new commit. Same label + same inputs → reuse.
- README: note on manifests and label reuse.

**Tests (`tests/test_evaluation_provenance.py`):** fingerprint stable for identical inputs and different when commit, `k`, task order/membership change; `evaluate()` writes the manifest before calling the runner (assert inside the fake runner that the manifest exists); a second `evaluate()` with a different `k` raises `ProvenanceError` and the runner call count is unchanged; `Run.heldout("x", ...)` for ref A then for ref B → `SystemExit` naming both commits, runner not invoked the second time; same label + same ref reuses; cached candidate `eval.json` with mismatched provenance → `SystemExit`; legacy `eval.json` without provenance → reused.

**Acceptance:** new tests + existing suite pass.

---

## Task 8: Isolated, package-qualified domain modules (spec Plan 6)

**Problem (reproduced by the review):** all three adapters insert their own directory on `sys.path` and `import briefs` / `import render` as top-level modules, so loading a second adapter in the same process reuses the first domain's cached `briefs`/`render`.

**Files:** create `domains/__init__.py` and `domains/{coding,eng,workspace}/__init__.py` (license header + one-line docstring); modify the three `domains/*/adapter.py`, `rrsi/domain.py`, `rrsi/doctor.py`; create `tests/test_domain_contract.py`.

**Interfaces / behavior:**
- Adapters import their siblings package-qualified (`from domains.coding import briefs, render`, likewise any other bare sibling import such as the workspace `split_workspace` import); they keep inserting only the **repo root** on `sys.path`. Before removing `sys.path.insert(0, str(HERE))`, grep each domain for other in-process bare imports of sibling modules; scripts launched as subprocesses are unaffected and must not change.
- `rrsi/domain.py::load_domain` imports `domains.<name>.adapter` with `importlib.import_module` (repo root on `sys.path`), keeping the `unknown domain` error for a missing adapter file.
- `rrsi/domain.py`: `validate_domain(domain) -> list[str]` — required methods overridden from `Domain` (`evolve_ids`, `smoke_ids`, `run`, `score`, `load_trial`, `render_trace`, `task_row`, `smoke`), `briefs` has `analyst`, `digester`, `proposer`, `critic`, `SKILL.md`/`PATTERNS.md` exist under `domain.root`. Must not call `evolve_ids()` or load external data.
- `rrsi/doctor.py`: add a `domain contract` check using `validate_domain`.

**Tests (`tests/test_domain_contract.py`):** for all six load orders of `coding`, `eng`, `workspace`, run a fresh `subprocess` (clean module cache) that loads the three domains in that order and asserts each domain's `briefs["proposer"]` equals the text built from its own `domains.<name>.briefs` module (compare against coding's `{policy}` substitution appropriately) and that the three `render` modules are distinct objects with the right `__name__`; loading the same domain twice returns a working domain; `validate_domain` returns `[]` for all three shipped domains and reports issues for a deliberately incomplete subclass.

**Acceptance:** new tests + full suite pass.

---

## Task 9: CI and operator documentation (spec Plans 3 and 6)

**Files:** create `.github/workflows/tests.yml`; modify `README.md`, `CONTRIBUTING.md` (short "Running tests" note), and `docs/2026-09-29-product-review-and-improvement-plans.md` (append an "Implementation status" section; do not rewrite the review).

**Behavior:**
- Workflow `tests`: triggers on `push` and `pull_request`; matrix Python `3.10`, `3.11`, `3.12` on `ubuntu-latest`; `actions/checkout@v4`, `actions/setup-python@v5`; install **only** `pytest` (proves tests need no model SDK or benchmark deps); run `python -m pytest -q`.
- README: an "Operator journey" subsection near the quickstart — `doctor` → `plan` → `smoke` → `baseline` → `run`; recovery semantics (the driver retries the earliest unsettled round and stops after 3 consecutive failures; `STOP`; interrupted settlements are completed automatically from `runs/<domain>/settlement.json`; one writer per domain via the run lock; re-running a settled round is refused); evidence rules (configuration validation; missing trials keep the denominator; unknown token cost blocks admission unless `allow_unknown_cost`; evaluation manifests under `runs/<domain>/manifests/`; held-out labels are bound to one ref). Consolidate any README paragraphs Tasks 3, 6 and 7 added so there is one coherent section rather than scattered notes. Keep the existing research content intact.
- Review doc "Implementation status" section: a table listing, per plan, what this increment delivered (with file/test names) and what remains open, plus a pointer to `docs/2026-09-29-reliability-increment-plan.md`. Honest about partial coverage.

**Acceptance:** `python3 -m pytest -q` passes; the workflow YAML parses (`python3 -c "import yaml, sys; yaml.safe_load(open('.github/workflows/tests.yml'))"` if PyYAML is available, otherwise careful review).
