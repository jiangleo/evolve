# Evolve Loop: Build -> Eval Autonomous Cycle

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

---

## Per-Round Reading List

New session -- before making any decisions, **read in order**:

| # | File | How | Purpose |
|---|------|-----|---------|
| 1 | `.evolve/program.md` | Full text | User strategy and constraints |
| 2 | `.evolve/spec.md` | Full text | Feature list and acceptance criteria |
| 3 | `.evolve/eval.yml` | Full text | Evaluation dimensions and thresholds |
| 4 | `.evolve/results.tsv` | Last 10 lines | Current progress |
| 5 | `.evolve/evaluation.md` | Full text (if exists) | Previous eval feedback |
| 6 | git log --oneline -3 | Command | What changed recently |

```python
from prepare import read_progress, load_eval_config, load_adapter

progress = read_progress(".evolve/results.tsv")
dimensions = load_eval_config(".evolve/eval.yml")
adapter = load_adapter(".evolve/adapter.py")
```

---

## State Machine Routing

```python
if progress["phase"] == "init":
    -> Init not complete, prompt to run /evolve

elif progress["total_iterations"] >= 100:
    -> Done Flow (global cap)

elif progress["phase"] == "build":
    # Check if all features are completed/skipped
    # Read spec.md to extract spec_features
    # done = set(progress["completed_features"] + progress["skipped_features"])
    # if set(spec_features) <= done -> Done Flow
    -> Build Flow

elif progress["phase"] == "eval":
    -> Eval Flow
```

### Last Row Mapping

```
No data rows              -> Init not complete
plan/keep                 -> Build (pick first feature from spec)
build/keep                -> Eval (current feature done, review it)
build/crash               -> Build (read run.log to fix; 3+ consecutive crashes -> skip)
contract/pass             -> Build (start coding)
contract/fail             -> Build (rewrite contract)
eval/pass                 -> Build (next unfinished feature)
eval/fail                 -> Build (read evaluation.md to fix; 3+ consecutive fails -> reset)
eval/skip                 -> Build (next unfinished feature)
All features pass/skip    -> Done
Global iterations >= 100  -> Done
```

---

## Build Flow

### Feature Selection

Read `.evolve/spec.md`, find the first feature not in `progress["completed_features"]` and not in `progress["skipped_features"]`, in order.

### New Feature Flow

1. Write Sprint Contract (`.evolve/sprint_contract.md`)
2. Quick review of contract
3. Record `contract/pass` or `contract/fail`
4. Start coding

### Fix Round (after eval/fail)

1. Read `.evolve/evaluation.md` fix priorities
2. Fix by priority
3. Code directly (reuse existing contract)

### Coding Rules

**Output Isolation:**

```bash
# Redirect all build/test commands to run.log
npm run build > .evolve/run.log 2>&1
python -m pytest > .evolve/run.log 2>&1
```

- On crash: `tail -n 50 .evolve/run.log` to diagnose
- Do not pipe raw long output into agent context

**Coding Flow:**

1. Implement feature
2. `git add` + `git commit` (one commit per feature)
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

### Failure Handling

```
consecutive_crashes >= 3
  -> skip, move to next feature

consecutive_fails <= 3
  -> read evaluation.md to fix -> re-Build

consecutive_fails > 3 and not has_been_reset
  -> git reset --hard <base_commit>
  -> record reset, allow 1 retry

consecutive_fails > 3 and has_been_reset
  -> skip (already retried and still failing)
```

---

## Eval Flow

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
# e.g. {"Test Pass Rate": 9.2}
```

### LLM Evaluation

Read `.evolve/program.md` evaluation method:
- **Codex**: invoke via codex CLI
- **Claude**: spawn independent Agent
- **Other**: per program.md configuration

Eval prompt is dynamically generated based on eval.yml dimensions. Only evaluates `type: llm-judged` dimensions.

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

# Judgment: any dimension below threshold -> fail
status = "pass"
for dim in dimensions:
    if final_scores.get(dim["name"], 0) < dim["threshold"]:
        status = "fail"
```

Write summary to `.evolve/evaluation.md`, including:
- Scores and thresholds per dimension
- PASS/FAIL conclusion
- Fix priorities (red/yellow/green)

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

All features pass/skip, or 100 iterations reached:

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
4. **One commit per feature**
5. **results.tsv is append-only**
6. **Redirect output to .evolve/run.log**
7. **Simplicity first** -- when equally effective, choose simpler implementation
8. **Never stop** -- until all features pass/skip or human interrupts. Do not ask "should I continue?"
9. **Can spawn subagents** -- via Agent tool for parallel independent subtasks (limited to different files)

---

## File Permission Matrix

| File | Human | Planner | Generator | Evaluator |
|------|-------|---------|-----------|-----------|
| program.md | read/write | read-only | read-only | read-only |
| eval.yml | read/write | read-only | read-only | read-only |
| adapter.py | read/write | read-only | read-only | read-only |
| spec.md | read | write | read-only | read-only |
| results.tsv | read | append | append | append |
| evaluation.md | read | - | read | write |
| run.log | read | - | write | write |
| Project code | read/write | - | read/write | read-only |
