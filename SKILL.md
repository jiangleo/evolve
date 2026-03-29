---
name: evolve
description: Define a goal. AI builds, evaluates, and iterates until it's met. Use with /loop 1m /evolve for continuous autonomous operation.
triggers:
  - /evolve
---

# Evolve: Define Goal -> Auto Build -> Auto Evaluate -> Iterate Until Done

## Overview

Three-phase autonomous loop: Init (interactive) -> Build -> Eval (auto loop).

- **Init**: This file. User participates, guided configuration.
- **Loop**: `loop.md`. AI runs automatically, user not involved (driven by `/loop 1m /evolve`).

Hard dependencies: Python 3.8+, Git. Everything else is declared by the project adapter.

---

## Trigger Routing

```
/evolve triggered
    |
Check if .evolve/ exists?
    |-- exists -> Resume logic (see table below)
    +-- not exists -> First-time setup (start at Step 1)
```

### Resume Logic

| Detected State | Behavior |
|---------------|----------|
| `.evolve/` exists but no `adapter.py` | Start from Step 3 |
| `adapter.py` exists, no `program.md` | Start from Step 4 |
| `program.md` exists, no `spec.md` | Start from Step 5 (validation) |
| `spec.md` exists, `results.tsv` empty or missing | Prompt to start the loop |
| `results.tsv` has data | Show progress report, prompt to continue |

---

## Init Workflow

### Step 1 -- Project Scan (automatic, no interaction)

Scan language, framework, test framework, directory structure. Output brief summary:

> "Detected FastAPI + PostgreSQL project, has pytest, entry point at app/main.py"

### Step 2 -- Understand Goal (1-2 questions)

- "What do you want to build/improve? One sentence."
- If description is vague: "What are the core features? List them."

### Step 3 -- Research Eval Criteria + Generate Adapter (automatic)

1. Based on product type + tech stack, research industry evaluation methods
2. Read `.claude/skills/evolve/adapters/base.py` for interface definition
3. Read `.claude/skills/evolve/adapters/web_app.py` or `teaching.py` as reference implementation
4. Auto-generate project-specific `.evolve/adapter.py` + `.evolve/eval.yml`

Show evaluation dimensions for user confirmation:

```
Suggested evaluation dimensions:
1. Functional Completeness -- deterministic, npm test, threshold 7.0
2. Code Quality -- llm-judged, threshold 7.0
3. Data Consistency -- deterministic, integration tests, threshold 7.0

Want to adjust?
```

User confirms/adjusts -> write to `.evolve/eval.yml` and `.evolve/adapter.py`.

#### eval.yml Format

```yaml
dimensions:
  - name: Functional Completeness
    type: deterministic
    cmd: npm test
    threshold: 7.0
  - name: Code Quality
    type: llm-judged
    threshold: 7.0
```

#### adapter.py Interface

```python
prerequisites = [{"name": "node", "check": "node --version", ...}]

def setup(project_dir: str) -> dict:       # -> {"status": "ready"|"crash", ...}
def run_checks(project_dir, feature) -> dict:  # -> {"scores": {...}, "details": "..."}
def teardown(info: dict) -> None:
```

### Step 4 -- Guide program.md Creation (field-by-field interaction)

Based on previously collected info, pre-fill most fields. Confirm field by field:

```markdown
# Program

## Product Requirements
<!-- Collected from Step 2 -->

## Technical Constraints
- Stack: <!-- Pre-filled from Step 1 scan -->
- Dependency limits: Only use specified stack and existing dependencies
- Allowed new dependencies: <!-- User confirms -->
- No-go zones: <!-- User fills -->

## Agent Rules
- Do not modify program.md
- Do not modify files under .claude/skills/evolve/
- Do not install new packages (unless allowed above)
- One git commit per feature
- Redirect all process output to .evolve/run.log
- Never stop -- loop until all features pass/skip or human interrupts
```

Generate `.evolve/program.md`.

### Step 5 -- Validation (automatic)

#### Level 1: Structural Validation (blocking)

| Check | Rule | Failure Message |
|-------|------|----------------|
| Product requirements | At least 1 non-empty | "Product requirements are empty. Describe what you want to build." |
| Template placeholders | No `{{` or `[fill...]` | "program.md line N is still a placeholder" |
| adapter.py | Exists and importable | "adapter.py load failed: {error}" |
| eval.yml | At least 1 eval dimension | "No eval dimensions. Re-run /evolve" |

#### Level 2: Semantic Validation (warnings)

| Check | Rule | Warning |
|-------|------|---------|
| Requirement granularity | Single item > 200 chars | "Requirement N is too long, consider splitting" |
| Eval threshold | Within 1-10 range | "Threshold N is outside 1-10 range" |

#### Level 3: Environment Validation (info)

```python
import sys
sys.path.insert(0, '.claude/skills/evolve')
from prepare import load_adapter, load_eval_config

# Validate adapter
adapter = load_adapter(".evolve/adapter.py")

# Validate eval.yml
dims = load_eval_config(".evolve/eval.yml")

# Check adapter prerequisites
for prereq in adapter.prerequisites:
    # Run prereq["check"], failure -> prompt install command
```

Output:

```
OK Product requirements: 5 items
OK Eval dimensions: 3 (Functional Completeness, Code Quality, Data Consistency)
OK adapter: loadable
OK python3 -- 3.11.5
OK git -- 2.43.0
!! Git working tree has uncommitted changes (recommend committing first)

Validation passed. Enter Planner phase? (Y/n)
```

### Step 6 -- Choose Evaluator (one question)

"What should evaluate the output?"
- A. Codex (recommended, most objective as independent evaluator)
- B. Claude (separate instance)
- C. Other

Record choice in `.evolve/program.md` under `## Evaluation Method`.

### Step 7 -- Planner Generates spec.md (automatic)

1. Create git branch: `git checkout -b evolve/<tag>`
2. Read `.evolve/program.md` + `.evolve/eval.yml`
3. Generate `.evolve/spec.md` (feature list + acceptance criteria)
4. Create `.evolve/results.tsv` (header row)
5. Create `.evolve/run.log` (empty)
6. Add `.evolve/` to `.gitignore`

Show spec.md for user review. After confirmation:

```
Init complete. Run /loop 1m /evolve to start the autonomous loop.
```

---

## prepare.py Function Reference

```bash
python -c "import sys; sys.path.insert(0, '.claude/skills/evolve'); from prepare import <func>; ..."
```

| Function | Signature | Description |
|----------|-----------|-------------|
| `load_eval_config` | `(eval_yml_path) -> list[dict]` | Parse eval.yml, return dimension list |
| `load_adapter` | `(adapter_path) -> module` | Load adapter from file path |
| `append_result` | `(results_tsv, row) -> None` | Append one row to results.tsv |
| `read_progress` | `(results_tsv) -> dict` | Read current progress and state machine state |
| `generate_report` | `(results_tsv) -> str` | Generate structured progress report |
| `acquire_lock` | `(evolve_dir) -> dict` | Acquire concurrency lock |
| `update_lock` | `(evolve_dir, phase, feature) -> None` | Update heartbeat |
| `release_lock` | `(evolve_dir) -> None` | Release lock |
