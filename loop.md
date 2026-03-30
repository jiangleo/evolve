# Evolve Loop V3: O decides, code executes

This file is loaded after Init (SKILL.md) completes. O reads this during the autonomous loop.

Designed for use with `/loop 1m /evolve` -- each round is a new session that recovers context from files.

---

## Prerequisites

Each time `/evolve` is triggered and `.evolve/results.tsv` exists, enter this loop.

### 0. Concurrency Lock

Hook already acquired the lock via `acquire_lock()`. If another session is running, Hook still updates manifest but O should check lock state.

Call `update_lock(".evolve", phase, feature)` at every major step.
Call `release_lock(".evolve")` when done.
Lock auto-expires after 2 minutes if session crashes.

---

## O's Dispatch Flow

### 1. Dispatch H (Haiku) for prep

O's first action each round: spawn H to do all prep work.

```python
# O spawns H with Haiku model — cheap, fast
Agent(prompt="You are H. Prep this round's context. Read agents/helper.md for instructions.",
      model="haiku")
```

H does:
1. Reads raw files → writes `.evolve/manifest.md` (structured status + summary)
2. Analyzes feature dependencies (which sections does the current feature need?)
3. Locates exact line ranges in large documents (grep for headings)
4. Calls `prepare_dispatch()` to assemble `dispatch_B.md` or `dispatch_C.md`

H uses file specs to scope context precisely:
```python
from prepare import prepare_dispatch

# Example: F07 needs its own section + F02 Card specs as reference
prepare_dispatch(".evolve", "B", [
    "program.md",
    "strategy.md",
    "product-experience-design.md#F07 Pattern Mirror",    # only F07 section
    "product-experience-design.md#F02 Canvas:953-969",    # only Card specs
], note="F07 references F02 Canvas Card C/D for layout")
```

Supported file spec formats:
- `"file.md"` — full file
- `"file.md:100-200"` — lines 100-200 only
- `"file.md#Section Name"` — heading-based section extraction
- `"file.md:42"` — single line

### 2. O reads manifest, decides

After H finishes, O reads `.evolve/manifest.md` (one file, enough to decide):

- `should_stop: yes` → stop immediately, report to user
- Confirm H's dispatch prep looks right
- Dispatch B or C

O does NOT re-read original files. H already prepped everything.

### 3. Dispatch B or C

```python
Agent(prompt="You are B. Read .evolve/dispatch_B.md for your full context and instructions.")
```

B/C reads the dispatch file, then works independently. If B/C needs more context (run.log, git log, source files), it reads them itself — it has full tool access.

### 4. After subagent completes

Next round's Hook will update manifest.md automatically. O just reads it again.

---

## Build Flow (B Agent)

O dispatches B subagent. B reads dispatch_B.md.

### Feature Selection

Read `.evolve/spec.md`, find the first feature not in completed list, in order.

### New Feature (after eval/pass)

B starts fresh on the next feature.

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

O dispatches C subagent. C reads dispatch_C.md.

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
**Do NOT specify `--model` when calling codex** — let it use the user's configured default.
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

| File | O | H | B | C |
|------|---|---|---|---|
| program.md | read-only | read | read-only | read-only |
| eval.yml | read-only | read | read-only | read-only |
| adapter.py | read-only | - | read-only | read-only |
| strategy.md | - | read | read-only | read/write |
| results.tsv | read | read | append | append |
| run.log | - | read (tail) | append | append |
| manifest.md | read | write | - | - |
| dispatch_*.md | - | write (via code) | read | read |
| Project code | - | read-only | read/write | read-only |
