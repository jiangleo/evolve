# Evolve Loop V2: O → B → C → B → C → ... → Done

This file is loaded after Init (SKILL.md) completes. The Agent reads this file during the autonomous loop.

Designed for use with `/loop 1m /evolve` -- each round is a new session that recovers context from files.

---

## Prerequisites

Each time `/evolve` is triggered and `.evolve/results.tsv` exists, enter this loop.

### 0. Concurrency Lock

```python
import sys
sys.path.insert(0, '.claude/skills/evolve')
from prepare import acquire_lock, update_lock, release_lock

lock = acquire_lock(".evolve")
if not lock["acquired"]:
    print(f"Waiting: {lock['reason']}")
    -> stop immediately
```

Call `update_lock(".evolve", phase, feature)` at every major step.
Call `release_lock(".evolve")` when done.
Lock auto-expires after 2 minutes if session crashes.

### 1. should_stop() Gate

**Before any AI work**, check code-enforced stop conditions:

```python
from prepare import read_progress, should_stop

progress = read_progress(".evolve/results.tsv")
feature = progress.get("current_feature") or "<first unfinished>"

stop, reason = should_stop(".evolve/results.tsv", feature)
if stop:
    print(f"⛔ {feature} stopped by code after {progress['total_iterations']} rounds")
    print(f"   Reason: {reason}")
    print(f"   Action needed: adjust program.md / lower threshold / provide hints")
    print(f"   Re-run /evolve to continue.")
    release_lock(".evolve")
    -> stop immediately
```

AI does not participate in this decision.

---

## Per-Round Reading List

New session -- before making any decisions, **read in order**:

| # | File | How | Purpose |
|---|------|-----|---------|
| 1 | `.evolve/program.md` | Full text | User goals, constraints, V2 design reference |
| 2 | `.evolve/spec.md` | Full text | Feature list and acceptance criteria |
| 3 | `.evolve/eval.yml` | Full text | Evaluation dimensions and thresholds |
| 4 | `.evolve/results.tsv` | Last 10 lines | Current progress |
| 5 | `.evolve/strategy.md` | Full text (if exists) | C's previous strategic decisions |
| 6 | git log --oneline -3 | Command | What changed recently |

```python
from prepare import read_progress, load_eval_config, load_adapter

progress = read_progress(".evolve/results.tsv")
dimensions = load_eval_config(".evolve/eval.yml")
adapter = load_adapter(".evolve/adapter.py")
```

---

## State Machine (V2)

```
build/keep       → dispatch C (evaluate the build)
build/crash      → dispatch B (fix)
eval/pass        → dispatch B (next feature; C resets strategy.md)
eval/fail        → dispatch B (C already updated strategy.md with decision)
All features pass → Done
```

No contract phase. No skip status. Stuck = code stops the loop via should_stop().

### Routing

`read_progress()` returns the NEXT phase to execute:

| Last results.tsv row | read_progress phase | Dispatch |
|---------------------|---------------------|----------|
| build/keep | eval | C (evaluate the build) |
| build/crash | build | B (fix the crash) |
| eval/pass | build | B (next feature) |
| eval/fail | build | B (C already wrote strategy.md) |

```python
if progress["phase"] == "build":
    # Check if all features completed
    # Read spec.md to extract spec_features
    # if all spec_features in progress["completed_features"] -> Done Flow
    -> Build Flow (dispatch B)

elif progress["phase"] == "eval":
    -> Eval Flow (dispatch C)
```

---

## Build Flow (B Agent)

O dispatches B subagent. B reads program.md + strategy.md.

### Feature Selection

Read `.evolve/spec.md`, find the first feature not in `progress["completed_features"]`, in order.

### New Feature (after eval/pass)

B starts fresh on the next feature. No sprint contract needed (V2 removed contracts).

### Fix Round (after eval/fail)

B reads `.evolve/strategy.md` which C already updated with the strategic decision:
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

O dispatches C subagent. C evaluates the build and makes strategic decisions.

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

C must call an independent evaluator. Enforced by prepare.py:

```python
from prepare import get_evaluator, validate_eval_result

evaluator = get_evaluator()  # returns "codex", "claude", or None
if evaluator is None:
    # Cannot proceed without independent evaluator
    -> stop loop, report to user
```

Invoke the evaluator CLI to score `type: llm-judged` dimensions.
Eval output written to `.evolve/eval_codex.md` (or `.evolve/eval_claude.md`).

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

C reads trajectory + strategy.md + eval results, then picks one action from the 6-option menu (see agents/critic.md).

Writes updated `.evolve/strategy.md`.

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

## File Permission Matrix (V2)

| File | O | B | C |
|------|---|---|---|
| program.md | read-only | read-only | read-only |
| eval.yml | read-only | read-only | read-only |
| adapter.py | read-only | read-only | read-only |
| strategy.md | - | read-only | read/write |
| results.tsv | read | append | append |
| run.log | append | append | append |
| Project code | - | read/write | read-only |
