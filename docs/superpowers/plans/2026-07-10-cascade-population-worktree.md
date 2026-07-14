# Cascade + Worktree Isolation + Population Branching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic verification cascade (fail-fast before LLM judging) with pairwise trajectory analysis, git-worktree isolation for parallel Builders with a merge-on-pass integration gate, and population branching for stuck features with a gated forced_pass.

**Architecture:** Three new modules in the skill root — `cascade.py`, `worktree.py`, `population.py` — re-exported by `prepare.py` so the documented `from prepare import ...` interface stays stable. Guarantees live in Python (`validate_eval_result`, `should_stop` pattern), not agent markdown. Spec: `docs/superpowers/specs/2026-07-10-cascade-population-worktree-design.md`.

**Tech Stack:** Python 3.8+ stdlib only (subprocess, csv, json, pathlib), git CLI, pytest. No new dependencies.

## Global Constraints

- Python 3.8+ compatible; stdlib only, no PyYAML (eval.yml parsing stays line-based like `load_eval_config`).
- prepare.py must not grow materially (it is 1,340 lines); new logic goes in the new modules.
- **Import convention:** the three new modules import `prepare` (and each other) ONLY lazily inside function bodies, never at module top level — `prepare.py` imports them at its END for re-export; top-level imports would be circular.
- results.tsv is append-only; old 7-column files must remain readable and must keep getting 7-column rows appended (header-adaptive writer).
- All tests go in `tests/`, run with `python -m pytest tests/ -v`, and must stay fast (tmp-dir git repos, no network, no real LLM calls).
- Conventional Commits; commit AND push after every task (project CLAUDE.md rule).
- Agent-facing docs are the contract: every behavior change must be reflected in loop.md / agents/*.md (Tasks 13–14).

---

### Task 1: `cascade.py` — parse the `cascade:` section of eval.yml

**Files:**
- Create: `cascade.py`
- Test: `tests/test_cascade.py`

**Interfaces:**
- Produces: `load_cascade_config(eval_yml_path: str) -> list[dict]` — each dict `{"name": str, "cmd": str, "timeout": int}`; `[]` when eval.yml has no `cascade:` section; raises `FileNotFoundError` if eval.yml missing, `ValueError` if a stage lacks `cmd`. Constant `DEFAULT_STAGE_TIMEOUT = 300`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cascade.py
import os, subprocess, tempfile, pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from cascade import load_cascade_config, DEFAULT_STAGE_TIMEOUT


def test_load_cascade_config_basic(tmp_path):
    yml = tmp_path / "eval.yml"
    yml.write_text(
        "dimensions:\n"
        "  - name: quality\n"
        "    type: llm-judged\n"
        "    threshold: 3.5\n"
        "cascade:\n"
        "  - name: build\n"
        "    cmd: npm run build\n"
        "    timeout: 120\n"
        "  - name: test\n"
        "    cmd: npx vitest run\n"
    )
    stages = load_cascade_config(str(yml))
    assert stages == [
        {"name": "build", "cmd": "npm run build", "timeout": 120},
        {"name": "test", "cmd": "npx vitest run", "timeout": DEFAULT_STAGE_TIMEOUT},
    ]


def test_load_cascade_config_absent_section(tmp_path):
    yml = tmp_path / "eval.yml"
    yml.write_text("dimensions:\n  - name: quality\n    threshold: 3.5\n")
    assert load_cascade_config(str(yml)) == []


def test_load_cascade_config_missing_file():
    with pytest.raises(FileNotFoundError):
        load_cascade_config("/nonexistent/eval.yml")


def test_load_cascade_config_stage_missing_cmd(tmp_path):
    yml = tmp_path / "eval.yml"
    yml.write_text("cascade:\n  - name: build\n")
    with pytest.raises(ValueError, match="missing cmd"):
        load_cascade_config(str(yml))


def test_load_cascade_config_ignores_dimensions_after(tmp_path):
    # cascade section ends when indentation returns to column 0
    yml = tmp_path / "eval.yml"
    yml.write_text(
        "cascade:\n"
        "  - name: lint\n"
        "    cmd: ruff check .\n"
        "dimensions:\n"
        "  - name: quality\n"
        "    cmd: should-not-leak\n"
    )
    stages = load_cascade_config(str(yml))
    assert len(stages) == 1
    assert stages[0]["cmd"] == "ruff check ."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cascade.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cascade'`

- [ ] **Step 3: Write the implementation**

```python
# cascade.py
"""
cascade.py -- Deterministic verification cascade for Evolve.

Runs cheap deterministic stages (build / lint / test / smoke) fail-fast
BEFORE any LLM judging. Generalizes the chat adapter's gate_fail: a
trivially broken build never reaches (or pays for) the judge.

Agent MUST NOT modify this file.

Import convention: this module never imports prepare at top level
(prepare re-exports it; a top-level import would be circular).
"""

import subprocess
from pathlib import Path

DEFAULT_STAGE_TIMEOUT = 300  # seconds


def load_cascade_config(eval_yml_path: str) -> list:
    """Parse the optional top-level `cascade:` section of eval.yml.

    Schema (line-based, same constrained style as load_eval_config):

        cascade:
          - name: build
            cmd: npm run build
            timeout: 300        # optional, default DEFAULT_STAGE_TIMEOUT

    Returns [] if the file has no cascade section (old projects unchanged).
    Raises FileNotFoundError if eval.yml missing.
    Raises ValueError if a stage has no cmd.
    """
    path = Path(eval_yml_path)
    if not path.exists():
        raise FileNotFoundError(f"eval.yml not found: {eval_yml_path}")

    stages = []
    in_cascade = False
    current = None

    for line in path.read_text().split("\n"):
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            if current:
                stages.append(current)
                current = None
            in_cascade = stripped.startswith("cascade:")
            continue
        if not in_cascade:
            continue
        if stripped.startswith("- name:"):
            if current:
                stages.append(current)
            current = {
                "name": stripped.split(":", 1)[1].strip(),
                "timeout": DEFAULT_STAGE_TIMEOUT,
            }
        elif current is not None and stripped.startswith("cmd:"):
            current["cmd"] = stripped.split(":", 1)[1].strip()
        elif current is not None and stripped.startswith("timeout:"):
            current["timeout"] = int(float(stripped.split(":", 1)[1].strip()))

    if current:
        stages.append(current)

    for stage in stages:
        if "cmd" not in stage:
            raise ValueError(f"cascade stage '{stage['name']}' missing cmd")

    return stages
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cascade.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit and push**

```bash
git add cascade.py tests/test_cascade.py
git commit -m "feat(cascade): parse optional cascade: section from eval.yml"
git push
```

---

### Task 2: `cascade.py` — `run_cascade()` fail-fast executor

**Files:**
- Modify: `cascade.py` (append)
- Test: `tests/test_cascade.py` (append)

**Interfaces:**
- Produces: `run_cascade(evolve_dir: str, feature: str, stages: list, cwd: str = ".", health_check=None) -> dict` returning `{"status": "passed"|"cascade_fail", "failed_stage": str|None, "stages_run": list[str], "output_tail": str}`. `health_check` is an optional zero-arg callable returning `(ok: bool, detail: str)` — the implicit stage 0 named `"health"`. On failure writes `.evolve/{feature}/cascade_fail.md`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cascade.py`:

```python
from cascade import run_cascade


def _stage(name, cmd, timeout=30):
    return {"name": name, "cmd": cmd, "timeout": timeout}


def test_run_cascade_all_pass(tmp_path):
    result = run_cascade(str(tmp_path), "F01",
                         [_stage("a", "true"), _stage("b", "true")])
    assert result["status"] == "passed"
    assert result["failed_stage"] is None
    assert result["stages_run"] == ["a", "b"]


def test_run_cascade_fail_fast(tmp_path):
    result = run_cascade(str(tmp_path), "F01",
                         [_stage("a", "true"),
                          _stage("b", "echo broken >&2; false"),
                          _stage("c", "true")])
    assert result["status"] == "cascade_fail"
    assert result["failed_stage"] == "b"
    assert result["stages_run"] == ["a", "b"]      # c never ran
    assert "broken" in result["output_tail"]
    report = tmp_path / "F01" / "cascade_fail.md"
    assert report.exists()
    assert "broken" in report.read_text()


def test_run_cascade_health_check_first(tmp_path):
    result = run_cascade(str(tmp_path), "F01", [_stage("a", "true")],
                         health_check=lambda: (False, "HTTP 500"))
    assert result["status"] == "cascade_fail"
    assert result["failed_stage"] == "health"
    assert result["stages_run"] == ["health"]
    assert "HTTP 500" in result["output_tail"]


def test_run_cascade_health_check_passes(tmp_path):
    result = run_cascade(str(tmp_path), "F01", [_stage("a", "true")],
                         health_check=lambda: (True, "200 OK"))
    assert result["status"] == "passed"
    assert result["stages_run"] == ["health", "a"]


def test_run_cascade_timeout(tmp_path):
    result = run_cascade(str(tmp_path), "F01",
                         [_stage("slow", "sleep 5", timeout=1)])
    assert result["status"] == "cascade_fail"
    assert result["failed_stage"] == "slow"
    assert "timeout" in result["output_tail"]


def test_run_cascade_empty_stages(tmp_path):
    result = run_cascade(str(tmp_path), "F01", [])
    assert result["status"] == "passed"
    assert result["stages_run"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cascade.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'run_cascade'`

- [ ] **Step 3: Write the implementation**

Append to `cascade.py`:

```python
def run_cascade(evolve_dir: str, feature: str, stages: list,
                cwd: str = ".", health_check=None) -> dict:
    """Run cascade stages in order, fail-fast.

    health_check: optional zero-arg callable -> (ok: bool, detail: str).
    It is the implicit stage 0 ("health") — codifies "verify the service
    responds before dispatching C" in code.

    Returns:
        {"status": "passed"|"cascade_fail",
         "failed_stage": str|None,
         "stages_run": [stage names in execution order],
         "output_tail": str}   # last 2000 chars of the failing output

    On failure writes .evolve/{feature}/cascade_fail.md.
    """
    stages_run = []

    if health_check is not None:
        stages_run.append("health")
        ok, detail = health_check()
        if not ok:
            return _cascade_fail(evolve_dir, feature, "health",
                                 detail, stages_run)

    for stage in stages:
        stages_run.append(stage["name"])
        timeout = stage.get("timeout", DEFAULT_STAGE_TIMEOUT)
        try:
            proc = subprocess.run(
                stage["cmd"], shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return _cascade_fail(evolve_dir, feature, stage["name"],
                                 f"timeout after {timeout}s", stages_run)
        if proc.returncode != 0:
            tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-2000:]
            return _cascade_fail(evolve_dir, feature, stage["name"],
                                 tail, stages_run)

    return {"status": "passed", "failed_stage": None,
            "stages_run": stages_run, "output_tail": ""}


def _cascade_fail(evolve_dir: str, feature: str, stage: str,
                  tail: str, stages_run: list) -> dict:
    """Write cascade_fail.md and build the failure result."""
    feat_dir = Path(evolve_dir) / feature
    feat_dir.mkdir(parents=True, exist_ok=True)
    (feat_dir / "cascade_fail.md").write_text(
        f"# Cascade Fail\n\n"
        f"failed_stage: {stage}\n"
        f"stages_run: {', '.join(stages_run)}\n\n"
        f"## Output tail\n\n```\n{tail}\n```\n"
    )
    return {"status": "cascade_fail", "failed_stage": stage,
            "stages_run": stages_run, "output_tail": tail}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cascade.py -v`
Expected: 11 PASS

- [ ] **Step 5: Commit and push**

```bash
git add cascade.py tests/test_cascade.py
git commit -m "feat(cascade): run_cascade fail-fast executor with implicit health stage"
git push
```

---

### Task 3: prepare.py — enforce cascade in `validate_eval_result`, new statuses, re-export

**Files:**
- Modify: `prepare.py` (`VALID_STATUSES` at ~line 24; `validate_eval_result` at ~line 328; re-export block at end of file)
- Test: `tests/test_prepare.py` (modify `test_validate_eval_result*`, add new tests)

**Interfaces:**
- Consumes: `load_cascade_config`, `run_cascade` from Task 1–2.
- Produces: `validate_eval_result(result)` now also requires `result["cascade"] in ("passed", "empty")` (`"empty"` = project declares no cascade section). `VALID_STATUSES` gains `"cascade_fail"` and `"forced"`. `from prepare import load_cascade_config, run_cascade` works.

- [ ] **Step 1: Update/write the failing tests**

In `tests/test_prepare.py`, find the existing `validate_eval_result` tests (search for `def test_validate_eval_result`) and replace/extend so the file contains:

```python
def test_validate_eval_result_ok():
    validate_eval_result({"independent_evaluator_used": True,
                          "cascade": "passed"})   # no raise


def test_validate_eval_result_empty_cascade_ok():
    validate_eval_result({"independent_evaluator_used": True,
                          "cascade": "empty"})    # no raise


def test_validate_eval_result_no_evaluator():
    with pytest.raises(ValueError, match="independent evaluator"):
        validate_eval_result({"independent_evaluator_used": False,
                              "cascade": "passed"})


def test_validate_eval_result_missing_cascade():
    with pytest.raises(ValueError, match="cascade"):
        validate_eval_result({"independent_evaluator_used": True})


def test_validate_eval_result_failed_cascade():
    with pytest.raises(ValueError, match="cascade"):
        validate_eval_result({"independent_evaluator_used": True,
                              "cascade": "cascade_fail"})


def test_new_statuses_registered():
    from prepare import VALID_STATUSES
    assert "cascade_fail" in VALID_STATUSES
    assert "forced" in VALID_STATUSES


def test_prepare_reexports_cascade():
    from prepare import load_cascade_config, run_cascade  # noqa: F401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_prepare.py -v -k "validate_eval or statuses or reexports"`
Expected: FAIL (`cascade` key not enforced, statuses missing, import error)

- [ ] **Step 3: Implement**

In `prepare.py`, change the constant (line ~24):

```python
VALID_STATUSES = {"keep", "pass", "fail", "crash", "reset",
                  "cascade_fail", "forced"}
```

Replace `validate_eval_result` (line ~328):

```python
def validate_eval_result(result: dict) -> None:
    """Validate an eval round result. Raises ValueError if invalid.

    Enforced invariants (AI cannot skip these):
    1. An independent evaluator was called.
    2. The deterministic cascade ran and passed ("passed"), or the project
       declares no cascade in eval.yml ("empty"). A cascade_fail round must
       be recorded with status=cascade_fail and never reaches the judge.
    """
    if not result.get("independent_evaluator_used"):
        raise ValueError("Eval invalid: no independent evaluator was called")
    if result.get("cascade") not in ("passed", "empty"):
        raise ValueError(
            "Eval invalid: deterministic cascade did not pass "
            "(expected result['cascade'] in {'passed', 'empty'})"
        )
```

At the very END of `prepare.py`, add the re-export block (grows in later tasks):

```python
# ---------------------------------------------------------------------------
# Re-exports (new modules; imported at end of file to avoid circular imports)
# ---------------------------------------------------------------------------

from cascade import load_cascade_config, run_cascade, DEFAULT_STAGE_TIMEOUT  # noqa: E402,F401
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS (existing validate tests were updated in Step 1)

- [ ] **Step 5: Commit and push**

```bash
git add prepare.py tests/test_prepare.py
git commit -m "feat(cascade): enforce cascade marker in validate_eval_result, add cascade_fail/forced statuses"
git push
```

---

### Task 4: prepare.py — optional 8th `pairwise` column in results.tsv

**Files:**
- Modify: `prepare.py` (`HEADER_FIELDS` line ~23; `append_result` line ~338)
- Test: `tests/test_prepare.py` (append)

**Interfaces:**
- Produces: `HEADER_FIELDS = [..., "pairwise"]` (8 fields). `append_result` is **header-adaptive**: new files get the 8-column header; appending to an existing file uses that file's own header (old 7-column files keep getting 7-column rows). Row dicts may include `"pairwise"` like `"log:better/ui:same/db:worse"`; absent → written as `-` (in 8-col files).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prepare.py`:

```python
def test_append_result_writes_pairwise_column(tmp_path):
    path = str(tmp_path / "results.tsv")
    append_result(path, {
        "commit": "abc", "phase": "eval", "feature": "F01",
        "scores": "7/8", "total": "7.5", "status": "fail",
        "summary": "below threshold", "pairwise": "log:better/ui:same",
    })
    lines = Path(path).read_text().strip().split("\n")
    assert lines[0].split("\t") == HEADER_FIELDS
    assert lines[0].split("\t")[-1] == "pairwise"
    assert lines[1].split("\t")[-1] == "log:better/ui:same"


def test_append_result_pairwise_defaults_to_dash(tmp_path):
    path = str(tmp_path / "results.tsv")
    append_result(path, {
        "commit": "abc", "phase": "build", "feature": "F01",
        "scores": "-", "total": "-", "status": "keep", "summary": "built",
    })
    lines = Path(path).read_text().strip().split("\n")
    assert lines[1].split("\t")[-1] == "-"


def test_append_result_respects_old_7col_header(tmp_path):
    # Old results.tsv (7 columns) keeps its shape — no pairwise appended
    old_header = ["commit", "phase", "feature", "scores", "total",
                  "status", "summary"]
    path = tmp_path / "results.tsv"
    path.write_text("\t".join(old_header) + "\n"
                    "abc\teval\tF01\t7/8\t7.5\tfail\told row\n")
    append_result(str(path), {
        "commit": "def", "phase": "eval", "feature": "F01",
        "scores": "8/8", "total": "8.0", "status": "pass",
        "summary": "ok", "pairwise": "log:better",
    })
    lines = path.read_text().strip().split("\n")
    assert len(lines[0].split("\t")) == 7
    assert len(lines[2].split("\t")) == 7          # pairwise dropped
    # and old files still parse
    progress = read_progress(str(path))
    assert "F01" in progress["completed_features"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_prepare.py -v -k pairwise_column or -k 7col`
Use: `python -m pytest tests/test_prepare.py -v -k "pairwise or 7col"`
Expected: FAIL (no pairwise in header)

- [ ] **Step 3: Implement**

In `prepare.py` change line ~23:

```python
HEADER_FIELDS = ["commit", "phase", "feature", "scores", "total",
                 "status", "summary", "pairwise"]
```

Replace `append_result`:

```python
def append_result(results_tsv: str, row: dict) -> None:
    """Append one row to results.tsv. Creates file with header if needed.

    Header-adaptive for backward compatibility: appending to an existing
    file uses THAT file's header (old 7-column files keep their shape);
    new files get the full HEADER_FIELDS including 'pairwise'.
    """
    path = Path(results_tsv)
    write_header = not path.exists() or path.stat().st_size == 0

    if write_header:
        fieldnames = HEADER_FIELDS
    else:
        with open(path, newline="") as f:
            first = f.readline().rstrip("\n")
        fieldnames = first.split("\t") if first else HEADER_FIELDS

    out = dict(row)
    if "pairwise" in fieldnames:
        out.setdefault("pairwise", "-")

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t",
                                extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(out)
```

Note: readers (`read_progress`, `analyze_trajectory`, `generate_report`, `scan_all_features`) all use `csv.DictReader`, which is header-driven — they need no change to *read* both shapes.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS. If any existing test asserts an exact 7-field header line, update it to use `HEADER_FIELDS` (the tests shown at `tests/test_prepare.py:17-31` already compare against `HEADER_FIELDS`, so they adapt automatically).

- [ ] **Step 5: Commit and push**

```bash
git add prepare.py tests/test_prepare.py
git commit -m "feat(pairwise): optional 8th pairwise column in results.tsv, header-adaptive writer"
git push
```

---

### Task 5: prepare.py — `analyze_trajectory` prefers pairwise, detects noise, skips cascade_fail

**Files:**
- Modify: `prepare.py` (`analyze_trajectory` lines 221–269)
- Test: `tests/test_prepare.py` (append)

**Interfaces:**
- Produces: `analyze_trajectory` may now return `"trend": "noisy"` in addition to the existing values. Rows with `status == "cascade_fail"` are excluded entirely. When every row in the window carries a parseable `pairwise` value, trend comes from the net pairwise verdicts; a sign contradiction between score delta and pairwise net → `"noisy"`. Helper `_pairwise_net(pw: str) -> int|None` (module-private).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prepare.py` (reuse the existing `_make_tsv` helper at `tests/test_prepare.py:65`, but these rows need 8 columns, so add a local helper):

```python
def _make_tsv8(rows):
    """Write an 8-column results.tsv into a temp file, return its path."""
    header = "\t".join(HEADER_FIELDS)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".tsv",
                                     delete=False) as f:
        f.write(header + "\n")
        for r in rows:
            f.write("\t".join(r) + "\n")
        return f.name


def test_analyze_trajectory_prefers_pairwise(tmp_path):
    # Scores flat (diff <= 0.5 would say "flat") but pairwise says better
    path = _make_tsv8([
        ["a", "eval", "F01", "7/7", "7.0", "fail", "r1", "log:same/ui:same"],
        ["b", "eval", "F01", "7/7", "7.1", "fail", "r2", "log:better/ui:same"],
        ["c", "eval", "F01", "7/7", "7.2", "fail", "r3", "log:better/ui:better"],
    ])
    try:
        t = analyze_trajectory(path, "F01")
        assert t["trend"] == "rising"
    finally:
        os.unlink(path)


def test_analyze_trajectory_contradiction_is_noisy(tmp_path):
    # Score jumped +1.5 but pairwise majority says worse -> judge noise
    path = _make_tsv8([
        ["a", "eval", "F01", "7/7", "7.0", "fail", "r1", "log:same/ui:same"],
        ["b", "eval", "F01", "7/7", "7.2", "fail", "r2", "log:worse/ui:same"],
        ["c", "eval", "F01", "9/8", "8.5", "fail", "r3", "log:worse/ui:worse"],
    ])
    try:
        t = analyze_trajectory(path, "F01")
        assert t["trend"] == "noisy"
    finally:
        os.unlink(path)


def test_analyze_trajectory_skips_cascade_fail_rows(tmp_path):
    path = _make_tsv8([
        ["a", "eval", "F01", "7/7", "7.0", "fail", "r1", "-"],
        ["b", "eval", "F01", "-", "0", "cascade_fail", "build broke", "-"],
        ["c", "eval", "F01", "7/8", "7.5", "fail", "r2", "-"],
        ["d", "eval", "F01", "8/8", "8.0", "fail", "r3", "-"],
    ])
    try:
        t = analyze_trajectory(path, "F01")
        assert 0.0 not in t["scores"]           # cascade_fail row excluded
        assert t["trend"] == "rising"           # 7.0 -> 8.0 over window
    finally:
        os.unlink(path)


def test_analyze_trajectory_no_pairwise_falls_back_to_scores(tmp_path):
    # Old 7-col behavior unchanged
    path = _make_tsv8([
        ["a", "eval", "F01", "7/7", "7.0", "fail", "r1", "-"],
        ["b", "eval", "F01", "7/7", "7.1", "fail", "r2", "-"],
        ["c", "eval", "F01", "7/7", "7.2", "fail", "r3", "-"],
    ])
    try:
        assert analyze_trajectory(path, "F01")["trend"] == "flat"
    finally:
        os.unlink(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_prepare.py -v -k "prefers_pairwise or noisy or skips_cascade or falls_back"`
Expected: `prefers_pairwise`, `noisy`, `skips_cascade` FAIL; `falls_back` passes already

- [ ] **Step 3: Implement**

In `prepare.py`, add above `analyze_trajectory`:

```python
def _pairwise_net(pw: str):
    """Parse 'log:better/ui:same/db:worse' -> net int (+1 per better,
    -1 per worse). Returns None if unparseable/absent."""
    if not pw or pw.strip() in ("-", ""):
        return None
    net, seen = 0, False
    for part in pw.split("/"):
        verdict = part.split(":")[-1].strip().lower()
        if verdict == "better":
            net += 1
            seen = True
        elif verdict == "worse":
            net -= 1
            seen = True
        elif verdict == "same":
            seen = True
    return net if seen else None
```

Replace the body of `analyze_trajectory` (keep the signature and docstring, extend the docstring's trend list with `"noisy"`):

```python
    path = Path(results_tsv)
    if not path.exists():
        return {"trend": "insufficient", "scores": [], "rounds": 0,
                "latest": 0.0}

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))

    entries = []   # (score, pairwise_net_or_None)
    for r in rows:
        if r.get("phase") != "eval" or r.get("feature") != feature:
            continue
        if r.get("status") == "cascade_fail":
            continue   # void round: broken build, not a real judgment
        try:
            score = float(r["total"])
        except (ValueError, TypeError, KeyError):
            continue
        entries.append((score, _pairwise_net(r.get("pairwise", ""))))

    scores = [e[0] for e in entries]
    if len(scores) < window:
        return {"trend": "insufficient", "scores": scores,
                "rounds": len(scores),
                "latest": scores[-1] if scores else 0.0}

    recent = entries[-window:]
    recent_scores = [e[0] for e in recent]
    diff = recent_scores[-1] - recent_scores[0]

    # Pairwise verdicts are round-vs-previous-round; the first window row's
    # verdict compares against a pre-window round, so use rounds 2..window.
    nets = [e[1] for e in recent[1:]]
    if all(n is not None for n in nets) and nets:
        pairwise_sum = sum(nets)
        contradiction = (diff > 0.5 and pairwise_sum < 0) or \
                        (diff < -0.5 and pairwise_sum > 0)
        if contradiction:
            trend = "noisy"
        elif pairwise_sum > 0:
            trend = "rising"
        elif pairwise_sum < 0:
            trend = "falling"
        else:
            trend = "flat"
    else:
        if diff > 0.5:
            trend = "rising"
        elif diff < -0.5:
            trend = "falling"
        else:
            trend = "flat"

    return {"trend": trend, "scores": recent_scores,
            "rounds": len(scores), "latest": recent_scores[-1]}
```

Note: `should_stop` (prepare.py:308) only acts on `trend == "flat"`; `"noisy"` deliberately contributes no stop/pivot signal.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 5: Commit and push**

```bash
git add prepare.py tests/test_prepare.py
git commit -m "feat(pairwise): trajectory prefers pairwise verdicts, noisy on contradiction, skips cascade_fail"
git push
```

---

### Task 5b: prepare.py — `prepare_dispatch` includes Previous Round Evidence for C

**Files:**
- Modify: `prepare.py` (`prepare_dispatch`, lines 806–863)
- Test: `tests/test_prepare.py` (append)

**Interfaces:**
- Produces: when `target == "C"` and `feature` is set and a previous eval output exists (`.evolve/{feature}/eval_codex.md` or `eval_agent.md` / `eval_claude.md`), the dispatch file gains a `## Previous Round Evidence` section containing that file's content plus the instruction to emit per-dimension pairwise verdicts. No previous eval → no section (round 1 has nothing to compare).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prepare.py`:

```python
def test_prepare_dispatch_c_includes_previous_evidence(tmp_path):
    evolve = tmp_path / ".evolve"
    feat_dir = evolve / "F01"
    feat_dir.mkdir(parents=True)
    (evolve / "program.md").write_text("# Program\ngoal\n")
    (feat_dir / "eval_codex.md").write_text("previous judge rationale here")

    path = prepare_dispatch(str(evolve), "C", ["program.md"], feature="F01")
    content = Path(path).read_text()
    assert "## Previous Round Evidence" in content
    assert "previous judge rationale here" in content
    assert "pairwise" in content            # instruction to emit verdicts


def test_prepare_dispatch_c_no_evidence_first_round(tmp_path):
    evolve = tmp_path / ".evolve"
    (evolve / "F01").mkdir(parents=True)
    (evolve / "program.md").write_text("# Program\ngoal\n")

    path = prepare_dispatch(str(evolve), "C", ["program.md"], feature="F01")
    assert "## Previous Round Evidence" not in Path(path).read_text()


def test_prepare_dispatch_b_never_gets_evidence(tmp_path):
    evolve = tmp_path / ".evolve"
    feat_dir = evolve / "F01"
    feat_dir.mkdir(parents=True)
    (evolve / "program.md").write_text("# Program\ngoal\n")
    (feat_dir / "eval_codex.md").write_text("judge output")

    path = prepare_dispatch(str(evolve), "B", ["program.md"], feature="F01")
    assert "## Previous Round Evidence" not in Path(path).read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_prepare.py -v -k previous_evidence or -k dispatch_c or -k dispatch_b`
Use: `python -m pytest tests/test_prepare.py -v -k "evidence or first_round"`
Expected: FAIL (section not generated)

- [ ] **Step 3: Implement**

In `prepare.py`, inside `prepare_dispatch`, insert right before the final `dispatch_path = ...` line (line ~861):

```python
    # C only: previous round's judge output enables pairwise verdicts.
    if target == "C" and feature:
        for eval_name in ("eval_codex.md", "eval_agent.md", "eval_claude.md"):
            prev_eval = evolve_path / feature / eval_name
            if prev_eval.exists():
                sections.append(
                    "## Previous Round Evidence\n"
                    "For EVERY dimension, judge this round against the "
                    "previous one below and emit `pairwise: "
                    "better|same|worse` per dimension (recorded in "
                    "results.tsv's pairwise column). Pass/fail stays on "
                    "absolute scores; pairwise feeds trajectory analysis.\n\n"
                    f"{prev_eval.read_text()}\n"
                )
                break
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 5: Commit and push**

```bash
git add prepare.py tests/test_prepare.py
git commit -m "feat(pairwise): dispatch_C carries previous round evidence for pairwise judging"
git push
```

---

### Task 6: `worktree.py` — create/remove feature worktrees

**Files:**
- Create: `worktree.py`
- Test: `tests/test_worktree.py`

**Interfaces:**
- Produces: `feature_slug(feature) -> str`; `base_branch(evolve_dir) -> str`; `feature_branch(evolve_dir, feature) -> str` (`"{base}--{slug}"` — sibling ref; git cannot create `X/Y` when branch `X` exists); `worktree_path(evolve_dir, feature) -> str` (`"{evolve_dir}/worktrees/{slug}"`); `create_feature_worktree(evolve_dir, feature, from_branch=None) -> {"path", "branch", "created"}`; `remove_feature_worktree(evolve_dir, feature, delete_branch=True)`. Assumption (documented): these are called from the main working tree, which sits on the `evolve/<tag>` branch.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_worktree.py
import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from worktree import (feature_slug, base_branch, feature_branch,
                      worktree_path, create_feature_worktree,
                      remove_feature_worktree)


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True,
                          capture_output=True, text=True)


def _git_repo(tmp_path):
    """Init a repo on branch evolve/demo with one commit and .evolve/."""
    _run(["git", "init", "-b", "evolve/demo", "."], tmp_path)
    _run(["git", "config", "user.email", "t@example.com"], tmp_path)
    _run(["git", "config", "user.name", "t"], tmp_path)
    (tmp_path / "app.txt").write_text("v1\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "init"], tmp_path)
    evolve = tmp_path / ".evolve"
    evolve.mkdir()
    return str(evolve)


def test_feature_slug_sanitizes():
    assert feature_slug("F01 Pattern Mirror") == "F01-Pattern-Mirror"
    assert feature_slug("a/b:c") == "a-b-c"


def test_branch_and_path_naming(tmp_path):
    evolve = _git_repo(tmp_path)
    assert base_branch(evolve) == "evolve/demo"
    assert feature_branch(evolve, "F01 auth") == "evolve/demo--F01-auth"
    assert worktree_path(evolve, "F01 auth").endswith(
        ".evolve/worktrees/F01-auth")


def test_create_and_remove_worktree(tmp_path):
    evolve = _git_repo(tmp_path)
    wt = create_feature_worktree(evolve, "F01 auth")
    assert wt["created"] is True
    assert Path(wt["path"], "app.txt").exists()
    # branch exists
    r = subprocess.run(["git", "rev-parse", "--verify",
                        "evolve/demo--F01-auth"],
                       cwd=tmp_path, capture_output=True)
    assert r.returncode == 0
    # idempotent
    again = create_feature_worktree(evolve, "F01 auth")
    assert again["created"] is False
    assert again["path"] == wt["path"]

    remove_feature_worktree(evolve, "F01 auth")
    assert not Path(wt["path"]).exists()
    r = subprocess.run(["git", "rev-parse", "--verify",
                        "evolve/demo--F01-auth"],
                       cwd=tmp_path, capture_output=True)
    assert r.returncode != 0            # branch deleted too


def test_create_from_explicit_branch(tmp_path):
    evolve = _git_repo(tmp_path)
    _run(["git", "branch", "other-base"], tmp_path)
    wt = create_feature_worktree(evolve, "F02", from_branch="other-base")
    assert wt["branch"] == "evolve/demo--F02"
    assert Path(wt["path"]).exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_worktree.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'worktree'`

- [ ] **Step 3: Write the implementation**

```python
# worktree.py
"""
worktree.py -- Git worktree isolation for parallel Builders.

Each feature's B works in .evolve/worktrees/{slug} on branch
{base}--{slug}, where {base} is the evolve/<tag> branch of the MAIN
working tree. All functions here must be called from the main working
tree, not from inside a worktree.

Agent MUST NOT modify this file.

Import convention: imports from prepare/cascade happen lazily inside
function bodies (prepare re-exports this module at its end).
"""

import re
import subprocess
from pathlib import Path


def _repo_root(evolve_dir: str) -> str:
    """The project repo root is the parent of .evolve/."""
    return str(Path(evolve_dir).resolve().parent)


def _git(args: list, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd,
                          capture_output=True, text=True)


def feature_slug(feature: str) -> str:
    """Sanitize a feature name into a branch/dir-safe slug."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", feature).strip("-.")
    return slug or "feature"


def base_branch(evolve_dir: str) -> str:
    """Current branch of the main working tree (the evolve/<tag> branch)."""
    proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], _repo_root(evolve_dir))
    if proc.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def feature_branch(evolve_dir: str, feature: str) -> str:
    return f"{base_branch(evolve_dir)}--{feature_slug(feature)}"


def worktree_path(evolve_dir: str, feature: str) -> str:
    return str(Path(evolve_dir) / "worktrees" / feature_slug(feature))


def create_feature_worktree(evolve_dir: str, feature: str,
                            from_branch: str = None) -> dict:
    """Create (or reuse) the worktree + branch for a feature.

    Returns {"path": str, "branch": str, "created": bool}.
    Raises RuntimeError on git failure.
    """
    root = _repo_root(evolve_dir)
    branch = feature_branch(evolve_dir, feature)
    path = worktree_path(evolve_dir, feature)

    if Path(path).exists():
        return {"path": path, "branch": branch, "created": False}

    Path(path).parent.mkdir(parents=True, exist_ok=True)

    if _git(["rev-parse", "--verify", branch], root).returncode != 0:
        start = from_branch or base_branch(evolve_dir)
        proc = _git(["branch", branch, start], root)
        if proc.returncode != 0:
            raise RuntimeError(f"git branch failed: {proc.stderr.strip()}")

    proc = _git(["worktree", "add", path, branch], root)
    if proc.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")

    return {"path": path, "branch": branch, "created": True}


def remove_feature_worktree(evolve_dir: str, feature: str,
                            delete_branch: bool = True) -> None:
    """Remove a feature's worktree (and branch). Idempotent."""
    root = _repo_root(evolve_dir)
    path = worktree_path(evolve_dir, feature)
    _git(["worktree", "remove", "--force", path], root)
    _git(["worktree", "prune"], root)
    if delete_branch:
        _git(["branch", "-D", feature_branch(evolve_dir, feature)], root)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_worktree.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit and push**

```bash
git add worktree.py tests/test_worktree.py
git commit -m "feat(worktree): per-feature git worktrees for parallel builders"
git push
```

---

### Task 7: `worktree.py` — `merge_feature()` with integration gate

**Files:**
- Modify: `worktree.py` (append)
- Test: `tests/test_worktree.py` (append)

**Interfaces:**
- Consumes: `acquire_build_lock`/`release_build_lock` from prepare (lazy import), `run_cascade` from cascade (lazy import).
- Produces: `merge_feature(evolve_dir, feature, cascade_stages=None, health_check=None, branch=None) -> {"status": "merged"|"gate_fail"|"locked", "detail": str}`. `branch` overrides the source branch (population branching merges a candidate branch under the parent feature's name). On `gate_fail` the merge is fully reverted and `.evolve/{feature}/merge_conflict.md` is written. On `merged` the worktree+branch are removed. build_lock is held for the whole merge (the only thing it still serializes).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worktree.py`:

```python
from worktree import merge_feature


def _commit_in_worktree(wt_path, fname, content, msg):
    p = Path(wt_path) / fname
    p.write_text(content)
    _run(["git", "add", "."], wt_path)
    _run(["git", "commit", "-m", msg], wt_path)


def test_merge_feature_happy_path(tmp_path):
    evolve = _git_repo(tmp_path)
    wt = create_feature_worktree(evolve, "F01")
    _commit_in_worktree(wt["path"], "feature.txt", "done\n", "feat: F01")

    result = merge_feature(evolve, "F01")
    assert result["status"] == "merged"
    assert (tmp_path / "feature.txt").exists()      # landed on evolve/demo
    assert not Path(wt["path"]).exists()            # worktree cleaned up


def test_merge_feature_integration_gate_reverts(tmp_path):
    evolve = _git_repo(tmp_path)
    wt = create_feature_worktree(evolve, "F01")
    _commit_in_worktree(wt["path"], "feature.txt", "done\n", "feat: F01")

    head_before = _run(["git", "rev-parse", "HEAD"], tmp_path).stdout.strip()
    result = merge_feature(evolve, "F01", cascade_stages=[
        {"name": "smoke", "cmd": "false", "timeout": 10}])
    assert result["status"] == "gate_fail"
    head_after = _run(["git", "rev-parse", "HEAD"], tmp_path).stdout.strip()
    assert head_after == head_before                # merge reverted
    assert not (tmp_path / "feature.txt").exists()
    conflict = tmp_path / ".evolve" / "F01" / "merge_conflict.md"
    assert conflict.exists()
    assert "smoke" in conflict.read_text()


def test_merge_feature_conflict_aborts(tmp_path):
    evolve = _git_repo(tmp_path)
    wt = create_feature_worktree(evolve, "F01")
    _commit_in_worktree(wt["path"], "app.txt", "worktree version\n", "feat")
    # conflicting change on the base branch
    (tmp_path / "app.txt").write_text("main version\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "conflicting"], tmp_path)

    result = merge_feature(evolve, "F01")
    assert result["status"] == "gate_fail"
    assert (tmp_path / "app.txt").read_text() == "main version\n"
    assert (tmp_path / ".evolve" / "F01" / "merge_conflict.md").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_worktree.py -v -k merge`
Expected: FAIL with `ImportError: cannot import name 'merge_feature'`

- [ ] **Step 3: Write the implementation**

Append to `worktree.py`:

```python
def _write_merge_conflict(evolve_dir: str, feature: str,
                          reason: str, detail: str) -> None:
    feat_dir = Path(evolve_dir) / feature
    feat_dir.mkdir(parents=True, exist_ok=True)
    (feat_dir / "merge_conflict.md").write_text(
        f"# Merge Gate Failure\n\nreason: {reason}\n\n"
        f"## Detail\n\n```\n{detail}\n```\n"
    )


def merge_feature(evolve_dir: str, feature: str, cascade_stages: list = None,
                  health_check=None, branch: str = None) -> dict:
    """Merge a feature branch into the base branch behind the integration
    gate: after merging, the deterministic cascade re-runs on the merged
    tree. Gate failure fully reverts the merge.

    branch: source branch override (population branching merges a
    candidate branch under the parent feature's name). Defaults to the
    feature's own branch.

    Holds build_lock for the whole merge — the only critical section it
    still protects.

    Returns {"status": "merged"|"gate_fail"|"locked", "detail": str}.
    """
    from prepare import acquire_build_lock, release_build_lock
    from cascade import run_cascade

    root = _repo_root(evolve_dir)
    source = branch or feature_branch(evolve_dir, feature)

    bl = acquire_build_lock(evolve_dir)
    if not bl["acquired"]:
        return {"status": "locked", "detail": bl["reason"]}

    try:
        proc = _git(["merge", "--no-ff", "-m",
                     f"merge: {feature} (evolve integration)", source], root)
        if proc.returncode != 0:
            _git(["merge", "--abort"], root)
            detail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
            _write_merge_conflict(evolve_dir, feature,
                                  "merge conflict", detail)
            return {"status": "gate_fail", "detail": detail}

        gate = run_cascade(evolve_dir, feature, cascade_stages or [],
                           cwd=root, health_check=health_check)
        if gate["status"] != "passed":
            _git(["reset", "--hard", "ORIG_HEAD"], root)
            detail = (f"integration cascade failed at stage "
                      f"'{gate['failed_stage']}':\n{gate['output_tail']}")
            _write_merge_conflict(evolve_dir, feature,
                                  "integration cascade regression", detail)
            return {"status": "gate_fail", "detail": detail}

        remove_feature_worktree(evolve_dir, feature, delete_branch=True)
        return {"status": "merged", "detail": ""}
    finally:
        release_build_lock(evolve_dir, bl["token"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_worktree.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit and push**

```bash
git add worktree.py tests/test_worktree.py
git commit -m "feat(worktree): merge_feature with integration gate (cascade re-run, revert on fail)"
git push
```

---

### Task 8: stale-worktree pruning + hook into `acquire_lock` + re-export

**Files:**
- Modify: `worktree.py` (append), `prepare.py` (`acquire_lock` line ~1273; re-export block at end)
- Test: `tests/test_worktree.py` (append)

**Interfaces:**
- Produces: `prune_stale_worktrees(evolve_dir) -> list[str]` — runs `git worktree prune`, then removes worktrees (and branches) whose feature is `completed` per `scan_all_features`. `acquire_lock` calls it best-effort after acquiring (exceptions swallowed — pruning must never block the loop). `from prepare import create_feature_worktree, remove_feature_worktree, merge_feature, prune_stale_worktrees, feature_slug, feature_branch, worktree_path, base_branch` works.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worktree.py`:

```python
from worktree import prune_stale_worktrees


def test_prune_removes_completed_feature_worktrees(tmp_path):
    evolve = _git_repo(tmp_path)
    # spec.md so scan_all_features sees the features
    (Path(evolve) / "spec.md").write_text("- [ ] F01\n- [ ] F02\n")
    header = "commit\tphase\tfeature\tscores\ttotal\tstatus\tsummary"
    (Path(evolve) / "results.tsv").write_text(
        header + "\n"
        "a\teval\tF01\t9/9\t9.0\tpass\tdone\n"
        "b\tbuild\tF02\t-\t-\tkeep\twip\n"
    )
    wt1 = create_feature_worktree(evolve, "F01")
    wt2 = create_feature_worktree(evolve, "F02")

    removed = prune_stale_worktrees(evolve)
    assert "F01" in removed
    assert not Path(wt1["path"]).exists()      # completed -> pruned
    assert Path(wt2["path"]).exists()          # in progress -> kept


def test_acquire_lock_prunes_best_effort(tmp_path):
    # acquire_lock on a NON-git dir must still work (prune errors swallowed)
    from prepare import acquire_lock, release_lock
    evolve = tmp_path / ".evolve"
    evolve.mkdir()
    lock = acquire_lock(str(evolve))
    assert lock["acquired"] is True
    release_lock(str(evolve))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_worktree.py -v -k prune`
Expected: FAIL with `ImportError: cannot import name 'prune_stale_worktrees'`

- [ ] **Step 3: Write the implementation**

Append to `worktree.py`:

```python
def prune_stale_worktrees(evolve_dir: str) -> list:
    """Remove worktree debris: git-level pruning plus worktrees whose
    feature is already completed. Returns list of removed slugs.

    Safe to call every round: branches hold all committed state, so
    removing a worktree loses at most a crashed B's uncommitted edits —
    which is exactly the debris this cleans up.
    """
    from prepare import scan_all_features

    root = _repo_root(evolve_dir)
    _git(["worktree", "prune"], root)

    removed = []
    wt_root = Path(evolve_dir) / "worktrees"
    if not wt_root.exists():
        return removed

    completed = {feature_slug(f["name"])
                 for f in scan_all_features(evolve_dir)
                 if f["state"] == "completed"}

    for entry in wt_root.iterdir():
        if entry.is_dir() and entry.name in completed:
            remove_feature_worktree(evolve_dir, entry.name)
            removed.append(entry.name)
    return removed
```

In `prepare.py`, inside `acquire_lock` (line ~1273), right before the final `return {"acquired": True, ...}`:

```python
    # Best-effort worktree debris cleanup (crashed sessions leave none).
    # Must never block the loop — swallow everything.
    try:
        from worktree import prune_stale_worktrees
        prune_stale_worktrees(evolve_dir)
    except Exception:
        pass
```

Extend the re-export block at the end of `prepare.py`:

```python
from worktree import (create_feature_worktree, remove_feature_worktree,  # noqa: E402,F401
                      merge_feature, prune_stale_worktrees, feature_slug,
                      feature_branch, worktree_path, base_branch)
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 5: Commit and push**

```bash
git add worktree.py prepare.py tests/test_worktree.py
git commit -m "feat(worktree): prune stale worktrees on lock acquisition"
git push
```

---

### Task 9: adapter contract — `allocate_slot()` for parallel resource isolation

**Files:**
- Modify: `adapters/base.py` (append to docstring + new optional function), `adapters/web_app.py` (append)
- Test: `tests/test_adapters.py` (append)

**Interfaces:**
- Produces: optional adapter function `allocate_slot(n: int) -> dict` returning env-var overrides for parallel instance `n`. `web_app.py` reference implementation offsets `PORT` by slot. Adapters without it are conflict-free by declaration.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_adapters.py` (match its existing import style — check the top of the file; it loads reference adapters via `importlib` or direct import; use the same mechanism. If it imports via path, mirror this):

```python
def test_web_app_allocate_slot():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "web_app_adapter",
        str(Path(__file__).parent.parent / "adapters" / "web_app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env0 = mod.allocate_slot(0)
    env2 = mod.allocate_slot(2)
    assert env0["PORT"] != env2["PORT"]
    assert int(env2["PORT"]) == int(env0["PORT"]) + 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_adapters.py -v -k allocate_slot`
Expected: FAIL with `AttributeError: ... has no attribute 'allocate_slot'`

- [ ] **Step 3: Implement**

Append to `adapters/base.py`:

```python
# ---------------------------------------------------------------------------
# Optional Functions
# ---------------------------------------------------------------------------

def allocate_slot(n: int) -> dict:
    """
    OPTIONAL. Environment overrides for running parallel instance n.

    With worktree isolation, several copies of the project may run
    simultaneously (parallel Builders / Critics / candidate branches).
    Adapters whose setup() binds shared resources (ports, db files,
    directories) implement this to keep instances from colliding:

        def allocate_slot(n: int) -> dict:
            return {"PORT": str(8000 + n)}

    setup() should honor the returned env vars. Adapters that do not
    define allocate_slot are declared conflict-free (e.g. content/teaching
    adapters that only write to their own worktree).
    """
    return {}
```

Append to `adapters/web_app.py`:

```python
BASE_PORT = 8000


def allocate_slot(n: int) -> dict:
    """Env overrides for parallel instance n — one port per slot.

    setup() must read PORT from the environment (os.environ.get("PORT",
    str(BASE_PORT))) for this to take effect.
    """
    return {"PORT": str(BASE_PORT + n)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_adapters.py -v`
Expected: all PASS

- [ ] **Step 5: Commit and push**

```bash
git add adapters/base.py adapters/web_app.py tests/test_adapters.py
git commit -m "feat(adapters): optional allocate_slot for parallel instance isolation"
git push
```

---

### Task 10: `population.py` — ids, branching state, `should_branch()`, HARD_LIMITS

**Files:**
- Create: `population.py`
- Modify: `prepare.py` (`HARD_LIMITS` line ~26)
- Test: `tests/test_population.py`

**Interfaces:**
- Produces: `CAND_SEP = "@cand"`; `BRANCH_AFTER_CONSECUTIVE_FAILS = 6`; `parent_feature(feature_id) -> str`; `candidate_feature_id(feature, i) -> str` (`"F01@cand1"`); `_branching_state(evolve_dir, feature) -> dict|None` and `_write_branching_state(...)` (state file `.evolve/{feature}/branching.json`); `should_branch(evolve_dir, feature) -> (bool, str)`. `HARD_LIMITS` gains `max_branching_rounds_per_feature: 1` and `candidates_per_branching: 3`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_population.py
import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from population import (parent_feature, candidate_feature_id,
                        should_branch, BRANCH_AFTER_CONSECUTIVE_FAILS)
from prepare import HARD_LIMITS

HEADER = "commit\tphase\tfeature\tscores\ttotal\tstatus\tsummary"


def _evolve(tmp_path, features=("F01",), rows=()):
    evolve = tmp_path / ".evolve"
    evolve.mkdir(exist_ok=True)
    (evolve / "spec.md").write_text(
        "".join(f"- [ ] {f}\n" for f in features))
    (evolve / "results.tsv").write_text(
        HEADER + "\n" + "".join("\t".join(r) + "\n" for r in rows))
    return str(evolve)


def _fail_rows(feature, n):
    return [[f"c{i}", "eval", feature, "6/6", "6.0", "fail", f"r{i}"]
            for i in range(n)]


def test_candidate_id_roundtrip():
    cid = candidate_feature_id("F01", 2)
    assert cid == "F01@cand2"
    assert parent_feature(cid) == "F01"
    assert parent_feature("F01") == "F01"


def test_hard_limits_extended():
    assert HARD_LIMITS["max_branching_rounds_per_feature"] == 1
    assert HARD_LIMITS["candidates_per_branching"] == 3


def test_should_branch_below_threshold(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 3))
    branch, reason = should_branch(evolve, "F01")
    assert branch is False


def test_should_branch_at_threshold(tmp_path):
    evolve = _evolve(tmp_path,
                     rows=_fail_rows("F01", BRANCH_AFTER_CONSECUTIVE_FAILS))
    branch, reason = should_branch(evolve, "F01")
    assert branch is True
    assert "consecutive fails" in reason


def test_should_branch_budget_exhausted(tmp_path):
    evolve = _evolve(tmp_path,
                     rows=_fail_rows("F01", BRANCH_AFTER_CONSECUTIVE_FAILS))
    state_dir = Path(evolve) / "F01"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "branching.json").write_text(json.dumps(
        {"round": 1, "completed": True, "winner_outcome": "none"}))
    branch, reason = should_branch(evolve, "F01")
    assert branch is False
    assert "budget" in reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_population.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'population'`

- [ ] **Step 3: Write the implementation**

In `prepare.py`, extend `HARD_LIMITS` (line ~26):

```python
HARD_LIMITS = {
    "max_rounds_total": 100,
    "max_rounds_per_feature": 30,
    "max_consecutive_crashes": 5,
    "max_consecutive_fails": 10,
    "max_flat_after_pivot": 3,
    "max_runtime_hours": 24,
    "max_branching_rounds_per_feature": 1,
    "candidates_per_branching": 3,
}
```

Create `population.py`:

```python
# population.py
"""
population.py -- Population branching for stuck features.

Escalation ladder (loop.md documents the full flow):
    consecutive_fails >= 3  -> Mentor advice (existing behavior)
    consecutive_fails >= 6  -> BRANCH: spawn N candidate worktrees, each
                               seeded with a DISTINCT approach
    all candidates fail     -> forced_pass becomes AVAILABLE (user approval
                               still required; enforced by can_force_pass)

Candidate rounds are recorded in results.tsv with feature id
"{feature}@cand{i}" — auditable, but never spec.md features.

Agent MUST NOT modify this file.

Import convention: imports from prepare/worktree happen lazily inside
function bodies (prepare re-exports this module at its end).
"""

import json
from pathlib import Path

CAND_SEP = "@cand"
BRANCH_AFTER_CONSECUTIVE_FAILS = 6


def parent_feature(feature_id: str) -> str:
    """'F01@cand2' -> 'F01'; plain ids pass through."""
    return feature_id.split(CAND_SEP)[0]


def candidate_feature_id(feature: str, i: int) -> str:
    return f"{feature}{CAND_SEP}{i}"


def _state_path(evolve_dir: str, feature: str) -> Path:
    return Path(evolve_dir) / feature / "branching.json"


def _branching_state(evolve_dir: str, feature: str):
    """Read branching.json, or None if no branching round was started."""
    path = _state_path(evolve_dir, feature)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_branching_state(evolve_dir: str, feature: str,
                           state: dict) -> None:
    path = _state_path(evolve_dir, feature)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def should_branch(evolve_dir: str, feature: str) -> tuple:
    """Decide whether the escalation ladder has reached the branching
    step. Returns (branch: bool, reason: str). Code-enforced — O calls
    this instead of deciding by feel.
    """
    from prepare import scan_all_features, HARD_LIMITS

    info = next((f for f in scan_all_features(evolve_dir)
                 if f["name"] == feature), None)
    if info is None:
        return False, f"unknown feature: {feature}"

    state = _branching_state(evolve_dir, feature)
    rounds_used = state.get("round", 0) if state else 0
    if rounds_used >= HARD_LIMITS["max_branching_rounds_per_feature"]:
        return False, "branching budget exhausted"

    fails = info["consecutive_fails"]
    if fails >= BRANCH_AFTER_CONSECUTIVE_FAILS:
        return True, f"{fails} consecutive fails (>= " \
                     f"{BRANCH_AFTER_CONSECUTIVE_FAILS})"
    return False, (f"{fails} consecutive fails (< "
                   f"{BRANCH_AFTER_CONSECUTIVE_FAILS})")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_population.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit and push**

```bash
git add population.py prepare.py tests/test_population.py
git commit -m "feat(population): candidate ids, branching state, should_branch ladder check"
git push
```

---

### Task 11: `population.py` — `spawn_candidates()`

**Files:**
- Modify: `population.py` (append)
- Test: `tests/test_population.py` (append)

**Interfaces:**
- Consumes: `create_feature_worktree`, `feature_branch`, `feature_slug` from worktree; `HARD_LIMITS` from prepare (all lazy).
- Produces: `spawn_candidates(evolve_dir, feature, approaches: list[str]) -> list[dict]` — each `{"cand_id": int, "feature_id": "F01@cand1", "path": str, "branch": str, "approach": str}`. Creates worktrees named `{slug}-cand{i}` forked from the feature's branch (or base branch if none), seeds `.evolve/{feature_id}/strategy.md` with the approach, writes `branching.json` with `completed: False`. Caps N at `HARD_LIMITS["candidates_per_branching"]`. Raises `ValueError` on empty `approaches`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_population.py` (reuses the git fixture from test_worktree — duplicate the two small helpers here since files must be independently runnable):

```python
import subprocess
import pytest
from population import spawn_candidates


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True,
                          capture_output=True, text=True)


def _git_evolve(tmp_path, rows=()):
    _run(["git", "init", "-b", "evolve/demo", "."], tmp_path)
    _run(["git", "config", "user.email", "t@example.com"], tmp_path)
    _run(["git", "config", "user.name", "t"], tmp_path)
    (tmp_path / "app.txt").write_text("v1\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "init"], tmp_path)
    evolve = tmp_path / ".evolve"
    evolve.mkdir()
    (evolve / "spec.md").write_text("- [ ] F01\n")
    (evolve / "results.tsv").write_text(
        HEADER + "\n" + "".join("\t".join(r) + "\n" for r in rows))
    return str(evolve)


def test_spawn_candidates_creates_worktrees_and_seeds(tmp_path):
    evolve = _git_evolve(tmp_path)
    cands = spawn_candidates(evolve, "F01",
                             ["approach A", "approach B", "approach C"])
    assert len(cands) == 3
    for i, cand in enumerate(cands, 1):
        assert cand["feature_id"] == f"F01@cand{i}"
        assert Path(cand["path"], "app.txt").exists()
        strategy = Path(evolve) / cand["feature_id"] / "strategy.md"
        assert cand["approach"] in strategy.read_text()
    state = json.loads((Path(evolve) / "F01" / "branching.json").read_text())
    assert state["round"] == 1
    assert state["completed"] is False
    assert len(state["candidates"]) == 3


def test_spawn_candidates_caps_at_budget(tmp_path):
    evolve = _git_evolve(tmp_path)
    cands = spawn_candidates(evolve, "F01", ["a", "b", "c", "d", "e"])
    assert len(cands) == HARD_LIMITS["candidates_per_branching"]


def test_spawn_candidates_requires_approaches(tmp_path):
    evolve = _git_evolve(tmp_path)
    with pytest.raises(ValueError):
        spawn_candidates(evolve, "F01", [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_population.py -v -k spawn`
Expected: FAIL with `ImportError: cannot import name 'spawn_candidates'`

- [ ] **Step 3: Write the implementation**

Append to `population.py`:

```python
def spawn_candidates(evolve_dir: str, feature: str,
                     approaches: list) -> list:
    """Fork N candidate worktrees for a stuck feature, each seeded with a
    distinct approach (O draws approaches from Mentor hypotheses and C's
    untried Pivot options).

    Returns [{"cand_id", "feature_id", "path", "branch", "approach"}].
    Raises ValueError if approaches is empty.
    """
    from prepare import HARD_LIMITS
    from worktree import (create_feature_worktree, feature_branch,
                          feature_slug, base_branch, _repo_root, _git)

    if not approaches:
        raise ValueError("spawn_candidates requires at least one approach")

    n = min(len(approaches), HARD_LIMITS["candidates_per_branching"])
    root = _repo_root(evolve_dir)

    # Fork from the feature's own branch if it exists, else the base branch.
    incumbent = feature_branch(evolve_dir, feature)
    if _git(["rev-parse", "--verify", incumbent], root).returncode != 0:
        incumbent = base_branch(evolve_dir)

    state = _branching_state(evolve_dir, feature) or {"round": 0}
    candidates = []
    for i in range(1, n + 1):
        cand_name = f"{feature_slug(feature)}-cand{i}"
        wt = create_feature_worktree(evolve_dir, cand_name,
                                     from_branch=incumbent)
        feature_id = candidate_feature_id(feature, i)
        cand_dir = Path(evolve_dir) / feature_id
        cand_dir.mkdir(parents=True, exist_ok=True)
        (cand_dir / "strategy.md").write_text(
            f"# Candidate {i} strategy (seeded)\n\n"
            f"## Approach\n\n{approaches[i - 1]}\n\n"
            f"## Rules\n\n- Work ONLY in worktree {wt['path']}\n"
            f"- Record results.tsv rows with feature id {feature_id}\n"
        )
        candidates.append({"cand_id": i, "feature_id": feature_id,
                           "path": wt["path"], "branch": wt["branch"],
                           "approach": approaches[i - 1]})

    _write_branching_state(evolve_dir, feature, {
        "round": state.get("round", 0) + 1,
        "completed": False,
        "winner_outcome": None,
        "candidates": [{"cand_id": c["cand_id"],
                        "feature_id": c["feature_id"],
                        "branch": c["branch"],
                        "approach": c["approach"]} for c in candidates],
    })
    return candidates
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_population.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit and push**

```bash
git add population.py tests/test_population.py
git commit -m "feat(population): spawn_candidates forks seeded candidate worktrees"
git push
```

---

### Task 12: `population.py` — `select_candidate()` + reset-aware fail counting

**Files:**
- Modify: `population.py` (append), `prepare.py` (`scan_all_features` consecutive-fails loop, line ~1058)
- Test: `tests/test_population.py` (append)

**Interfaces:**
- Consumes: results.tsv candidate rows, `branching.json`, `merge_feature(..., branch=)` from Task 7.
- Produces: `select_candidate(evolve_dir, feature) -> dict` with `{"outcome": "pass"|"adopt"|"none", "cand_id": int|None, "feature_id": str|None, "detail": str}`. Rules: cascade_fail rows are disqualified; a candidate whose last eval `status == "pass"` wins outright (`"pass"`); otherwise the candidate with the highest **minimum dimension score** (tie-break: highest total) is adopted (`"adopt"`) only if it beats the incumbent's last min-dim, else `"none"`. Updates `branching.json` (`completed: True`, `winner_outcome`). On `"adopt"`, resets the incumbent branch to the winner (`git branch -f`) and appends a `status=reset` build row so fail counting restarts. In `scan_all_features`, a `reset` row now breaks the consecutive-fails count.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_population.py`:

```python
from population import select_candidate


def _min_dim_rows():
    """3 candidates: cand1 cascade_fail, cand2 min-dim 6, cand3 min-dim 7."""
    return [
        ["a", "eval", "F01", "6/6", "6.0", "fail", "incumbent"],
        ["b", "eval", "F01@cand1", "-", "0", "cascade_fail", "broken"],
        ["c", "eval", "F01@cand2", "6/9", "7.5", "fail", "half"],
        ["d", "eval", "F01@cand3", "7/8", "7.5", "fail", "solid"],
    ]


def _spawn_three(evolve):
    return spawn_candidates(evolve, "F01", ["a1", "a2", "a3"])


def test_select_candidate_pass_wins_outright(tmp_path):
    evolve = _git_evolve(tmp_path, rows=_min_dim_rows() + [
        ["e", "eval", "F01@cand2", "9/9", "9.0", "pass", "threshold met"],
    ])
    _spawn_three(evolve)
    result = select_candidate(evolve, "F01")
    assert result["outcome"] == "pass"
    assert result["feature_id"] == "F01@cand2"
    state = json.loads((Path(evolve) / "F01" / "branching.json").read_text())
    assert state["completed"] is True
    assert state["winner_outcome"] == "pass"


def test_select_candidate_adopts_best_min_dim(tmp_path):
    evolve = _git_evolve(tmp_path, rows=_min_dim_rows())
    _spawn_three(evolve)
    result = select_candidate(evolve, "F01")
    assert result["outcome"] == "adopt"           # cand3: min-dim 7 > 6
    assert result["feature_id"] == "F01@cand3"
    # adopt appends a reset row for the parent feature
    tsv = (Path(evolve) / "results.tsv").read_text()
    assert "reset" in tsv and "adopted candidate 3" in tsv


def test_select_candidate_none_when_no_improvement(tmp_path):
    rows = [
        ["a", "eval", "F01", "7/7", "7.0", "fail", "incumbent"],
        ["b", "eval", "F01@cand1", "5/6", "5.5", "fail", "worse"],
        ["c", "eval", "F01@cand2", "6/6", "6.0", "fail", "worse"],
        ["d", "eval", "F01@cand3", "-", "0", "cascade_fail", "broken"],
    ]
    evolve = _git_evolve(tmp_path, rows=rows)
    _spawn_three(evolve)
    result = select_candidate(evolve, "F01")
    assert result["outcome"] == "none"
    state = json.loads((Path(evolve) / "F01" / "branching.json").read_text())
    assert state["winner_outcome"] == "none"


def test_scan_resets_fail_count_on_reset_row(tmp_path):
    from prepare import scan_all_features
    rows = (_fail_rows("F01", 4) +
            [["r", "build", "F01", "-", "-", "reset", "adopted candidate 2"]])
    evolve = _evolve(tmp_path, rows=rows)
    info = next(f for f in scan_all_features(evolve) if f["name"] == "F01")
    assert info["consecutive_fails"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_population.py -v -k "select or resets"`
Expected: FAIL

- [ ] **Step 3: Write the implementation**

In `prepare.py`, in `scan_all_features`'s consecutive-fails loop (line ~1058), add a `reset` break as the FIRST condition:

```python
            # Count consecutive fails
            for r in reversed(feat_data):
                if r.get("status") == "reset":
                    break
                if r.get("phase") == "eval" and r.get("status") == "fail":
                    info["consecutive_fails"] += 1
                elif r.get("phase") == "eval" and r.get("status") == "pass":
                    break
                elif r.get("phase") == "build" and r.get("status") == "keep":
                    break
```

Append to `population.py`:

```python
def _min_dim(scores_str: str):
    """'7/8/9' -> 7.0. Returns None if unparseable."""
    try:
        vals = [float(s) for s in scores_str.split("/")]
        return min(vals) if vals else None
    except (ValueError, AttributeError):
        return None


def _last_eval_row(rows: list, feature_id: str):
    for r in reversed(rows):
        if r.get("feature") == feature_id and r.get("phase") == "eval":
            return r
    return None


def select_candidate(evolve_dir: str, feature: str) -> dict:
    """Pick the branching round's winner from results.tsv candidate rows.

    Rules (spec §3):
      1. cascade_fail rows disqualify that candidate;
      2. a candidate whose last eval status == "pass" wins outright;
      3. otherwise the candidate with the highest MINIMUM dimension score
         (tie-break: highest total) is ADOPTED as the new lineage — but
         only if it beats the incumbent's last min-dim;
      4. else outcome "none" (forced_pass gate opens; see can_force_pass).

    Marks branching.json completed and records winner_outcome.
    On "pass" AND "adopt": resets the incumbent feature branch to the
    winner's branch — so the standard merge_feature(feature) path works
    afterwards and EVERY candidate branch can be safely cleaned up here
    (otherwise a pass-winner's branch would be deleted before O merges it).
    On "adopt" additionally appends a status=reset row so fail counting
    restarts.
    """
    import csv as _csv
    from prepare import append_result
    from worktree import (feature_branch, feature_slug, _repo_root, _git,
                          remove_feature_worktree)

    state = _branching_state(evolve_dir, feature)
    if not state or not state.get("candidates"):
        return {"outcome": "none", "cand_id": None, "feature_id": None,
                "detail": "no branching round in progress"}

    tsv = Path(evolve_dir) / "results.tsv"
    rows = []
    if tsv.exists():
        with open(tsv, newline="") as f:
            rows = list(_csv.DictReader(f, delimiter="\t"))

    incumbent_row = _last_eval_row(
        rows, feature) if rows else None
    incumbent_min = _min_dim(incumbent_row["scores"]) if incumbent_row \
        else None

    best = None   # (min_dim, total, cand)
    winner = None
    for cand in state["candidates"]:
        last = _last_eval_row(rows, cand["feature_id"])
        if last is None or last.get("status") == "cascade_fail":
            continue
        if last.get("status") == "pass":
            winner = ("pass", cand)
            break
        min_dim = _min_dim(last.get("scores", ""))
        if min_dim is None:
            continue
        try:
            total = float(last.get("total", "0"))
        except ValueError:
            total = 0.0
        if best is None or (min_dim, total) > (best[0], best[1]):
            best = (min_dim, total, cand)

    if winner is None and best is not None and \
            (incumbent_min is None or best[0] > incumbent_min):
        winner = ("adopt", best[2])

    outcome = winner[0] if winner else "none"
    cand = winner[1] if winner else None

    state["completed"] = True
    state["winner_outcome"] = outcome
    state["winner"] = cand["feature_id"] if cand else None
    _write_branching_state(evolve_dir, feature, state)

    if outcome in ("pass", "adopt"):
        # Point the incumbent feature branch at the winner. After this,
        # the winner's commits live on the feature branch, so all
        # candidate branches (including the winner's) are safe to delete
        # below, and O merges via the standard merge_feature(feature).
        root = _repo_root(evolve_dir)
        incumbent_branch = feature_branch(evolve_dir, feature)
        if _git(["rev-parse", "--verify", "refs/heads/" + incumbent_branch],
                root).returncode == 0:
            # Feature worktree must not hold the branch while we move it.
            remove_feature_worktree(evolve_dir, feature,
                                    delete_branch=False)
            _git(["branch", "-f", incumbent_branch, cand["branch"]], root)
        else:
            _git(["branch", incumbent_branch, cand["branch"]], root)
        if outcome == "adopt":
            append_result(str(tsv), {
                "commit": "-", "phase": "build", "feature": feature,
                "scores": "-", "total": "-", "status": "reset",
                "summary": f"adopted candidate {cand['cand_id']} lineage "
                           f"({cand['branch']})",
            })

    # Candidate cleanup: every candidate worktree+branch goes. Safe even
    # for the winner — its commits now live on the incumbent branch.
    for c in state["candidates"]:
        cand_name = f"{feature_slug(feature)}-cand{c['cand_id']}"
        remove_feature_worktree(evolve_dir, cand_name)

    return {"outcome": outcome,
            "cand_id": cand["cand_id"] if cand else None,
            "feature_id": cand["feature_id"] if cand else None,
            "detail": f"winner_outcome={outcome}"}
```

Note for the runtime flow (documented in Task 14's loop.md edits, not code): on `outcome == "pass"` the feature branch already points at the winner, so O merges via the standard `merge_feature(evolve_dir, feature, cascade_stages=stages)` and appends the parent's pass row. The `branch=` override on merge_feature stays available for other uses but is not needed here.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 5: Commit and push**

```bash
git add population.py prepare.py tests/test_population.py
git commit -m "feat(population): select_candidate with min-dim rule, adopt resets lineage"
git push
```

---

### Task 13: gated forced_pass + reporting split (true vs forced) + `@cand` filtering

**Files:**
- Modify: `population.py` (append), `prepare.py` (`read_progress` line ~351, `generate_report` line ~462, `scan_all_features` state block line ~1067; re-export block)
- Test: `tests/test_population.py` and `tests/test_prepare.py` (append)

**Interfaces:**
- Produces: `can_force_pass(evolve_dir, feature) -> (bool, str)` — True only when branching completed with `winner_outcome == "none"` (or `"adopt"` that still never passed — i.e. any completed round without a `"pass"` winner). `mark_forced_pass(evolve_dir, feature, user_approved: bool)` — the only sanctioned writer of `status="forced"` rows; raises `ValueError` if gate closed or `user_approved` is not True. `read_progress` gains `forced_features: list` (eval/forced completes a feature but is NOT in `completed_features`) and ignores `@cand` rows for completion. `scan_all_features` treats eval/forced as `completed` and gains state `"branching"` when `branching.json` exists with `completed: False`. `generate_report` overview shows `Passed: M true + K forced / T` and lists forced features with a `⚑` marker; `@cand` rows are folded into the parent's iteration record, not listed as features.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_population.py`:

```python
from population import can_force_pass, mark_forced_pass


def _completed_branching(evolve, feature, outcome):
    d = Path(evolve) / feature
    d.mkdir(exist_ok=True)
    (d / "branching.json").write_text(json.dumps(
        {"round": 1, "completed": True, "winner_outcome": outcome,
         "candidates": [{"cand_id": 1, "feature_id": f"{feature}@cand1",
                         "branch": "x", "approach": "a"}]}))


def test_can_force_pass_requires_branching(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 8))
    ok, reason = can_force_pass(evolve, "F01")
    assert ok is False
    assert "branching" in reason


def test_can_force_pass_closed_while_in_flight(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 8))
    d = Path(evolve) / "F01"
    d.mkdir(exist_ok=True)
    (d / "branching.json").write_text(json.dumps(
        {"round": 1, "completed": False}))
    assert can_force_pass(evolve, "F01")[0] is False


def test_can_force_pass_closed_after_passing_winner(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 8))
    _completed_branching(evolve, "F01", "pass")
    assert can_force_pass(evolve, "F01")[0] is False


def test_can_force_pass_open_after_no_winner(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 8))
    _completed_branching(evolve, "F01", "none")
    ok, reason = can_force_pass(evolve, "F01")
    assert ok is True


def test_mark_forced_pass_gate_and_approval(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 8))
    with pytest.raises(ValueError, match="gate"):
        mark_forced_pass(evolve, "F01", user_approved=True)
    _completed_branching(evolve, "F01", "none")
    with pytest.raises(ValueError, match="approval"):
        mark_forced_pass(evolve, "F01", user_approved=False)
    mark_forced_pass(evolve, "F01", user_approved=True)
    tsv = (Path(evolve) / "results.tsv").read_text()
    assert "forced" in tsv
```

Append to `tests/test_prepare.py`:

```python
def test_read_progress_forced_features(tmp_path):
    path = str(tmp_path / "results.tsv")
    append_result(path, {"commit": "a", "phase": "eval", "feature": "F01",
                         "scores": "9/9", "total": "9.0", "status": "pass",
                         "summary": "real pass"})
    append_result(path, {"commit": "-", "phase": "eval", "feature": "F02",
                         "scores": "-", "total": "-", "status": "forced",
                         "summary": "forced_pass approved"})
    append_result(path, {"commit": "b", "phase": "eval",
                         "feature": "F03@cand1", "scores": "9/9",
                         "total": "9.0", "status": "pass",
                         "summary": "candidate pass"})
    p = read_progress(path)
    assert p["completed_features"] == ["F01"]      # no forced, no @cand
    assert p["forced_features"] == ["F02"]


def test_scan_all_features_forced_and_branching(tmp_path):
    evolve = tmp_path / ".evolve"
    evolve.mkdir()
    (evolve / "spec.md").write_text("- [ ] F01\n- [ ] F02\n")
    (evolve / "results.tsv").write_text(
        "\t".join(HEADER_FIELDS) + "\n"
        "-\teval\tF01\t-\t-\tforced\twaived\t-\n"
    )
    f02 = evolve / "F02"
    f02.mkdir()
    (f02 / "branching.json").write_text(
        '{"round": 1, "completed": false}')
    feats = {f["name"]: f for f in scan_all_features(str(evolve))}
    assert feats["F01"]["state"] == "completed"
    assert feats["F02"]["state"] == "branching"


def test_generate_report_shows_forced_split(tmp_path):
    path = str(tmp_path / "results.tsv")
    append_result(path, {"commit": "a", "phase": "eval", "feature": "F01",
                         "scores": "9/9", "total": "9.0", "status": "pass",
                         "summary": "real"})
    append_result(path, {"commit": "-", "phase": "eval", "feature": "F02",
                         "scores": "-", "total": "-", "status": "forced",
                         "summary": "waived"})
    report = generate_report(path)
    assert "1 true + 1 forced" in report
    assert "⚑ F02" in report
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_population.py tests/test_prepare.py -v -k "force or forced or branching"`
Expected: FAIL

- [ ] **Step 3: Implement**

Append to `population.py`:

```python
def can_force_pass(evolve_dir: str, feature: str) -> tuple:
    """forced_pass gate: open ONLY after a completed branching round
    produced no passing winner. Returns (ok: bool, reason: str)."""
    state = _branching_state(evolve_dir, feature)
    if state is None:
        return False, "gate closed: no branching round attempted yet"
    if not state.get("completed"):
        return False, "gate closed: branching round still in flight"
    if state.get("winner_outcome") == "pass":
        return False, "gate closed: branching produced a passing winner"
    return True, ("branching round completed with no passing candidate "
                  f"(winner_outcome={state.get('winner_outcome')})")


def mark_forced_pass(evolve_dir: str, feature: str,
                     user_approved: bool) -> None:
    """The ONLY sanctioned path to a status=forced row.

    Raises ValueError if the gate is closed or approval is missing.
    """
    ok, reason = can_force_pass(evolve_dir, feature)
    if not ok:
        raise ValueError(f"forced_pass gate: {reason}")
    if user_approved is not True:
        raise ValueError("forced_pass requires explicit user approval")

    from prepare import append_result
    append_result(str(Path(evolve_dir) / "results.tsv"), {
        "commit": "-", "phase": "eval", "feature": feature,
        "scores": "-", "total": "-", "status": "forced",
        "summary": f"forced_pass approved by user ({reason})",
    })
```

In `prepare.py` — `read_progress` (line ~371): add `"forced_features": []` to the initial `result` dict, and change the completed-features collection loop to:

```python
    for row in rows:
        phase = row.get("phase", "")
        status = row.get("status", "")
        feature = row.get("feature", "-")
        commit = row.get("commit", "")

        if feature != "-" and "@cand" in feature:
            continue   # candidate rows never complete a spec feature

        if phase == "eval" and status == "pass":
            if feature not in result["completed_features"] and feature != "-":
                result["completed_features"].append(feature)
            result["last_pass_commit"] = commit
        elif phase == "eval" and status == "forced":
            if feature not in result["forced_features"] and feature != "-":
                result["forced_features"].append(feature)
```

In `prepare.py` — `scan_all_features` state block (line ~1067), extend:

```python
            if last_phase == "eval" and last_status in ("pass", "forced"):
                info["state"] = "completed"
            elif last_phase == "build" and last_status == "keep":
                info["state"] = "needs_eval"
            elif last_phase == "build" and last_status == "crash":
                info["state"] = "needs_build"
            elif last_phase == "eval" and last_status == "fail":
                info["state"] = "needs_build"
            else:
                info["state"] = "needs_build"
```

and after that block (before the lock check), add the branching override:

```python
        # Branching round in flight overrides build/eval states
        branching_json = evolve_path / feat_name / "branching.json"
        if info["state"] != "completed" and branching_json.exists():
            try:
                bstate = json.loads(branching_json.read_text())
                if not bstate.get("completed"):
                    info["state"] = "branching"
            except (json.JSONDecodeError, OSError):
                pass
```

In `prepare.py` — `generate_report`: in the per-row grouping loop (line ~483), fold candidates into their parent:

```python
    for row in rows:
        feat = row.get("feature", "-")
        if feat == "-":
            continue
        feat = feat.split("@cand")[0]   # fold candidate rows into parent
```

Treat `forced` as terminal in the grouping (`final_status` will be `"forced"`), then:
- change `completed = [...]` block (line ~499) to:

```python
    completed = [f for f, i in features.items() if i["final_status"] == "pass"]
    forced = [f for f, i in features.items() if i["final_status"] == "forced"]
    skipped = [f for f, i in features.items() if i["final_status"] == "skip"]
```

- current-feature scan (line ~505): exclude `"forced"` alongside `"pass"`/`"skip"`.
- complete condition (line ~515): `len(completed) + len(forced) + len(skipped) == total_features`.
- Overview line (line ~526): when `forced` is non-empty use

```python
        lines.append(f"  Passed: {len(completed)} true + {len(forced)} "
                     f"forced / {total_features} features"
                     + (f" | Current: {current_feat} (best {best})"
                        if current_feat else ""))
```

(keep the existing wording when `forced` is empty), and in Feature Progress add before the `skip` case:

```python
            elif info["final_status"] == "forced":
                lines.append(f"  ⚑ {feat}    -- forced (waived, not a true pass)")
```

Extend the re-export block at the end of `prepare.py`:

```python
from population import (should_branch, spawn_candidates, select_candidate,  # noqa: E402,F401
                        can_force_pass, mark_forced_pass, parent_feature,
                        candidate_feature_id,
                        BRANCH_AFTER_CONSECUTIVE_FAILS)
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS

- [ ] **Step 5: Commit and push**

```bash
git add population.py prepare.py tests/test_population.py tests/test_prepare.py
git commit -m "feat(population): gated forced_pass, true-vs-forced reporting, branching state"
git push
```

---

### Task 14: agent docs — loop.md, critic.md, orchestrator.md, builder.md

**Files:**
- Modify: `loop.md`, `agents/critic.md`, `agents/orchestrator.md`, `agents/builder.md`

No tests (docs). Make exactly these edits:

- [ ] **Step 1: loop.md**

1. In `## O's Dispatch Flow` after step 3.5, insert a new step:

```markdown
### 3.6. Branching check — code-enforced escalation ladder

Before dispatching B for a stuck feature, ask the code (not your judgment):

```python
from prepare import should_branch, spawn_candidates, select_candidate, merge_feature

branch, reason = should_branch(".evolve", feat["name"])
if branch:
    # O seeds N distinct approaches from Mentor hypotheses + C's untried Pivots
    cands = spawn_candidates(".evolve", feat["name"], approaches)
    # dispatch one B→C chain per candidate worktree (counts against the
    # 5-concurrency cap); candidate results.tsv rows use "F01@cand1" ids
```

When every candidate has an eval row, close the round:

```python
result = select_candidate(".evolve", feat["name"])
if result["outcome"] == "pass":
    # select_candidate already reset the feature branch to the winner —
    # merge through the standard integration gate:
    merge_feature(".evolve", feat["name"], cascade_stages=stages)
    # then append the parent's eval/pass row
elif result["outcome"] == "adopt":
    # lineage was reset to the best candidate; feature returns to needs_build
    pass
else:  # "none"
    # forced_pass gate is now open — can_force_pass() returns True.
    # Ask the USER before calling mark_forced_pass(..., user_approved=True).
    pass
```

Escalation ladder (code-enforced): 3 fails → Mentor; 6 fails → branching;
branching with no passing winner → forced_pass available (user approval
required); user declines → BLOCKER.
```

2. In `### 5. Dispatch B`, replace the "Only one B agent at a time (git constraint)" paragraph with:

```markdown
B agents run in PARALLEL, one per feature, each in its own git worktree
(created via `create_feature_worktree(".evolve", feat["name"])`; B's
dispatch tells it the worktree path). build_lock no longer serializes B —
it only protects merges into the evolve/<tag> branch (held internally by
`merge_feature()`). Cap total concurrent codex processes at 5.

When a feature passes eval, O integrates it:

```python
from prepare import merge_feature, load_cascade_config
stages = load_cascade_config(".evolve/eval.yml")
result = merge_feature(".evolve", feat["name"], cascade_stages=stages)
# "merged"    -> feature completed, worktree cleaned up
# "gate_fail" -> merge reverted; see .evolve/{feature}/merge_conflict.md;
#                feature returns to needs_build
```
```

3. In `## Eval Flow (C Agent)`, insert before `### Deterministic Scoring`:

```markdown
### Deterministic Cascade (runs FIRST — cheap gates before any judge)

```python
from prepare import load_cascade_config, run_cascade
stages = load_cascade_config(".evolve/eval.yml")
gate = run_cascade(".evolve", feature, stages, cwd=worktree_path)
if gate["status"] == "cascade_fail":
    # Round is VOID — no LLM judge call, no dimension scores.
    append_result(".evolve/results.tsv", {
        "commit": "<hash>", "phase": "eval", "feature": feature,
        "scores": "-", "total": "0", "status": "cascade_fail",
        "summary": f"cascade_fail at {gate['failed_stage']}"})
    -> stop this eval round
```

`validate_eval_result` now also requires `result["cascade"]` to be
`"passed"` (or `"empty"` when eval.yml declares no cascade) — the judge
cannot run against a broken build.
```

4. In `### Record Result` (C), extend the `append_result` example with the pairwise field:

```python
    "pairwise": "log:better/ui:same/db:worse",   # per-dimension vs previous round
```

5. In `## Concurrency Rules`, replace rule 1 with:

```markdown
1. **B parallel via worktrees**: each feature's B works in
   `.evolve/worktrees/{slug}` on branch `evolve/<tag>--{slug}`; build_lock
   only serializes merges into evolve/<tag>
```

6. In the `## File Permission Matrix` per-feature table, add rows:

```markdown
| cascade_fail.md | read | read | read | write |
| merge_conflict.md | write (via merge_feature) | read | read | - |
| branching.json | write (via population fns) | read | - | - |
```

- [ ] **Step 2: agents/critic.md**

1. In `## Per-Run Flow`, insert as the new step 1 (renumber the rest):

```markdown
1. Run the deterministic cascade FIRST (`run_cascade` — includes the
   implicit health check). `cascade_fail` → record the void round
   (status=cascade_fail, scores "-", total 0, summary quoting the failing
   stage) and STOP — no judge call. Never choose Pivot from a
   cascade_fail alone; it is not evidence of a failed approach.
```

2. After the results.tsv example in Per-Run Flow, add:

```markdown
### Pairwise verdicts (mandatory when a previous round exists)

Your dispatch contains `## Previous Round Evidence`. For EVERY dimension,
judge this round against it: `better | same | worse`. Record in
results.tsv's `pairwise` column as `log:better/ui:same/db:worse`.
Pass/fail stays on absolute scores vs threshold — pairwise only feeds
trajectory analysis, which now trusts it over raw score deltas and marks
contradictions (score up, pairwise worse) as `noisy`.
```

3. In `## Mentor Advice (if present in your dispatch)`, replace the
"advice #3 → record `status=blocker`" paragraph with:

```markdown
- If this is mentor advice #3 and the feature still fails this round, the
  feature becomes BRANCHING-ELIGIBLE, not a blocker: O will run
  should_branch()/spawn_candidates() (see loop.md 3.6). BLOCKER is
  reached only when a branching round completed with no passing winner
  AND the user declined forced_pass. Only then record `status=blocker`
  in results.tsv and write the BLOCKER section in strategy.md.
```

- [ ] **Step 3: agents/orchestrator.md**

Append a section:

```markdown
## Escalation Ladder（代码强制，不是建议）

卡住的 feature 走阶梯，每一步由代码判定，O 不凭感觉：

1. `consecutive_fails >= 3` → 派 Mentor（现行为不变）
2. `should_branch()` 返回 True（≥6 连败且预算未用完）→ `spawn_candidates()`
   派 3 个候选，每个 worktree 一条 B→C 链，方案必须彼此不同（来自 Mentor
   假设 + C 未试过的 Pivot）
3. 候选全部评完 → `select_candidate()` 关轮：pass 直接合入；adopt 换谱系
   重来；none → forced_pass 门打开
4. **只有 `can_force_pass()` 为 True 时才允许问用户 forced_pass**；
   `mark_forced_pass()` 是唯一入口，绕过会 raise
5. 用户拒绝 forced_pass → BLOCKER（终态跳过）

报告里 forced 和真 pass 分开展示（`M true + K forced / T`），永远不把
弃权说成达标。
```

- [ ] **Step 4: agents/builder.md**

Append:

```markdown
## Worktree Isolation

- Your dispatch names YOUR worktree path (`.evolve/worktrees/{slug}`).
  ALL code edits happen there — never in the main working tree, never on
  the evolve/<tag> branch directly.
- Commit inside the worktree as usual (one commit per run). Integration
  into evolve/<tag> is O's job via merge_feature() after the feature
  passes — you never merge.
- If your feature id contains `@cand` you are one of several parallel
  candidates: follow the seeded approach in your strategy.md exactly;
  do not converge on another candidate's approach.
```

- [ ] **Step 5: Commit and push**

```bash
git add loop.md agents/critic.md agents/orchestrator.md agents/builder.md
git commit -m "docs(agents): cascade-first eval, worktree builders, escalation ladder"
git push
```

---

### Task 15: README updates + final verification

**Files:**
- Modify: `README.md`, `README-en.md`

- [ ] **Step 1: Update README.md**

1. Test badge (line 9): run `python -m pytest tests/ -q | tail -1`, put the real count in the badge (e.g. `tests-160%20passed`).
2. In `## 核心原则`, append:

```markdown
9. **确定性级联先行** — 便宜的 build/lint/test/健康检查先跑、先挂先停，LLM 评审只在级联全过后才花钱；全 0 分再也不会伪装成产品回归。
10. **卡住就分支，不再直接弃权** — ≥6 连败派 3 个候选并行试不同方案，全败才解锁 forced_pass（仍需你批准）；报告永远区分真 pass 和 forced。
```

3. In `## 核心概念` after the "一切都是文件" table, add rows:

```markdown
| `{feature}/cascade_fail.md` | 级联失败详情（哪个 stage、输出尾部） | C（每次失败覆写） |
| `{feature}/branching.json` | 种群分支状态：轮次、候选、胜者 | O（经 population 函数） |
| `{feature}/merge_conflict.md` | 集成门失败详情 | O（经 merge_feature） |
```

4. In `## 跑偏了的逃生口` table, update the forced_pass row:

```markdown
| 某 feature 烧 N 轮无解 | "给它跑一轮分支" | ≥6 连败自动触发；全败后才可 forced_pass |
```

- [ ] **Step 2: Update README-en.md**

Mirror the same three edits in English (badge, two new principles, new file-table rows, escape-hatch row). Principle text:

```markdown
9. **Deterministic cascade first** — cheap build/lint/test/health gates run and fail fast before any LLM judging pays; all-zero rounds can no longer masquerade as product regressions.
10. **Branch when stuck, don't waive** — ≥6 consecutive fails spawns 3 parallel candidates with distinct approaches; forced_pass unlocks only after all candidates fail (and still needs your approval); reports always split true passes from forced ones.
```

- [ ] **Step 3: Full verification**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass (~160). Then sanity-check the re-export surface:

```bash
python -c "import sys; sys.path.insert(0, '.'); from prepare import \
load_cascade_config, run_cascade, create_feature_worktree, merge_feature, \
prune_stale_worktrees, should_branch, spawn_candidates, select_candidate, \
can_force_pass, mark_forced_pass; print('re-exports OK')"
```

Expected: `re-exports OK`

- [ ] **Step 4: Commit and push**

```bash
git add README.md README-en.md
git commit -m "docs(readme): cascade + branching principles, true/forced split, badge refresh"
git push
```
