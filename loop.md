# Evolve Loop V3: O decides, code executes

This file is loaded after Init (SKILL.md) completes. O reads this during the autonomous loop.

Designed for use with `/loop 1m /evolve` -- each round is a new session that recovers context from files.

---

## Prerequisites

Each time `/evolve` is triggered and `.evolve/results.tsv` exists, enter this loop.

### 0. Concurrency Lock

O acquires the lock at the start of each round:

```python
import sys
sys.path.insert(0, '.claude/skills/evolve')
from prepare import (
    acquire_lock, update_lock, release_lock,
    scan_all_features,
    acquire_build_lock, release_build_lock,
    acquire_feature_lock, release_feature_lock,
    prepare_dispatch,
)

lock = acquire_lock(".evolve")
if not lock["acquired"]:
    # Another session running — show progress, then stop
    -> Progress Report (see below), then stop immediately
```

Call `update_lock(".evolve", phase, feature)` at every major step.
Call `release_lock(".evolve")` when done.
Lock auto-expires after 2 minutes if session crashes.

**Per-feature locks** (used by O when dispatching B/C):
- `acquire_feature_lock(".evolve", "F01", "B")` — acquires build_lock + feature lock (B exclusive)
- `acquire_feature_lock(".evolve", "F02", "C")` — acquires feature lock only (C parallel)
- `release_feature_lock(".evolve", "F01", "B")` — releases feature lock + build_lock
- `release_feature_lock(".evolve", "F02", "C")` — releases feature lock only

---

## O's Dispatch Flow

O runs a **parallel pipeline** each round. Multiple agents can run concurrently as long as concurrency rules are respected (see Concurrency Rules below).

### 1. Scan feature states

```python
features = scan_all_features(".evolve")
# Returns: [{"name": "F01", "state": "needs_eval"|"needs_build"|"completed"|"not_started",
#            "in_progress": None|"B"|"C", "last_status": ..., "eval_count": ...,
#            "consecutive_fails": ...}]
```

### 2. Dispatch H (Haiku) for prep — parallel

O spawns H agents for every feature that needs prep. Multiple H agents can run simultaneously.

```python
# Identify features that need prep
needs_prep = [f for f in features if f["state"] in ("needs_build", "needs_eval", "not_started")
              and f["in_progress"] is None]

for feat in needs_prep:
    Agent(prompt=f"You are H. Prep context for {feat['name']}. Read agents/helper.md for instructions.",
          model="haiku", run_in_background=True)
```

H does:
1. Reads raw files → writes `.evolve/manifest.md` (structured status + summary, includes Feature States section)
2. Analyzes feature dependencies (which sections does the current feature need?)
3. Locates exact line ranges in large documents (grep for headings)
4. Calls `prepare_dispatch()` with `feature=` parameter to assemble per-feature dispatch files

H uses file specs to scope context precisely:
```python
from prepare import prepare_dispatch

# Example: F07 needs its own section + F02 Card specs as reference
prepare_dispatch(".evolve", "B", [
    "program.md",
    "{feature}/strategy.md",
    "product-experience-design.md#F07 Pattern Mirror",    # only F07 section
    "product-experience-design.md#F02 Canvas",             # only F02 Canvas section
], note="F07 references F02 Canvas Card C/D for layout", feature="F07 Pattern Mirror")
# Writes to: .evolve/F07 Pattern Mirror/dispatch_B.md
```

Supported file spec formats:
- `"file.md"` — full file
- `"file.md:100-200"` — lines 100-200 only
- `"file.md#Section Name"` — heading-based section extraction
- `"file.md:42"` — single line

### 3. O reads manifest, decides parallel dispatch

After H agents finish, O reads `.evolve/manifest.md` (one file, enough to decide):

- `should_stop: yes` → stop immediately, report to user
- Read Feature States section to determine what to dispatch

O does NOT re-read original files. H already prepped everything.

**Dispatch decision logic:**

```python
features = scan_all_features(".evolve")

eval_ready  = [f for f in features if f["state"] == "needs_eval" and f["in_progress"] is None]
build_ready = [f for f in features if f["state"] == "needs_build" and f["in_progress"] is None]
start_ready = [f for f in features if f["state"] == "not_started" and f["in_progress"] is None]
# start_ready: filter further by dependency — only those whose dependencies are "completed"
```

### 3.5. Dispatch M (Mentor) — parallel, via Agent tool (Opus 4.6)

Before dispatching B/C for a feature, check if the feature is **stuck**.
If consecutive_fails >= 3 AND prior advice count < 3, O invokes M (Claude
Opus 4.6) to read the full history and write advice for B and C.

M is dispatched via the **Agent tool** (not codex exec) because:
- Opus gives a different-model second opinion (B/C are both gpt-5.4-high).
- M is pure consultant — writes markdown, does not edit code.
- Agent tool is one-shot and well-bounded for the diagnostic work.

```python
from adapter import (
    should_invoke_mentor, build_dispatch_M, count_mentor_advice,
)

for feat in features:
    invoke, reason = should_invoke_mentor(feat["name"])
    if invoke:
        prompt = build_dispatch_M(feat["name"], round_n=feat.get("eval_count", 0))
        Agent(
            description=f"Mentor advice for {feat['name']}",
            subagent_type="general-purpose",
            model="opus",
            prompt=prompt,
            run_in_background=True,
        )
    elif reason.startswith("blocker"):
        # 3 advices already and still failing — mark BLOCKER, skip this feature
        # for the rest of the run.  Write to strategy.md and results.tsv.
        # (BLOCKER is a permanent skip — no more B/C/M dispatch for this feature.)
        mark_feature_blocker(feat["name"], reason)
```

After the Agent returns, it will have written
`.evolve/features/{feat}/mentor_advice_{n}.md`.  The next round's
`build_dispatch_B` and `build_dispatch_C` automatically prepend this file
so B and C see the advice.  O does not need to thread anything manually.

**Mentor concurrency:** multiple M agents can run in parallel across
different features (each writes to its own feature dir), but a feature
should not have two M agents in flight simultaneously — use feature locks.

### 4. Dispatch C — parallel via codex CLI

All features that need eval can be dispatched simultaneously.  C is invoked
as a **codex exec subprocess** (gpt-5.4-high by default), not as a Claude
subagent — one fewer wrapper layer, and `dispatch_C.md` is already a
self-contained prompt.

```python
from adapter import dispatch_codex_agent

for feat in eval_ready:
    acquire_feature_lock(".evolve", feat["name"], "C")
    # dispatch_codex_agent rebuilds dispatch_C.md then runs:
    #   codex exec --model gpt-5.4-high -s workspace-write --skip-git-repo-check
    # Output streamed to .evolve/features/{feat}/dispatch_C_codex.log
    result = dispatch_codex_agent(feat["name"], "C", round_n=...)
    # result["status"] ∈ {"ok", "crash", "timeout", "not_found"}
    release_feature_lock(".evolve", feat["name"], "C")
```

To parallelize across features, wrap `dispatch_codex_agent()` in
`concurrent.futures.ThreadPoolExecutor` — each codex process is fully
independent and C is expected to only touch its own feature's strategy.md +
the shared append-only results.tsv.

Override the model / sandbox / timeout via env vars if needed:
- `EVOLVE_CODEX_MODEL` (default `gpt-5.4-high`)
- `EVOLVE_CODEX_SANDBOX` (default `workspace-write`; set to `danger-full-access` only if absolutely required)
- `EVOLVE_DISPATCH_C_TIMEOUT` (default 1200s / 20 min)

### 5. Dispatch B — exclusive via codex CLI

Only one B agent at a time (git constraint).  Pick the first needs_build
feature with build_lock free.  Same codex exec pattern as C.

```python
from adapter import dispatch_codex_agent

if build_ready:
    feat = build_ready[0]
    lock_ok = acquire_feature_lock(".evolve", feat["name"], "B")
    if lock_ok:
        result = dispatch_codex_agent(
            feat["name"], "B",
            mode="prep" if feat["state"] == "not_started" else "fix",
            round_n=...,
        )
        release_feature_lock(".evolve", feat["name"], "B")
```

Override timeout via `EVOLVE_DISPATCH_B_TIMEOUT` (default 1800s / 30 min).

**Pipeline overlap is allowed:** B(F02) and C(F01) can run concurrently
because each is its own codex process — they do not share state beyond the
append-only results.tsv.

### 6. Dispatch H for not_started features — parallel

Features whose dependencies are all completed can be prepped by H.

```python
for feat in start_ready:
    Agent(prompt=f"You are H. Prep context for {feat['name']}. Read agents/helper.md for instructions.",
          model="haiku", run_in_background=True)
```

### 7. After subagent completes

- B agent: `release_feature_lock(".evolve", feat, "B")` (releases build_lock + feature lock)
- C agent: `release_feature_lock(".evolve", feat, "C")` (releases feature lock only)
- Next round's Hook will update manifest.md automatically. O just reads it again.

---

## Build Flow (B Agent)

O dispatches B subagent. B reads `.evolve/{feature}/dispatch_B.md`.

### Feature Selection

The feature is determined by O's dispatch logic (see O's Dispatch Flow above). The dispatch file specifies the target feature.

### New Feature (after eval/pass)

B starts fresh on the next feature.

### Fix Round (after eval/fail)

B reads `.evolve/{feature}/strategy.md` which C already updated with the strategic decision:
- Continue → keep current approach, fix specific issues
- Pivot → new technical approach described in strategy.md
- Rollback → revert to specified commit, restart
- Re-execute → redo following strategy.md more closely
- Decompose → only implement first sub-task from strategy.md
- Consolidate → clean up dead code / stale comments / contradictions (no new features)

### Coding Rules

**Output Isolation:**

```bash
# Redirect all build/test commands to run.log (append, not overwrite)
npm run build >> .evolve/run.log 2>&1
python -m pytest >> .evolve/run.log 2>&1
```

- On crash: `tail -n 50 .evolve/run.log` to diagnose
- Do not pipe raw long output into agent context

**Coding Flow:**

1. Implement feature / fix
2. `git add` + `git commit` (one commit per B run — finer rollback granularity)
3. Append to results.tsv

**Simplicity Principle:**

- No new dependencies by default (unless program.md allows)
- When equally effective, choose the simpler implementation
- Deleting code without affecting results = good outcome

**Success Record:**

```python
from prepare import append_result
append_result(".evolve/results.tsv", {
    "commit": "<hash>", "phase": "build", "feature": "<name>",
    "scores": "-", "total": "-", "status": "keep",
    "summary": "implemented <brief>"
})
```

**Crash Record:**

```python
append_result(".evolve/results.tsv", {
    "commit": "<hash>", "phase": "build", "feature": "<name>",
    "scores": "-", "total": "0", "status": "crash",
    "summary": "<error>"
})
```

---

## Eval Flow (C Agent)

O dispatches C subagent. C reads `.evolve/{feature}/dispatch_C.md`.

### Environment Setup

```python
env = adapter.setup(project_dir)
if env["status"] == "crash":
    append_result(..., status="crash", summary=f"setup failed: {env['error']}")
    -> return to Build Flow
```

### Deterministic Scoring

```python
check_result = adapter.run_checks(project_dir, feature)
deterministic_scores = check_result["scores"]
```

### Independent Evaluator (MANDATORY)

`{feature}/dispatch_C.md` contains a pre-assembled `## Evaluator Prompt` section (built by H). C should use it directly as codex/claude CLI input instead of re-extracting from source files.

C must call an independent evaluator. Enforced by prepare.py:

```python
from prepare import get_evaluator, validate_eval_result

evaluator = get_evaluator()  # returns "agent", "codex", "claude", or None
if evaluator is None:
    # Cannot proceed without independent evaluator
    -> stop loop, report to user
```

Invoke the evaluator CLI to score `type: llm-judged` dimensions.

**Evaluator-specific flags:**
- `agent` (Cursor CLI): use `agent -p --trust --model gpt-5.4-high "<prompt>"` (override via `EVOLVE_AGENT_MODEL` env var)
- `codex`: do NOT specify `--model` — let it use the user's configured default
- `claude`: no special flags needed

Eval output written to `.evolve/{feature}/eval_{evaluator}.md`.

### Score Aggregation

```python
final_scores = {}
for dim in dimensions:
    name = dim["name"]
    if dim["type"] == "deterministic":
        final_scores[name] = deterministic_scores.get(name, 0)
    else:
        final_scores[name] = llm_scores.get(name, 0)

# Any dimension below threshold -> fail
status = "pass"
for dim in dimensions:
    if final_scores.get(dim["name"], 0) < dim["threshold"]:
        status = "fail"
```

### Trajectory Analysis + Strategic Decision

```python
from prepare import analyze_trajectory

trajectory = analyze_trajectory(".evolve/results.tsv", feature)
# Returns: {"trend": "rising"|"flat"|"falling"|"insufficient", ...}
```

C reads trajectory + `{feature}/strategy.md` + eval results, then picks one action from the 6-option menu (see agents/critic.md).

Writes updated `.evolve/{feature}/strategy.md`.

### Record Result

```python
scores_str = "/".join(str(final_scores.get(d["name"], "-")) for d in dimensions)
total = round(sum(final_scores.values()) / len(final_scores), 1) if final_scores else 0

append_result(".evolve/results.tsv", {
    "commit": "<hash>", "phase": "eval", "feature": "<name>",
    "scores": scores_str, "total": str(total),
    "status": status,
    "summary": "all pass" if status == "pass" else "<dimension> below threshold"
})
```

### Cleanup + Update Report

```python
adapter.teardown(env.get("info", {}))

from prepare import generate_report
report = generate_report(".evolve/results.tsv")
Path(".evolve/report.md").write_text(report)
```

---

## Done Flow

All features pass, or should_stop() halts the loop:

```python
report = generate_report(".evolve/results.tsv")
release_lock(".evolve")
```

Output report to user, stop the loop.

---

## Progress Report (EVERY round, ALWAYS)

**No matter what happened this round** — acquired lock or not, did work or not, build or eval — ALWAYS end with a brief progress summary. The user sees this every minute.

### Format

```
📍 Round N | Feature: <name> | Phase: <build|eval> | Status: <what just happened>
   Passed: M/T features | Latest score: X.X | Details: .evolve/report.md
```

### Rules

1. **One-liner first** — the `📍` line above. This is what the user scans.
2. **File link** — always include `.evolve/report.md` path so user can click for details.
3. **Update report.md** — call `generate_report()` and write to `.evolve/report.md` before outputting the summary.
4. **Lock-not-acquired case** — read manifest.md (read-only), show progress, note another session is running.
5. **Keep it short** — 1-2 lines max. No lengthy explanations.

---

## Concurrency Rules

1. **B exclusive**: Only one B agent at a time (enforced by build_lock)
2. **C parallel**: Multiple C agents can run simultaneously on different features
3. **H parallel**: Multiple H agents can prep simultaneously
4. **Pipeline overlap**: B(F02) + C(F01) can run concurrently
5. **State isolation**: Each feature has its own `.evolve/{feature}/` subdirectory
6. **Shared files**: results.tsv, run.log, program.md, eval.yml are shared (append-only or read-only)

---

## Agent Rules

1. **Do not modify program.md** -- contract between human and agent
2. **Do not modify files under .claude/skills/evolve/** -- evaluation infrastructure is immutable
3. **Do not install new packages** -- unless program.md allows
3. **Git commit per B/C run** -- finer rollback granularity
4. **results.tsv is append-only**
5. **run.log is append-only** -- with timestamp separators
6. **Simplicity first** -- when equally effective, choose simpler implementation
7. **Never stop** -- until all features pass or should_stop() halts. Do not ask "should I continue?"
8. **Can spawn subagents** -- via Agent tool for parallel independent subtasks

---

## File Permission Matrix

**Shared files** (in `.evolve/` root):

| File | O | H | B | C |
|------|---|---|---|---|
| program.md | read-only | read | read-only | read-only |
| eval.yml | read-only | read | read-only | read-only |
| spec.md | read-only | read | read-only | read-only |
| adapter.py | read-only | - | read-only | read-only |
| results.tsv | read | read | append | append |
| run.log | - | read (tail) | append | append |
| manifest.md | read | write | - | - |
| Project code | - | read-only | read/write | read-only |

**Per-feature files** (in `.evolve/{feature}/`):

| File | O | H | B | C |
|------|---|---|---|---|
| strategy.md | - | read | read-only | read/write |
| dispatch_B.md | - | write (via code) | read | - |
| dispatch_C.md | - | write (via code) | - | read |
| eval_codex.md | - | - | - | write |
