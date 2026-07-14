# Tier 1 Token Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut evolve's per-round token consumption (evidence truncation, summary caching, model routing, output contracts, cache-friendly ordering) with zero functional change, plus one prerequisite lock-leak bugfix.

**Architecture:** All code changes live in `prepare.py` (constants + `build_manifest` + `prepare_dispatch`); two items are agent-contract doc edits (`agents/critic.md`, `agents/mentor.md`). Spec: `docs/superpowers/specs/2026-07-10-token-optimization-tier1-design.md`. Branch: `feature/cascade-population-worktree` (continues the gap-fix branch).

**Tech Stack:** Python 3.8+ stdlib only (hashlib, json, os, pathlib), pytest.

## Global Constraints

- **Zero functional change**: no scoring semantics, no pass/fail behavior, no dispatch-decision changes.
- Env knobs exactly as specced: `EVOLVE_EVIDENCE_CAP` (default `6000`, `0` disables), `EVOLVE_MANIFEST_MODEL` (default `claude-haiku-4-5-20251001`).
- The manifest's structured `Status` / `Feature States` sections are ALWAYS recomputed — only the LLM summary is cached.
- Tests: tmp-dir pattern, no network — `_haiku_summarize` / `anthropic` are always monkeypatched or fall back deterministically.
- Run tests with `python3 -m pytest`; commit AND push after every task (project CLAUDE.md rule).

---

### Task 1: Bugfix — build_manifest must not hold build_lock

**Files:**
- Modify: `prepare.py` (`build_manifest`, the probe at ~line 771)
- Test: `tests/test_prepare.py` (append)

**Interfaces:**
- Consumes: `acquire_build_lock(evolve_dir) -> {"acquired", "reason", "feature", "token"}`, `release_build_lock(evolve_dir, token)` (existing).
- Produces: `build_manifest()` still returns the manifest string with a `build_lock: free|locked (...)` status line, but never leaves the lock held.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_prepare.py`:

```python
def test_build_manifest_does_not_leak_build_lock(tmp_path, monkeypatch):
    import prepare as prepare_mod
    monkeypatch.setattr(prepare_mod, "_haiku_summarize",
                        lambda status, files: "(stub summary)")
    evolve = tmp_path / ".evolve"
    evolve.mkdir()
    (evolve / "spec.md").write_text("- [ ] F01\n")
    (evolve / "results.tsv").write_text("\t".join(HEADER_FIELDS) + "\n")

    manifest = build_manifest(str(evolve))
    assert "build_lock: free" in manifest
    # The probe must not leave the lock held
    bl = acquire_build_lock(str(evolve))
    assert bl["acquired"] is True
    release_build_lock(str(evolve), bl["token"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_prepare.py -v -k leak_build_lock`
Expected: FAIL — second `acquire_build_lock` returns `acquired: False` (lock leaked by the probe)

- [ ] **Step 3: Implement**

In `prepare.py`, `build_manifest`, replace:

```python
    # Build lock status
    bl = acquire_build_lock(evolve_dir)
    build_lock_status = "free" if bl["acquired"] else f"locked ({bl['reason']})"
```

with:

```python
    # Build lock status — probe WITHOUT holding: acquiring for the status
    # line and never releasing poisoned merges for BUILD_LOCK_STALE_SECONDS.
    bl = acquire_build_lock(evolve_dir)
    if bl["acquired"]:
        release_build_lock(evolve_dir, bl["token"])
        build_lock_status = "free"
    else:
        build_lock_status = f"locked ({bl['reason']})"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_prepare.py -v -k leak_build_lock`
Expected: PASS. Then full suite: `python3 -m pytest tests/ -q` — all pass.

- [ ] **Step 5: Commit and push**

```bash
git add prepare.py tests/test_prepare.py
git commit -m "fix(manifest): build-lock probe releases immediately — leak poisoned merges for 30min"
git push
```

---

### Task 2: Evidence truncation in prepare_dispatch

**Files:**
- Modify: `prepare.py` (constants near line 50; the `## Previous Round Evidence` block in `prepare_dispatch`, ~line 969)
- Test: `tests/test_prepare.py` (append)

**Interfaces:**
- Produces: module constant `EVIDENCE_CAP = int(os.environ.get("EVOLVE_EVIDENCE_CAP", "6000"))`; helper `_truncate_evidence(content: str, cap: int) -> str` (module-private, testable).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prepare.py`:

```python
def test_truncate_evidence_over_cap():
    from prepare import _truncate_evidence
    content = "H" * 2000 + "M" * 20000 + "T" * 6000
    out = _truncate_evidence(content, 6000)
    assert out.startswith("H" * 1000)
    assert out.endswith("T" * 5000)
    assert "truncated" in out
    assert str(len(content) - 6000) in out


def test_truncate_evidence_under_cap_untouched():
    from prepare import _truncate_evidence
    content = "short evidence"
    assert _truncate_evidence(content, 6000) == content


def test_truncate_evidence_cap_zero_disables():
    from prepare import _truncate_evidence
    content = "X" * 50000
    assert _truncate_evidence(content, 0) == content


def test_prepare_dispatch_truncates_previous_evidence(tmp_path):
    evolve = tmp_path / ".evolve"
    feat_dir = evolve / "F01"
    feat_dir.mkdir(parents=True)
    (evolve / "program.md").write_text("# Program\ngoal\n")
    (feat_dir / "eval_codex.md").write_text("A" * 1000 + "B" * 20000 +
                                            "Z" * 5000)
    path = prepare_dispatch(str(evolve), "C", ["program.md"], feature="F01")
    content = Path(path).read_text()
    assert "truncated" in content
    assert "B" * 20000 not in content        # middle removed
    assert content.rstrip().endswith("Z" * 5000)  # tail kept (evidence is last)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_prepare.py -v -k truncate`
Expected: FAIL with `ImportError: cannot import name '_truncate_evidence'`

- [ ] **Step 3: Implement**

In `prepare.py`, near the model constants (~line 50), add:

```python
# Previous Round Evidence cap (chars). 0 disables truncation.
EVIDENCE_CAP = int(os.environ.get("EVOLVE_EVIDENCE_CAP", "6000"))
```

Above `prepare_dispatch`, add:

```python
def _truncate_evidence(content: str, cap: int) -> str:
    """Head+tail truncation for previous-round judge output.

    Judge files put dimension scores at the head and conclusions/rationale
    at the tail; the middle is process transcript. Keep the first 1,000
    chars + the last (cap - 1000), with an explicit marker. cap <= 0
    disables truncation.
    """
    if cap <= 0 or len(content) <= cap:
        return content
    head, tail = content[:1000], content[-(cap - 1000):]
    return (f"{head}\n\n[... truncated {len(content) - cap} chars ...]\n\n"
            f"{tail}")
```

In `prepare_dispatch`'s Previous Round Evidence block, change
`f"{prev_eval.read_text()}\n"` to
`f"{_truncate_evidence(prev_eval.read_text(), EVIDENCE_CAP)}\n"`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all PASS (existing evidence tests use small files — unaffected)

- [ ] **Step 5: Commit and push**

```bash
git add prepare.py tests/test_prepare.py
git commit -m "feat(tokens): cap Previous Round Evidence at EVOLVE_EVIDENCE_CAP chars"
git push
```

---

### Task 3: Manifest summary caching

**Files:**
- Modify: `prepare.py` (`build_manifest`, around the `summary = _haiku_summarize(...)` call at ~line 832)
- Test: `tests/test_prepare.py` (append)

**Interfaces:**
- Consumes: `_haiku_summarize(status_text, raw_files) -> str` (existing).
- Produces: cache file `.evolve/manifest_summary.json` with `{"fingerprint": str, "summary": str}`. Manifest structure/content otherwise unchanged; `Status`/`Feature States` always fresh.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prepare.py`:

```python
def _manifest_env(tmp_path):
    evolve = tmp_path / ".evolve"
    evolve.mkdir()
    (evolve / "spec.md").write_text("- [ ] F01\n")
    (evolve / "results.tsv").write_text(
        "\t".join(HEADER_FIELDS) + "\n"
        "a\tbuild\tF01\t-\t-\tkeep\tbuilt\t-\n")
    return str(evolve)


def test_manifest_summary_cached_on_unchanged_inputs(tmp_path, monkeypatch):
    import prepare as prepare_mod
    evolve = _manifest_env(tmp_path)
    calls = []
    monkeypatch.setattr(prepare_mod, "_haiku_summarize",
                        lambda s, f: calls.append(1) or "summary v1")
    build_manifest(evolve)
    assert len(calls) == 1

    def _boom(s, f):
        raise AssertionError("summarizer must not be called on cache hit")
    monkeypatch.setattr(prepare_mod, "_haiku_summarize", _boom)
    manifest = build_manifest(evolve)          # unchanged inputs
    assert "summary v1" in manifest            # cached summary reused


def test_manifest_summary_invalidated_on_input_change(tmp_path, monkeypatch):
    import prepare as prepare_mod
    evolve = _manifest_env(tmp_path)
    monkeypatch.setattr(prepare_mod, "_haiku_summarize",
                        lambda s, f: "summary v1")
    build_manifest(evolve)
    # new results.tsv row changes round -> fingerprint miss
    append_result(str(Path(evolve) / "results.tsv"), {
        "commit": "b", "phase": "eval", "feature": "F01",
        "scores": "7/7", "total": "7.0", "status": "fail", "summary": "r1"})
    monkeypatch.setattr(prepare_mod, "_haiku_summarize",
                        lambda s, f: "summary v2")
    manifest = build_manifest(evolve)
    assert "summary v2" in manifest


def test_manifest_status_fresh_despite_summary_cache(tmp_path, monkeypatch):
    import prepare as prepare_mod
    evolve = _manifest_env(tmp_path)
    monkeypatch.setattr(prepare_mod, "_haiku_summarize",
                        lambda s, f: "cached summary")
    m1 = build_manifest(evolve)
    assert "build_lock: free" in m1
    # lock state changes WITHOUT any file input changing
    bl = acquire_build_lock(evolve)
    m2 = build_manifest(evolve)                # summary cache hit
    assert "cached summary" in m2
    assert "build_lock: locked" in m2          # Status recomputed fresh
    release_build_lock(evolve, bl["token"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_prepare.py -v -k manifest_summary or -k status_fresh`
Use: `python3 -m pytest tests/test_prepare.py -v -k "manifest_summary or status_fresh"`
Expected: cache tests FAIL (`_boom` raises / "summary v1" regenerated as v2 everywhere); status_fresh may pass pre-change — note which.

- [ ] **Step 3: Implement**

In `prepare.py`, `build_manifest`, replace:

```python
    # Call Haiku
    summary = _haiku_summarize(status_text, raw_files)
```

with:

```python
    # Summary caching: the summary narrates raw_files + (round, phase,
    # feature); volatile lock/timing state never enters its inputs. The
    # structured sections above are ALWAYS recomputed — only the LLM call
    # is skipped on a fingerprint hit.
    fingerprint_src = json.dumps({
        "round": progress["total_iterations"],
        "phase": progress["phase"],
        "feature": feature,
        "raw": raw_files,
    }, sort_keys=True)
    fingerprint = hashlib.sha256(fingerprint_src.encode()).hexdigest()

    cache_path = evolve_path / "manifest_summary.json"
    summary = None
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("fingerprint") == fingerprint:
                summary = cached.get("summary")
        except (json.JSONDecodeError, OSError):
            pass
    if summary is None:
        summary = _haiku_summarize(status_text, raw_files)
        try:
            cache_path.write_text(json.dumps(
                {"fingerprint": fingerprint, "summary": summary}))
        except OSError:
            pass
```

Add `import hashlib` to the stdlib imports at the top of `prepare.py`
(json/os are already imported).

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all PASS. Note: any pre-existing `build_manifest` test that
monkeypatches `_haiku_summarize` and calls twice in one tmp dir now hits
the cache — check `tests/test_prepare.py` for such tests and, if one
asserts a second summarize call, update it to reflect intended caching
(document in the report).

- [ ] **Step 5: Commit and push**

```bash
git add prepare.py tests/test_prepare.py
git commit -m "feat(tokens): fingerprint-cache the manifest summary; structured sections stay fresh"
git push
```

---

### Task 4: Route the manifest summary to a small model

**Files:**
- Modify: `prepare.py` (constants ~line 50; `_haiku_summarize` model arg at ~line 729)
- Test: `tests/test_prepare.py` (append)

**Interfaces:**
- Produces: `MANIFEST_MODEL = os.environ.get("EVOLVE_MANIFEST_MODEL", "claude-haiku-4-5-20251001")`; `_haiku_summarize` calls `client.messages.create(model=MANIFEST_MODEL, ...)`. `HELPER_MODEL`/`HAIKU_MODEL` untouched.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prepare.py`:

```python
def test_manifest_model_default():
    from prepare import MANIFEST_MODEL
    assert MANIFEST_MODEL == "claude-haiku-4-5-20251001"


def test_haiku_summarize_uses_manifest_model(monkeypatch):
    import sys, types
    import prepare as prepare_mod
    captured = {}

    class _FakeMsg:
        content = [types.SimpleNamespace(text="fake summary")]

    class _FakeClient:
        def __init__(self, timeout=None):
            self.messages = types.SimpleNamespace(
                create=lambda **kw: captured.update(kw) or _FakeMsg())

    fake_anthropic = types.SimpleNamespace(Anthropic=_FakeClient)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    out = prepare_mod._haiku_summarize("status", {"f": "content"})
    assert out == "fake summary"
    assert captured["model"] == prepare_mod.MANIFEST_MODEL
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_prepare.py -v -k manifest_model or -k uses_manifest_model`
Use: `python3 -m pytest tests/test_prepare.py -v -k "manifest_model"`
Expected: FAIL — `MANIFEST_MODEL` undefined; captured model equals the Sonnet HELPER_MODEL

- [ ] **Step 3: Implement**

In `prepare.py`, below the `HAIKU_MODEL` alias (~line 49), add:

```python
# Manifest summary is a <=300-token compression job — small-model work.
# H's own agent stays on HELPER_MODEL; only this one call routes down.
MANIFEST_MODEL = os.environ.get(
    "EVOLVE_MANIFEST_MODEL", "claude-haiku-4-5-20251001")
```

In `_haiku_summarize`, change `model=HAIKU_MODEL,` to `model=MANIFEST_MODEL,`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all PASS

- [ ] **Step 5: Commit and push**

```bash
git add prepare.py tests/test_prepare.py
git commit -m "feat(tokens): route manifest summary to EVOLVE_MANIFEST_MODEL (haiku default)"
git push
```

---

### Task 5: Cache-friendly dispatch section ordering

**Files:**
- Modify: `prepare.py` (`prepare_dispatch`, ~lines 940-983)
- Test: `tests/test_prepare.py` (append)

**Interfaces:**
- Produces: constant `STABLE_DISPATCH_FILES = {"program.md", "eval.yml", "spec.md", "adapter.py"}`. Section order: header → stable files (relative order kept) → other files (relative order kept) → `## Note from O` → `## Previous Round Evidence`. Stability judged on the parsed filename (before `:range`/`#section`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prepare.py`:

```python
def test_dispatch_stable_files_first_note_after(tmp_path):
    evolve = tmp_path / ".evolve"
    (evolve / "F01").mkdir(parents=True)
    (evolve / "program.md").write_text("PROGRAM-CONTENT")
    (evolve / "F01" / "strategy.md").write_text("STRATEGY-CONTENT")
    path = prepare_dispatch(str(evolve), "B",
                            ["F01/strategy.md", "program.md"],
                            note="VOLATILE-NOTE", feature="F01")
    content = Path(path).read_text()
    # stable file first even though listed second
    assert content.index("PROGRAM-CONTENT") < content.index("STRATEGY-CONTENT")
    # volatile note after ALL file sections
    assert content.index("VOLATILE-NOTE") > content.index("STRATEGY-CONTENT")


def test_dispatch_evidence_stays_last(tmp_path):
    evolve = tmp_path / ".evolve"
    feat = evolve / "F01"
    feat.mkdir(parents=True)
    (evolve / "program.md").write_text("PROGRAM-CONTENT")
    (feat / "eval_codex.md").write_text("EVIDENCE-CONTENT")
    path = prepare_dispatch(str(evolve), "C", ["program.md"],
                            note="VOLATILE-NOTE", feature="F01")
    content = Path(path).read_text()
    assert content.index("VOLATILE-NOTE") > content.index("PROGRAM-CONTENT")
    assert content.index("EVIDENCE-CONTENT") > content.index("VOLATILE-NOTE")


def test_dispatch_stability_ignores_section_suffix(tmp_path):
    evolve = tmp_path / ".evolve"
    (evolve / "F01").mkdir(parents=True)
    (evolve / "program.md").write_text("# A\nSECTION-A\n# B\nSECTION-B\n")
    (evolve / "F01" / "strategy.md").write_text("STRATEGY-CONTENT")
    path = prepare_dispatch(str(evolve), "B",
                            ["F01/strategy.md", "program.md#A"],
                            feature="F01")
    content = Path(path).read_text()
    # program.md#A parses to program.md -> stable -> first
    assert content.index("SECTION-A") < content.index("STRATEGY-CONTENT")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_prepare.py -v -k "stable_files or stays_last or ignores_section"`
Expected: first and third FAIL (note currently precedes files; listed order preserved); `stays_last` may already pass — note which.

- [ ] **Step 3: Implement**

In `prepare.py`, add near `EVIDENCE_CAP`:

```python
# Dispatch section ordering: stable-content-first is prompt-cache
# friendly (a volatile first section invalidates any prefix cache at
# byte 1). Stability judged on the parsed filename's basename.
STABLE_DISPATCH_FILES = {"program.md", "eval.yml", "spec.md", "adapter.py"}
```

In `prepare_dispatch`, restructure the assembly. Replace the current
`sections = [...]` / `if note:` / `for file_spec in file_list:` flow with:

```python
    def _is_stable(file_spec: str) -> bool:
        filename, _ = _parse_file_spec(file_spec)
        return Path(filename).name in STABLE_DISPATCH_FILES

    ordered_specs = ([s for s in file_list if _is_stable(s)] +
                     [s for s in file_list if not _is_stable(s)])

    sections = [f"# Dispatch: {target}\n"]

    for file_spec in ordered_specs:
        # ... (the existing per-file body unchanged: parse, read,
        #      slice/truncate, append "## {file_spec}\n{content}\n")

    # Volatile sections LAST (cache-friendly ordering)
    if note:
        sections.append(f"## Note from O\n{note}\n")
```

Keep the existing `## Previous Round Evidence` block (C only) after the
note block, so evidence remains the final section. The per-file loop body
is unchanged — only the iteration order and the note placement move.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all PASS. The Task-5b evidence tests assert content presence,
not order relative to note — they keep passing.

- [ ] **Step 5: Commit and push**

```bash
git add prepare.py tests/test_prepare.py
git commit -m "feat(tokens): stable-first dispatch ordering, volatile note/evidence last"
git push
```

---

### Task 6: Agent-contract docs — judge output contract + mentor input budget

**Files:**
- Modify: `agents/critic.md` (after the Pairwise verdicts section), `agents/mentor.md` (new section near the top, after the role description)

No unit tests (agent-contract docs). Make exactly these edits:

- [ ] **Step 1: agents/critic.md — judge output contract**

Insert after the "### Pairwise verdicts" section:

```markdown
### Judge Output Contract (mandatory)

The judge's written output is billed twice — once as output, once as the
next round's Previous Round Evidence input. Keep it structured and short:

- exactly one line per dimension: `<dimension>: <score> — <rationale, ≤30 words>`
- then one pairwise block, one line per dimension:
  `<dimension>: better|same|worse — <one-line basis>`
- then at most 3 summary lines
- NEVER transcribe conversation content or paste raw logs — reference the
  evidence file path instead (detailed grounds already live in the
  evidence directory)

Embed this contract in the Evaluator Prompt when invoking the judge CLI.
```

- [ ] **Step 2: agents/mentor.md — input budget**

Insert a new section after the role/overview block at the top:

```markdown
## Input Budget (mandatory)

Three Opus mentors fire hourly; unbounded reads dominate late-session
cost. Hard caps:

- results.tsv: read ONLY the last 30 rows (`tail -30 .evolve/results.tsv`);
  summarize anything older as one line ("N earlier rounds, M passes")
- evidence / log files: ≤2,000 chars per file, prefer the tail
- git history: `git log --oneline -20` — no per-commit diffs
- each META report you write: ≤60 lines

Cross-window conclusions survive via the META closed loop (your prior
advice + its measured consequences come back to you) — you do not need
raw history beyond the window.
```

- [ ] **Step 3: Verify docs and suite**

Run: `grep -n "Judge Output Contract" agents/critic.md && grep -n "Input Budget" agents/mentor.md`
Expected: both found once.
Run: `python3 -m pytest tests/ -q`
Expected: all PASS (docs don't affect tests).

- [ ] **Step 4: Commit and push**

```bash
git add agents/critic.md agents/mentor.md
git commit -m "docs(tokens): judge output contract + mentor input budget"
git push
```
