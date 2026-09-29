# RRSI: Regularized Recursive Self-Improvement of Agent Harnesses

Check out our [paper](https://arxiv.org/abs/2609.24972) and [project page](https://regularized-rsi.com/) for more details.

## 🔥 Updates

- [09/21/2026] Our [paper](https://arxiv.org/abs/2609.24972) is out! Check it out! [[project page]](https://regularized-rsi.com/)

## 🧬 Overview

<p align="center">
  <img src="./assets/rrsi_overview.png" width="92%" alt="RRSI overview: proposal-side and selection-side regularization of the harness search">
</p>

An LLM agent's capability is largely set by its harness: the prompts, control
flow, tools, memory and context management around a frozen model. Evolving the
harness against a fixed evolve set is effective but overfits: the harness
memorizes the training tasks, and large in-distribution gains shrink or vanish
out of distribution. RRSI keeps the harness edit space open and regularizes the
search trajectory through it instead.

On the proposal side, an annealed budget caps how many independent edits one
candidate may bundle, the proposer is conditioned on the full edit history so a
falsified hypothesis is not redrawn, and a stalled run is redirected toward
components it has never exercised. On the selection side, a critic screens
every candidate for suite-specific logic before it is evaluated, a
noise-adjusted floor blocks gains within evaluation variance, a cost rule
requires added inference tokens to be paid for by measured gain, and
components that stop helping are pruned.

### ✨ Key features

* **Open edit space, regularized search.** Prompts, control flow, configuration, context management, tools, skills, memory and sub-agents may all be modified; the constraints act on how the search moves, not on what the harness may contain.
* **One method, three instances.** The same loop drives a terminal agent (Terminal-Bench 2.1), a document-work agent (Harvey LAB) and an engineering-design agent (EngDesign); each instance is a `Domain` adapter plus its starting harness.
* **Candidates in git worktrees.** Every candidate harness is drafted, screened and evaluated in its own worktree on a branch off `evolve/<domain>`; accepting one fast-forwards the branch, so the incumbent is always a commit.
* **Evidence you can audit.** The edit history records, per edit, the component, the hypothesis, the measured score and cost change and the verdict; the prompts the proposer, analyst and critic receive are plain files in `domains/<name>/`.

## 🧩 Method to code

| Paper | Code |
|---|---|
| Empirical score and cost estimate | `rrsi/evaluate.py: aggregate` (weighted per-trial rewards; a missing trial counts 0 with the full denominator) |
| Annealed edit budget b_t | `rrsi/schedule.py: edit_budget`, enforced in the proposer's done() |
| Edit history L_t, tried set T_t, recent yield g_t | `rrsi/history.py: History` (one JSONL record per edit; a = 1 only for the edits of the candidate that became H_{t+1}) |
| Stall flag, untried components, exploration directives | `rrsi/history.py: stall_flag, exploration`; reserved slots enforced in `rrsi/propose.py` |
| Analyze(H_t, D) | `rrsi/analyst.py` dispatching `rrsi/digester.py` |
| Proposer with (component, hypothesis, diff) tags | `rrsi/propose.py`; tags validated against the diff by `rrsi/components.py` |
| Critic (leakage screen before evaluation) | `rrsi/critic.py` (domain regex denylist plus LLM review, bounded repair) |
| Evaluate in parallel | `rrsi/evaluate.py`, `Run.round` thread pool |
| Noise-adjusted floor, cost rule, within-band rule, argmax | `rrsi/selection.py` |
| Novelty nu_t (structural component types never in a winning edit) | `rrsi/components.py: novelty` over K_str = client_tool, skill, memory, subagent |
| Prune set B_t | `History.prune_set`, handed to the proposer with the accepted machinery to remove |
| Noise band delta | fixed per instance in `rrsi.json` (0.017 / 0.004 / 0.020); `rrsi/calibrate.py` re-estimates it when `delta` is `null` (bootstrap over trials of the base evaluation, or repeated base evaluations) |
| Non-compensatory domain criteria | `Domain.guards` (engineering: valid-rate drop, no-submission rise) |

---

## ⚡️ Quickstart

### 0. Install

```bash
git clone https://github.com/google-research/rrsi.git && cd rrsi
pip install -e ".[dev]"            # the search core (Python 3.10 or newer)
python3 -m pytest tests
```

The benchmark runners live in their own environments: harbor for the coding instance (`domains/coding/.venv`), and a Python 3.11 environment with `pip install -e ".[agentic]"` for the workspace and engineering instances (`RRSI_AGENT_PYTHON`).

### 1. LLM configuration

The proposer, the analyst, the critic and the frozen policy are Claude Opus 4.8 on Vertex AI (`policy_model` in `domains/coding/rrsi.json`, `ORCHESTRATOR_MODEL` for the other two instances; any LiteLLM model string works). The Harvey LAB judge is Gemini 3.5 Flash.

```bash
gcloud auth application-default login
export VERTEX_PROJECT="your-project-id" VERTEXAI_PROJECT="your-project-id"
export VERTEX_LOCATION=global VERTEXAI_LOCATION=global
export RRSI_VERTEX_PROJECTS="your-project-id"
```

### 2. Run an instance

Every instance follows the same shape:

```bash
python3 rrsi.py --domain <coding|workspace|eng> smoke     # liveness: compile, construct, a couple of tasks
python3 rrsi.py --domain <name> baseline                   # Evaluate(H_0), seed runs/<name>/frontier.json
python3 rrsi.py --domain <name> run                        # rounds 0..T-1; resumes at the earliest unsettled round; touch runs/<name>/STOP to stop
python3 rrsi.py --domain <name> status
```

Each round drafts two candidates in their own git worktrees, screens them, evaluates both on the full evolve set and fast-forwards `evolve/<name>` to the winner. `runs/<name>/` holds the frontier, the edit history and the raw trials. Hyperparameters live in `domains/<name>/rrsi.json` and can be overridden on the command line (`--T`, `--k`, `--delta`, `--beta1`, ...); `readjudicate --t <t>` re-applies Algorithm 2 to a stored round and `reevaluate --t <t>` re-measures one after an infrastructure failure. `run` always resumes from the earliest unsettled round; `--start N` cannot skip one and exits 1 if `N` is beyond it. See [Operator journey](#operator-journey) for the order to run things in and for recovery and evidence rules.

Please refer to the specific document for the instance you want to run for its environment, its evaluation protocol and the out-of-distribution runs:

- [`domains/coding`](domains/coding/README.md): Terminal-Bench 2.1, then SWE-bench Verified
- [`domains/workspace`](domains/workspace/README.md): Harvey LAB, then JobBench, GDPval and APEX-Agents
- [`domains/eng`](domains/eng/README.md): EngDesign, then EngDesign v1 and Frontier-Eng

The short version of each:

```bash
# coding: Docker + harbor
python3 -m venv domains/coding/.venv && domains/coding/.venv/bin/pip install "harbor>=0.18"
python3 rrsi.py --domain coding baseline && python3 rrsi.py --domain coding run
bash domains/coding/scripts/swe_eval.sh                       # H_0 and the incumbent on SWE-bench Verified

# workspace: a Harvey LAB checkout at the pinned commit; the split is generated from it on first use
git clone https://github.com/harveyai/harvey-labs.git && (cd harvey-labs && git checkout 1da4750 && uv sync)
export HARVEY_LAB_ROOT=$PWD/harvey-labs RRSI_AGENT_PYTHON=~/venvs/rrsi-agentic/bin/python
python3 rrsi.py --domain workspace baseline && python3 rrsi.py --domain workspace run
python3 rrsi.py --domain workspace heldout --label champ      # the 40 held-out tasks; ood/run_{jobbench,gdpval,apex}.sh for the rest

# eng: the official EngDesign tasks in the verifier layout, a grading venv, a jailed tool gateway
git clone https://github.com/AGI4Engineering/EngDesign.git
python3 domains/eng/scripts/engdesign/build_engdesign_bench.py --engdesign-open EngDesign/EngDesign-Open --out domains/eng/engdesign_bench
python3 -m venv domains/eng/.venvs/engdesign && domains/eng/.venvs/engdesign/bin/pip install -r domains/eng/scripts/engdesign/requirements.txt
bash domains/eng/scripts/preflight.sh
python3 rrsi.py --domain eng baseline && python3 rrsi.py --domain eng run
bash domains/eng/scripts/final_eval.sh frontier               # Frontier-Eng, from a Frontier-Engineering checkout
```

### Operator journey

Start a new experiment in this order. The first two steps make no model call, need no credentials and create no `runs/` directories.

```bash
python3 rrsi.py --domain <name> doctor [--json]   # 1. prerequisites; exits 1 if any check fails
python3 rrsi.py --domain <name> plan [--json]     # 2. upper-bound workload for the configured T, k and m
python3 rrsi.py --domain <name> smoke             # 3. liveness of the starting harness (runs a couple of tasks)
python3 rrsi.py --domain <name> baseline          # 4. Evaluate(H_0), calibrate delta, seed the frontier
python3 rrsi.py --domain <name> run               # 5. rounds until T (does step 4 first if it is missing)
```

1. **`doctor`** checks Python, the `anthropic` package (found, not imported), that `RRSI_VERTEX_PROJECTS` is set (only the project count is shown), git, the configuration, the runs directory, the proposer constitution and harness files, the domain contract (required methods, briefs and constitution files; no task set is loaded for it), the evolve task set, and the instance's own tools (Docker, the benchmark checkout, `bwrap`). It reports all failures together, with a remedy where one exists, so fix them before spending anything.
2. **`plan`** prints baseline, candidate and smoke trial counts and search-role invocations (analyst, proposer, critic) for the configured `T`, `k` and `m`, for example 178 baseline and up to 7,120 candidate trials for `coding`. They are upper bounds, not spend or latency, and exclude judge calls, retries and infrastructure reruns. Search-role figures count invocations, not model calls or tokens: the analyst and its digester subagents run several model turns per invocation, and a proposer or critic invocation may make more than one model call. `round --dry-run` is not a free preview: it still makes paid analyst calls.
3. **`smoke`, `baseline`, `run`** are the steps that spend. `run` is the driver: it resumes from the earliest unsettled round and never skips ahead.

**Recovery.**

* *Progress.* A round is settled once the frontier trajectory has an entry for round t+1. A failed round is retried, and after 3 consecutive rounds that do not settle the driver stops and exits 1, so a broken environment cannot consume the horizon. A candidate rejected on measurement is not a failure: that round settles with H_{t+1} = H_t. A round whose every screened candidate failed evaluation for infrastructure reasons is not settled; re-running it reuses the completed drafts and evaluations. The driver reports `all rounds settled` only when T rounds are settled and exits 1 otherwise; `--start N` beyond the earliest unsettled round also exits 1.
* *Stop and resume.* `touch runs/<name>/STOP` stops the driver before the next round; it names that round, exits 0 and does not report completion. Remove the file and rerun `run` to resume.
* *Interrupted settlements.* A round settles as one recoverable step: `runs/<name>/settlement.json` is written as `pending`, `evolve/<name>` moves to H_{t+1} by compare-and-swap, the frontier is replaced atomically, and the record is marked `done`. If the process dies part-way, the next `round`, `baseline`, `readjudicate` or `reevaluate` completes the settlement from that record before anything else, without re-evaluating. If the branch has moved somewhere the record does not expect, the command exits explaining why and changes nothing; restore the branch, or remove `settlement.json` to abandon the settlement.
* *One writer per domain.* `baseline`, `calibrate`, `round`, `readjudicate`, `reevaluate`, `heldout` and `smoke` hold `runs/<name>/.lock` while they run (POSIX `flock`, released by the OS however the process ends). A second writer exits naming the holder's pid. `run` takes no lock itself, since each round it launches does; `status`, `doctor` and `plan` never do.
* *Settled rounds are final.* `round --t <t>` for a settled round is refused and changes nothing. `readjudicate --t <t>` and `reevaluate --t <t>` apply only to the most recently settled round, because a later round was drafted from its outcome; their preconditions are checked before any file is touched.

**Evidence rules.**

* *Configuration is validated at load.* `rrsi.json` and command-line overrides are range-checked (`T` and `k` at least 1, `1 <= m <= 8`, `1 <= b_min <= b_max`, `0 <= m_draft <= m`, finite nonnegative noise and cost parameters, and so on); every problem is reported at once and the command does not start. A key that looks like a misspelled core hyperparameter (`bta1`) prints a warning, and other unknown keys are kept as domain extensions. An empty `RRSI_VERTEX_PROJECTS` fails with a clear message.
* *Missing trials keep the denominator.* A task with fewer than k trials counts the shortfall as missing trials scoring 0. NaN or out-of-range rewards, misaligned weight or token lists, and duplicate or unrequested tasks fail the evaluation (`EvaluationError`) instead of inflating the score.
* *Unknown token cost blocks admission.* If the candidate or the incumbent has no positive token count, Delta C is unknown and the candidate is not admissible (`cost evidence incomplete`); earlier versions treated unknown as 0. Set `"allow_unknown_cost": true` in `rrsi.json` to admit such candidates on score alone (a research override; the decision still records Delta C as unknown). `eval.json` carries `token_coverage`, the share of trial slots that C is averaged over. `readjudicate` of a legacy round re-applies this rule, so a candidate without token counts that the old rule accepted is now rejected unless the override is set.
* *Evaluation manifests.* Every evaluation records its identity (commit, harness tree, `k`, and a hash of the ordered task ids) in `runs/<name>/manifests/<job>.json` before any trial runs, and in the job's `eval.json`. A job name is bound to that identity: reusing it for a different commit, `k` or task set is refused and the earlier results are kept. A cached candidate `eval.json` whose identity differs is refused; one without provenance (an older run) is reused with a warning. The incumbent's and calibration's stored evaluations, and `readjudicate`, do not yet re-verify provenance.
* *Held-out labels.* `heldout --label <label> [--ref <ref>]` binds the label to one ref (its commit), `k` and task set. Reusing the label for another `--ref` is refused and the recorded results are kept, so use a new label. A held-out job directory with no manifest (an older run) is refused the same way.

## 📊 Results

Numbers from the paper, with Claude Opus 4.8 as the frozen policy in every instance and every number measured against the unevolved harness H_0 in the same window. "Evolve" is the split the harness was searched on; the other rows never entered selection. Terminal-Bench, SWE-bench, JobBench, GDPval, APEX-Agents and EngDesign report pass rate, Harvey LAB the fraction of rubric criteria passed and Frontier-Eng Medal points.

| Domain | Benchmark | Role | H_0 | RRSI | Δ |
|:---|:---|:---|:---:|:---:|:---:|
| Coding | Terminal-Bench 2.1 | evolve | 74.2 | **80.2** | +6.0 |
| Coding | SWE-bench Verified | OOD | 82.0 | **83.8** | +1.8 |
| Agentic workspace | Harvey LAB | evolve | 89.4 | **90.5** | +1.1 |
| Agentic workspace | Harvey LAB | ID held-out | 86.9 | **89.2** | +2.3 |
| Agentic workspace | JobBench | OOD | 36.0 | **40.7** | +4.7 |
| Agentic workspace | GDPval | OOD | 48.8 | **52.3** | +3.5 |
| Agentic workspace | APEX-Agents | OOD | 34.2 | **37.9** | +3.7 |
| Engineering design | EngDesign | evolve | 50.0 | **54.9** | +4.9 |
| Engineering design | Frontier-Eng | OOD | 17.7 | **22.0** | +4.3 |

The search is not tied to one policy family: with Gemini 3.5 Flash as the frozen policy, the same coding instance goes from 64.6 to 78.7 on Terminal-Bench 2.1 and from 76.8 to 79.0 on SWE-bench Verified.

## 🧱 Adding a domain

A domain is a Python package, `domains/<name>/` (with an `__init__.py`), whose
`adapter.py` exports `DOMAIN`, an instance of `rrsi.domain.Domain` that
implements:

* `evolve_ids`, `heldout_ids`, `smoke_ids`: the task splits;
* `run(root, runs_dir, job, ids, k)` and `score(runs_dir, job, ids, k)`: run the harness checked out under `root` and return per-task trial rewards (Evaluate);
* `load_trial`, `render_trace`, `task_row`: the evidence the analyst, digester and proposer read;
* `smoke`: a liveness check of a candidate before it is evaluated;
* `critic_patterns`, `component_signals`, `briefs`, `guards`: the domain's leakage denylist, diff-to-component signals, role prompts and non-compensatory acceptance criteria;
* optionally `doctor_checks`: the domain's own offline prerequisite checks for `doctor`;

plus `harness_path` (the evolvable directory), `SKILL.md` and `PATTERNS.md`
(the proposer's constitution) and `rrsi.json` (hyperparameters). The core
never reads a trajectory format or a benchmark directory itself.

The adapter imports its sibling modules package-qualified, for example
`from domains.<name> import briefs, render`, so two domains loaded in one
process never share a module. `doctor` runs `validate_domain` on the adapter
(required methods overridden, the four briefs, the constitution files) without
loading a task set; it does not yet check the split rules, result cardinality
or renderer behavior.

## 🧪 Tests

```bash
python3 -m pytest -q               # or: python3 tests/test_core.py
```

The suite is offline: it makes no model call and needs no `anthropic` package, credentials, Docker or benchmark checkout. CI (`.github/workflows/tests.yml`) runs it on Python 3.10, 3.11 and 3.12 with only `pytest` installed.

## 🙏 Acknowledgements

The starting harnesses are the Terminus-2 agent from [harbor](https://github.com/laude-institute/harbor) and the react_toolbelt agent and runner from [archipelago](https://github.com/Mercor-Intelligence/archipelago). The instances evaluate on [Terminal-Bench](https://github.com/harbor-framework/terminal-bench), [SWE-bench Verified](https://github.com/SWE-bench/SWE-bench), [Harvey LAB](https://github.com/harveyai/harvey-labs), [JobBench](https://github.com/Job-Bench/job-bench-eval), [GDPval](https://openai.com/index/gdpval/), [APEX-Agents](https://www.mercor.com/apex/apex-agents-leaderboard/), [EngDesign](https://github.com/AGI4Engineering/EngDesign) and [Frontier-Eng](https://github.com/Einsia/Frontier-Engineering).

## 💬 Citation

```bibtex
@article{xia2026rrsi,
  title={RRSI: Regularized Recursive Self-Improvement of Agent Harnesses},
  author={Xia, Peng and Han, Rujun and Wang, Zifeng and Chen, Yanfei and Zhuang, Yufan and Lee, Yoonho and Huang, Chengsong and Yu, Han and CuiZhu, Zhongying and Ming, Yifei and Yao, Huaxiu and Gokturk, Burak and Pfister, Tomas and Lee, Chen-Yu},
  journal={arXiv preprint arXiv:2609.24972},
  year={2026}
}
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache 2.0; see [LICENSE](LICENSE). Third-party code under `third_party/` carries its own license.

## Disclaimer

This is not an officially supported Google product. This project is not eligible for the [Google Open Source Software Vulnerability Rewards Program](https://bughunters.google.com/open-source-security).
