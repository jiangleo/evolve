import json, os, tempfile, time, pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from prepare import (append_result, read_progress, HEADER_FIELDS,
                     generate_report, acquire_lock, update_lock, release_lock,
                     load_eval_config, load_adapter,
                     analyze_trajectory, should_stop, validate_eval_result,
                     get_evaluator, HARD_LIMITS, INDEPENDENT_EVALUATORS,
                     build_manifest, prepare_dispatch, _find_current_feature,
                     _parse_uncompleted_features,
                     _parse_file_spec, _extract_section,
                     scan_all_features, acquire_build_lock, release_build_lock,
                     acquire_feature_lock, release_feature_lock,
                     BUILD_LOCK_STALE_SECONDS)

def test_append_result_creates_header():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
        path = f.name
    try:
        append_result(path, {
            "commit": "a1b2c3d", "phase": "plan", "feature": "-",
            "scores": "-", "total": "-", "status": "keep",
            "summary": "initial spec"
        })
        lines = Path(path).read_text().strip().split('\n')
        assert len(lines) == 2
        assert lines[0] == '\t'.join(HEADER_FIELDS)
        assert "a1b2c3d" in lines[1]
    finally:
        os.unlink(path)

def test_append_result_preserves_existing():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
        f.write('\t'.join(HEADER_FIELDS) + '\n')
        f.write('a1b2c3d\tplan\t-\t-\t-\tkeep\tinitial\n')
        path = f.name
    try:
        append_result(path, {
            "commit": "b2c3d4e", "phase": "build", "feature": "auth",
            "scores": "-", "total": "-", "status": "keep",
            "summary": "JWT auth"
        })
        lines = Path(path).read_text().strip().split('\n')
        assert len(lines) == 3
    finally:
        os.unlink(path)

def test_append_crash_row():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
        f.write('\t'.join(HEADER_FIELDS) + '\n')
        path = f.name
    try:
        append_result(path, {
            "commit": "d4e5f6g", "phase": "build", "feature": "chat",
            "scores": "0/0/0/0/0", "total": "0", "status": "crash",
            "summary": "websocket OOM"
        })
        lines = Path(path).read_text().strip().split('\n')
        assert "0/0/0/0/0" in lines[1]
    finally:
        os.unlink(path)


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


def _make_tsv(rows):
    """Helper: create a temp TSV with header + rows."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False) as f:
        f.write('\t'.join(HEADER_FIELDS) + '\n')
        for row in rows:
            f.write('\t'.join(str(row.get(h, '-')) for h in HEADER_FIELDS) + '\n')
        return f.name

def test_read_progress_empty():
    path = _make_tsv([])
    try:
        p = read_progress(path)
        assert p["phase"] == "init"
        assert p["total_iterations"] == 0
    finally:
        os.unlink(path)

def test_read_progress_after_plan():
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"}
    ])
    try:
        p = read_progress(path)
        assert p["phase"] == "build"
        assert p["current_feature"] is None
    finally:
        os.unlink(path)

def test_read_progress_after_build_keep():
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "JWT auth"}
    ])
    try:
        p = read_progress(path)
        assert p["phase"] == "eval"
        assert p["current_feature"] == "auth"
    finally:
        os.unlink(path)

def test_read_progress_consecutive_fails():
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build auth"},
        {"commit": "c3d", "phase": "eval", "feature": "auth", "scores": "5/8/6/4/7",
         "total": "6", "status": "fail", "summary": "E2E fail"},
        {"commit": "d4e", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "fix"},
        {"commit": "e5f", "phase": "eval", "feature": "auth", "scores": "5/8/6/4/7",
         "total": "6", "status": "fail", "summary": "still fail"},
    ])
    try:
        p = read_progress(path)
        assert p["phase"] == "build"
        assert p["consecutive_fails"] == 2
        assert p["current_feature"] == "auth"
    finally:
        os.unlink(path)

def test_read_progress_eval_pass_moves_to_next():
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
        {"commit": "c3d", "phase": "eval", "feature": "auth", "scores": "8/9/8/8/7",
         "total": "8", "status": "pass", "summary": "all pass"},
    ])
    try:
        p = read_progress(path)
        assert p["phase"] == "build"
        assert p["completed_features"] == ["auth"]
        assert p["last_pass_commit"] == "c3d"
    finally:
        os.unlink(path)

def test_read_progress_all_done():
    """V2: all features pass (no skip in V2)."""
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
        {"commit": "c3d", "phase": "eval", "feature": "auth", "scores": "8/9/8/8/7",
         "total": "8", "status": "pass", "summary": "pass"},
    ])
    try:
        p = read_progress(path)
        assert p["phase"] == "build"
        assert p["completed_features"] == ["auth"]
    finally:
        os.unlink(path)

def test_read_progress_consecutive_crashes():
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "chat", "scores": "-",
         "total": "-", "status": "crash", "summary": "OOM"},
        {"commit": "c3d", "phase": "build", "feature": "chat", "scores": "-",
         "total": "-", "status": "crash", "summary": "OOM again"},
        {"commit": "d4e", "phase": "build", "feature": "chat", "scores": "-",
         "total": "-", "status": "crash", "summary": "OOM third"},
    ])
    try:
        p = read_progress(path)
        assert p["phase"] == "build"
        assert p["consecutive_crashes"] == 3
        assert p["current_feature"] == "chat"
    finally:
        os.unlink(path)

def test_read_progress_has_been_reset():
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
        {"commit": "c3d", "phase": "eval", "feature": "auth", "scores": "3/4/3/3/3",
         "total": "3.2", "status": "fail", "summary": "fail 1"},
        {"commit": "d4e", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "reset", "summary": "reset to base"},
        {"commit": "e5f", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "retry"},
        {"commit": "f6g", "phase": "eval", "feature": "auth", "scores": "3/4/3/3/3",
         "total": "3.2", "status": "fail", "summary": "fail after reset"},
    ])
    try:
        p = read_progress(path)
        assert p["has_been_reset"] is True
        assert p["consecutive_fails"] == 2
    finally:
        os.unlink(path)

def test_read_progress_no_reset():
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
        {"commit": "c3d", "phase": "eval", "feature": "auth", "scores": "5/6/5/5/5",
         "total": "5.2", "status": "fail", "summary": "fail"},
    ])
    try:
        p = read_progress(path)
        assert p["has_been_reset"] is False
    finally:
        os.unlink(path)

def test_read_progress_feature_iterations():
    """V2: feature_iterations counts all rows for current feature."""
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
        {"commit": "c3d", "phase": "eval", "feature": "auth", "scores": "5/6",
         "total": "5.5", "status": "fail", "summary": "fail"},
        {"commit": "d4e", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "fix"},
    ])
    try:
        p = read_progress(path)
        assert p["feature_iterations"] == 3  # build + eval + build for auth
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# generate_report tests
# ---------------------------------------------------------------------------

def test_generate_report_in_progress():
    """Report shows structured progress for an in-progress run."""
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
        {"commit": "c3d", "phase": "eval", "feature": "auth", "scores": "8/9/8",
         "total": "8.3", "status": "pass", "summary": "all pass"},
        {"commit": "d4e", "phase": "build", "feature": "chat", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
        {"commit": "e5f", "phase": "eval", "feature": "chat", "scores": "6/8/5",
         "total": "6.3", "status": "fail", "summary": "E2E fail"},
    ])
    try:
        report = generate_report(path)
        assert "# Evolve Progress" in report
        assert "In Progress" in report
        assert "\u2713 auth" in report  # checkmark auth
        assert "\u25b6 chat" in report  # triangle chat
        assert "1/2" in report  # 1 of 2 features completed
        assert "E2E fail" in report
    finally:
        os.unlink(path)


def test_generate_report_all_done():
    """Report shows completion when all features pass."""
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
        {"commit": "c3d", "phase": "eval", "feature": "auth", "scores": "8/9",
         "total": "8.5", "status": "pass", "summary": "pass"},
    ])
    try:
        report = generate_report(path)
        assert "# Evolve Progress" in report
        assert "\u2713 auth" in report
        assert "1/1" in report
    finally:
        os.unlink(path)


def test_generate_report_empty():
    """Report for empty results.tsv."""
    path = _make_tsv([])
    try:
        report = generate_report(path)
        assert "# Evolve Progress" in report
        assert "Waiting" in report
    finally:
        os.unlink(path)


def test_generate_report_multiple_features():
    """Report shows multiple features with different states."""
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
        {"commit": "c3d", "phase": "eval", "feature": "auth", "scores": "8/9",
         "total": "8.5", "status": "pass", "summary": "pass"},
        {"commit": "d4e", "phase": "build", "feature": "chat", "scores": "-",
         "total": "-", "status": "keep", "summary": "build chat"},
    ])
    try:
        report = generate_report(path)
        assert "\u2713 auth" in report
        assert "chat" in report
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Lock tests
# ---------------------------------------------------------------------------

def test_acquire_lock_fresh(tmp_path):
    result = acquire_lock(str(tmp_path))
    assert result["acquired"] is True
    lock_file = tmp_path / "lock"
    assert lock_file.exists()
    data = json.loads(lock_file.read_text())
    assert "heartbeat" in data
    release_lock(str(tmp_path))
    assert not lock_file.exists()

def test_acquire_lock_blocked_by_active(tmp_path):
    # First acquire
    acquire_lock(str(tmp_path))
    # Second acquire should be blocked (heartbeat is fresh)
    result = acquire_lock(str(tmp_path))
    assert result["acquired"] is False
    assert "Another session" in result["reason"]
    release_lock(str(tmp_path))

def test_acquire_lock_stale_takeover(tmp_path):
    # Write a stale lock (heartbeat 5 minutes ago)
    lock_file = tmp_path / "lock"
    lock_file.write_text(json.dumps({
        "pid": 99999, "started": time.time() - 300,
        "heartbeat": time.time() - 300, "phase": "build"
    }))
    # Should take over stale lock
    result = acquire_lock(str(tmp_path))
    assert result["acquired"] is True
    release_lock(str(tmp_path))

def test_update_lock_heartbeat(tmp_path):
    acquire_lock(str(tmp_path))
    update_lock(str(tmp_path), "eval", "auth")
    lock_file = tmp_path / "lock"
    data = json.loads(lock_file.read_text())
    assert data["phase"] == "eval"
    assert data["feature"] == "auth"
    release_lock(str(tmp_path))

def test_release_lock_idempotent(tmp_path):
    # Releasing a non-existent lock should not error
    release_lock(str(tmp_path))
    release_lock(str(tmp_path))

# ---------------------------------------------------------------------------
# eval.yml tests
# ---------------------------------------------------------------------------

def test_load_eval_config_basic(tmp_path):
    """Parse a standard eval.yml with mixed dimension types."""
    yml = tmp_path / "eval.yml"
    yml.write_text("""# Evaluation dimensions
dimensions:
  - name: Functional Completeness
    type: deterministic
    cmd: npm test
    threshold: 7.0
  - name: Code Quality
    type: llm-judged
    threshold: 7.0
  - name: Performance
    type: deterministic
    cmd: python .evolve/bench.py
    threshold: 8.0
""")
    dims = load_eval_config(str(yml))
    assert len(dims) == 3
    assert dims[0]["name"] == "Functional Completeness"
    assert dims[0]["type"] == "deterministic"
    assert dims[0]["cmd"] == "npm test"
    assert dims[0]["threshold"] == 7.0
    assert dims[1]["name"] == "Code Quality"
    assert dims[1]["type"] == "llm-judged"
    assert "cmd" not in dims[1]
    assert dims[1]["threshold"] == 7.0
    assert dims[2]["threshold"] == 8.0


def test_load_eval_config_missing_file():
    """Missing eval.yml raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_eval_config("/nonexistent/eval.yml")


def test_load_eval_config_defaults(tmp_path):
    """Dimensions without type/threshold get defaults."""
    yml = tmp_path / "eval.yml"
    yml.write_text("""dimensions:
  - name: Simple Dimension
""")
    dims = load_eval_config(str(yml))
    assert len(dims) == 1
    assert dims[0]["type"] == "llm-judged"
    assert dims[0]["threshold"] == 3.5


def test_load_eval_config_comments_and_blanks(tmp_path):
    """Comments and blank lines are ignored."""
    yml = tmp_path / "eval.yml"
    yml.write_text("""# This is a comment
dimensions:

  # Another comment
  - name: Dimension A
    type: deterministic
    cmd: pytest
    threshold: 8.0

  - name: Dimension B
    threshold: 6.0
""")
    dims = load_eval_config(str(yml))
    assert len(dims) == 2
    assert dims[0]["name"] == "Dimension A"
    assert dims[1]["name"] == "Dimension B"
    assert dims[1]["threshold"] == 6.0

def test_load_eval_config_scoring_rubric(tmp_path):
    """Parse scoring_rubric anchor points."""
    yml = tmp_path / "eval.yml"
    yml.write_text("""dimensions:
  - name: Content Quality
    type: llm-judged
    threshold: 7.0
    scoring_rubric:
      1: "content missing or irrelevant"
      5: "content mostly complete but gaps"
      8: "content complete, covers all requirements"
      10: "perfect, nothing to improve"
""")
    dims = load_eval_config(str(yml))
    assert len(dims) == 1
    assert "scoring_rubric" in dims[0]
    rubric = dims[0]["scoring_rubric"]
    assert rubric[1] == "content missing or irrelevant"
    assert rubric[8] == "content complete, covers all requirements"
    assert rubric[10] == "perfect, nothing to improve"


def test_load_eval_config_checks(tmp_path):
    """Parse deterministic checks list."""
    yml = tmp_path / "eval.yml"
    yml.write_text("""dimensions:
  - name: File Completeness
    type: deterministic
    threshold: 8.0
    checks:
      - config.yml exists and valid
      - README.md has usage section
      - tests/ directory has at least 3 files
""")
    dims = load_eval_config(str(yml))
    assert len(dims) == 1
    assert "checks" in dims[0]
    assert len(dims[0]["checks"]) == 3
    assert dims[0]["checks"][0] == "config.yml exists and valid"


def test_load_eval_config_description(tmp_path):
    """Parse multi-line description."""
    yml = tmp_path / "eval.yml"
    yml.write_text("""dimensions:
  - name: Code Quality
    type: llm-judged
    threshold: 7.0
    description: >
      Evaluate code readability, structure,
      and adherence to project conventions.
  - name: Tests
    type: deterministic
    threshold: 8.0
""")
    dims = load_eval_config(str(yml))
    assert len(dims) == 2
    assert "description" in dims[0]
    assert "readability" in dims[0]["description"]
    assert dims[1]["name"] == "Tests"


def test_load_eval_config_full_featured(tmp_path):
    """Parse eval.yml with all new fields together."""
    yml = tmp_path / "eval.yml"
    yml.write_text("""dimensions:
  - name: Product Design
    type: llm-judged
    threshold: 8.0
    description: >
      Evaluate the product design quality.
    scoring_rubric:
      5: "basic design"
      8: "good design"
      10: "exceptional design"
  - name: Implementation
    type: deterministic
    threshold: 8.0
    checks:
      - SKILL.md exists
      - frontmatter has required fields
""")
    dims = load_eval_config(str(yml))
    assert len(dims) == 2
    assert dims[0]["scoring_rubric"][8] == "good design"
    assert dims[0]["description"].strip().startswith("Evaluate")
    assert len(dims[1]["checks"]) == 2


# ---------------------------------------------------------------------------
# adapter loading tests
# ---------------------------------------------------------------------------

def test_load_adapter_from_path(tmp_path):
    """Load a project-specific adapter from file path."""
    adapter_file = tmp_path / "adapter.py"
    adapter_file.write_text("""
prerequisites = [
    {"name": "node", "check": "node --version"}
]

def setup(project_dir):
    return {"status": "ready", "info": {}, "error": None}

def run_checks(project_dir, feature):
    return {"scores": {}, "details": "no checks"}

def teardown(info):
    pass
""")
    adapter = load_adapter(str(adapter_file))
    assert hasattr(adapter, 'setup')
    assert hasattr(adapter, 'run_checks')
    assert hasattr(adapter, 'teardown')
    assert hasattr(adapter, 'prerequisites')
    assert len(adapter.prerequisites) == 1
    result = adapter.setup("/tmp")
    assert result["status"] == "ready"


def test_load_adapter_missing_file():
    """Missing adapter file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_adapter("/nonexistent/adapter.py")


def test_load_adapter_missing_functions(tmp_path):
    """Adapter missing required functions raises ValueError."""
    adapter_file = tmp_path / "adapter.py"
    adapter_file.write_text("""
def setup(project_dir):
    return {"status": "ready", "info": {}, "error": None}
# missing run_checks and teardown
""")
    with pytest.raises(ValueError, match="missing required functions"):
        load_adapter(str(adapter_file))


def test_load_adapter_no_prerequisites(tmp_path):
    """Adapter without prerequisites attribute gets empty default."""
    adapter_file = tmp_path / "adapter.py"
    adapter_file.write_text("""
def setup(project_dir):
    return {"status": "ready", "info": {}, "error": None}

def run_checks(project_dir, feature):
    return {"scores": {}, "details": ""}

def teardown(info):
    pass
""")
    adapter = load_adapter(str(adapter_file))
    assert adapter.prerequisites == []


# ---------------------------------------------------------------------------
# analyze_trajectory tests
# ---------------------------------------------------------------------------

def test_analyze_trajectory_insufficient():
    """Fewer than window eval rows returns insufficient."""
    path = _make_tsv([
        {"commit": "a1b", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b2c", "phase": "eval", "feature": "auth", "scores": "5",
         "total": "5", "status": "fail", "summary": "fail"},
    ])
    try:
        t = analyze_trajectory(path, "auth")
        assert t["trend"] == "insufficient"
        assert t["scores"] == [5.0]
        assert t["rounds"] == 1
    finally:
        os.unlink(path)


def test_analyze_trajectory_rising():
    """Scores going up by > 0.5 returns rising."""
    path = _make_tsv([
        {"commit": "a", "phase": "eval", "feature": "auth", "scores": "5",
         "total": "5.0", "status": "fail", "summary": "f"},
        {"commit": "b", "phase": "eval", "feature": "auth", "scores": "6",
         "total": "6.0", "status": "fail", "summary": "f"},
        {"commit": "c", "phase": "eval", "feature": "auth", "scores": "7",
         "total": "7.0", "status": "fail", "summary": "f"},
    ])
    try:
        t = analyze_trajectory(path, "auth")
        assert t["trend"] == "rising"
        assert t["scores"] == [5.0, 6.0, 7.0]
        assert t["latest"] == 7.0
    finally:
        os.unlink(path)


def test_analyze_trajectory_falling():
    """Scores going down by > 0.5 returns falling."""
    path = _make_tsv([
        {"commit": "a", "phase": "eval", "feature": "auth", "scores": "8",
         "total": "8.0", "status": "fail", "summary": "f"},
        {"commit": "b", "phase": "eval", "feature": "auth", "scores": "7",
         "total": "7.0", "status": "fail", "summary": "f"},
        {"commit": "c", "phase": "eval", "feature": "auth", "scores": "6",
         "total": "6.0", "status": "fail", "summary": "f"},
    ])
    try:
        t = analyze_trajectory(path, "auth")
        assert t["trend"] == "falling"
        assert t["latest"] == 6.0
    finally:
        os.unlink(path)


def test_analyze_trajectory_flat():
    """Scores within ±0.5 returns flat."""
    path = _make_tsv([
        {"commit": "a", "phase": "eval", "feature": "auth", "scores": "6",
         "total": "6.0", "status": "fail", "summary": "f"},
        {"commit": "b", "phase": "eval", "feature": "auth", "scores": "6.2",
         "total": "6.2", "status": "fail", "summary": "f"},
        {"commit": "c", "phase": "eval", "feature": "auth", "scores": "6.3",
         "total": "6.3", "status": "fail", "summary": "f"},
    ])
    try:
        t = analyze_trajectory(path, "auth")
        assert t["trend"] == "flat"
    finally:
        os.unlink(path)


def test_analyze_trajectory_ignores_build_rows():
    """Only eval rows are counted, build rows ignored."""
    path = _make_tsv([
        {"commit": "a", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
        {"commit": "b", "phase": "eval", "feature": "auth", "scores": "5",
         "total": "5.0", "status": "fail", "summary": "f"},
        {"commit": "c", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "fix"},
        {"commit": "d", "phase": "eval", "feature": "auth", "scores": "6",
         "total": "6.0", "status": "fail", "summary": "f"},
    ])
    try:
        t = analyze_trajectory(path, "auth")
        assert t["trend"] == "insufficient"  # only 2 eval rows, window=3
        assert t["rounds"] == 2
    finally:
        os.unlink(path)


def test_analyze_trajectory_filters_by_feature():
    """Only rows matching the requested feature are counted."""
    path = _make_tsv([
        {"commit": "a", "phase": "eval", "feature": "auth", "scores": "5",
         "total": "5.0", "status": "fail", "summary": "f"},
        {"commit": "b", "phase": "eval", "feature": "chat", "scores": "8",
         "total": "8.0", "status": "pass", "summary": "p"},
        {"commit": "c", "phase": "eval", "feature": "auth", "scores": "6",
         "total": "6.0", "status": "fail", "summary": "f"},
        {"commit": "d", "phase": "eval", "feature": "auth", "scores": "7",
         "total": "7.0", "status": "pass", "summary": "p"},
    ])
    try:
        t = analyze_trajectory(path, "auth")
        assert t["trend"] == "rising"
        assert t["scores"] == [5.0, 6.0, 7.0]
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# should_stop tests
# ---------------------------------------------------------------------------

def test_should_stop_false_normal():
    """Normal progress should not stop."""
    path = _make_tsv([
        {"commit": "a", "phase": "plan", "feature": "-", "scores": "-",
         "total": "-", "status": "keep", "summary": "spec"},
        {"commit": "b", "phase": "build", "feature": "auth", "scores": "-",
         "total": "-", "status": "keep", "summary": "build"},
    ])
    try:
        stop, reason = should_stop(path, "auth")
        assert stop is False
        assert reason == ""
    finally:
        os.unlink(path)


def test_should_stop_max_rounds_total():
    """Stops when total iterations exceed max_rounds_total."""
    rows = [{"commit": "a", "phase": "plan", "feature": "-", "scores": "-",
             "total": "-", "status": "keep", "summary": "spec"}]
    for i in range(100):
        rows.append({"commit": f"c{i}", "phase": "build", "feature": "auth",
                      "scores": "-", "total": "-", "status": "keep",
                      "summary": f"build {i}"})
    path = _make_tsv(rows)
    try:
        stop, reason = should_stop(path, "auth")
        assert stop is True
        assert "Total round limit" in reason
    finally:
        os.unlink(path)


def test_should_stop_consecutive_crashes():
    """Stops after max consecutive crashes."""
    rows = [{"commit": "a", "phase": "plan", "feature": "-", "scores": "-",
             "total": "-", "status": "keep", "summary": "spec"}]
    for i in range(5):
        rows.append({"commit": f"c{i}", "phase": "build", "feature": "auth",
                      "scores": "-", "total": "0", "status": "crash",
                      "summary": f"crash {i}"})
    path = _make_tsv(rows)
    try:
        stop, reason = should_stop(path, "auth")
        assert stop is True
        assert "consecutive crashes" in reason
    finally:
        os.unlink(path)


def test_should_stop_consecutive_fails():
    """Stops after max consecutive eval failures."""
    rows = [{"commit": "a", "phase": "plan", "feature": "-", "scores": "-",
             "total": "-", "status": "keep", "summary": "spec"}]
    for i in range(10):
        rows.append({"commit": f"b{i}", "phase": "build", "feature": "auth",
                      "scores": "-", "total": "-", "status": "keep",
                      "summary": f"build {i}"})
        rows.append({"commit": f"e{i}", "phase": "eval", "feature": "auth",
                      "scores": "5", "total": "5", "status": "fail",
                      "summary": f"fail {i}"})
    path = _make_tsv(rows)
    try:
        stop, reason = should_stop(path, "auth")
        assert stop is True
        assert "consecutive eval failures" in reason
    finally:
        os.unlink(path)


def test_should_stop_max_rounds_per_feature():
    """Stops when a single feature exceeds max_rounds_per_feature."""
    rows = [{"commit": "a", "phase": "plan", "feature": "-", "scores": "-",
             "total": "-", "status": "keep", "summary": "spec"}]
    for i in range(30):
        rows.append({"commit": f"b{i}", "phase": "build", "feature": "auth",
                      "scores": "-", "total": "-", "status": "keep",
                      "summary": f"build {i}"})
    path = _make_tsv(rows)
    try:
        stop, reason = should_stop(path, "auth")
        assert stop is True
        assert "per-feature round limit" in reason
    finally:
        os.unlink(path)


def test_should_stop_flat_after_pivot():
    """Stops when trajectory is flat and pivots exceed max_flat_after_pivot.

    Note: pivots_on_this_feature defaults to 0 in current read_progress().
    This test verifies the code path exists and works when pivots are tracked.
    """
    # Build rows with flat trajectory (scores within ±0.5)
    rows = [{"commit": "a", "phase": "plan", "feature": "-", "scores": "-",
             "total": "-", "status": "keep", "summary": "spec"}]
    for i in range(5):
        rows.append({"commit": f"b{i}", "phase": "build", "feature": "auth",
                      "scores": "-", "total": "-", "status": "keep",
                      "summary": f"build {i}"})
        rows.append({"commit": f"e{i}", "phase": "eval", "feature": "auth",
                      "scores": "6", "total": "6.0", "status": "fail",
                      "summary": f"fail {i}"})
    path = _make_tsv(rows)
    try:
        # With default pivots=0, should NOT stop on flat alone
        stop, reason = should_stop(path, "auth")
        assert "still no improvement" not in reason
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# validate_eval_result tests
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# get_evaluator tests
# ---------------------------------------------------------------------------

def test_get_evaluator_returns_string_or_none():
    """get_evaluator returns a string (if available) or None."""
    result = get_evaluator()
    assert result is None or isinstance(result, str)


def test_get_evaluator_priority_order():
    """get_evaluator tries evaluators in INDEPENDENT_EVALUATORS order."""
    # The result should be the first available from the list
    result = get_evaluator()
    if result is not None:
        # It should be one of the known evaluators
        assert result in INDEPENDENT_EVALUATORS
        # It should be the first available one in priority order
        import shutil
        for name in INDEPENDENT_EVALUATORS:
            if shutil.which(name) is not None:
                assert result == name, f"Expected {name} (first available) but got {result}"
                break


# ---------------------------------------------------------------------------
# constants tests
# ---------------------------------------------------------------------------

def test_hard_limits_keys():
    """HARD_LIMITS has all required keys."""
    expected = {"max_rounds_total", "max_rounds_per_feature",
                "max_consecutive_crashes", "max_consecutive_fails",
                "max_flat_after_pivot", "max_runtime_hours",
                "max_branching_rounds_per_feature", "candidates_per_branching"}
    assert set(HARD_LIMITS.keys()) == expected


def test_hard_limits_values():
    """HARD_LIMITS values match the V2 design spec."""
    assert HARD_LIMITS["max_rounds_total"] == 100
    assert HARD_LIMITS["max_rounds_per_feature"] == 30
    assert HARD_LIMITS["max_consecutive_crashes"] == 5
    assert HARD_LIMITS["max_consecutive_fails"] == 10
    assert HARD_LIMITS["max_flat_after_pivot"] == 3
    assert HARD_LIMITS["max_runtime_hours"] == 24


def test_should_stop_runtime_limit():
    """Stops when started_at timestamp exceeds max_runtime_hours."""
    rows = [{"commit": "a", "phase": "plan", "feature": "-", "scores": "-",
             "total": "-", "status": "keep", "summary": "spec"},
            {"commit": "b", "phase": "build", "feature": "auth", "scores": "-",
             "total": "-", "status": "keep", "summary": "build"}]
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "results.tsv")
        with open(path, 'w') as f:
            f.write('\t'.join(HEADER_FIELDS) + '\n')
            for row in rows:
                f.write('\t'.join(row.get(h, '') for h in HEADER_FIELDS) + '\n')
        # Write started_at with timestamp 25 hours ago
        started_at = os.path.join(tmpdir, "started_at")
        with open(started_at, 'w') as f:
            f.write(str(time.time() - 25 * 3600))
        stop, reason = should_stop(path, "auth")
        assert stop is True
        assert "Runtime limit" in reason


def test_acquire_lock_creates_started_at():
    """First acquire_lock writes started_at file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = acquire_lock(tmpdir)
        assert result["acquired"] is True
        started_at = os.path.join(tmpdir, "started_at")
        assert os.path.exists(started_at)
        ts = float(open(started_at).read().strip())
        assert time.time() - ts < 5  # created just now
        release_lock(tmpdir)


def test_acquire_lock_preserves_started_at():
    """Subsequent acquire_lock does not overwrite started_at."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # First lock
        acquire_lock(tmpdir)
        started_at = os.path.join(tmpdir, "started_at")
        original_ts = open(started_at).read().strip()
        release_lock(tmpdir)
        # Second lock
        time.sleep(0.01)
        acquire_lock(tmpdir)
        assert open(started_at).read().strip() == original_ts
        release_lock(tmpdir)


def test_independent_evaluators():
    """INDEPENDENT_EVALUATORS is a non-empty list of strings."""
    assert isinstance(INDEPENDENT_EVALUATORS, list)
    assert len(INDEPENDENT_EVALUATORS) > 0
    assert all(isinstance(e, str) for e in INDEPENDENT_EVALUATORS)


def test_independent_evaluators_priority_order():
    """INDEPENDENT_EVALUATORS has agent first, then codex, then claude."""
    assert INDEPENDENT_EVALUATORS[0] == "agent"
    assert INDEPENDENT_EVALUATORS[1] == "codex"
    assert INDEPENDENT_EVALUATORS[2] == "claude"


# ---------------------------------------------------------------------------
# _find_current_feature tests
# ---------------------------------------------------------------------------

def test_find_current_feature_from_progress(tmp_path):
    """Uses current_feature from progress if available."""
    progress = {"current_feature": "auth", "completed_features": []}
    result = _find_current_feature(str(tmp_path), progress)
    assert result == "auth"


def test_find_current_feature_from_spec(tmp_path):
    """Falls back to first uncompleted feature in spec.md."""
    (tmp_path / "spec.md").write_text(
        "- [x] File Upload — done\n"
        "- [ ] JWT Auth — pending\n"
        "- [ ] Admin Panel — pending\n"
    )
    progress = {"current_feature": None, "completed_features": ["File Upload"]}
    result = _find_current_feature(str(tmp_path), progress)
    assert result == "JWT Auth"


def test_find_current_feature_all_completed(tmp_path):
    """Returns 'unknown' when all features completed."""
    (tmp_path / "spec.md").write_text("- [x] Auth — done\n")
    progress = {"current_feature": None, "completed_features": ["Auth"]}
    result = _find_current_feature(str(tmp_path), progress)
    assert result == "unknown"


def test_find_current_feature_no_spec(tmp_path):
    """Returns 'unknown' when spec.md doesn't exist."""
    progress = {"current_feature": None, "completed_features": []}
    result = _find_current_feature(str(tmp_path), progress)
    assert result == "unknown"


# ---------------------------------------------------------------------------
# prepare_dispatch tests
# ---------------------------------------------------------------------------

def test_prepare_dispatch_basic(tmp_path):
    """Assembles dispatch file from file list."""
    (tmp_path / "program.md").write_text("# Goals\nBuild a chat app")
    (tmp_path / "strategy.md").write_text("Continue: fix error handling")

    path = prepare_dispatch(str(tmp_path), "B", ["program.md", "strategy.md"])
    assert path == str(tmp_path / "dispatch_B.md")

    content = (tmp_path / "dispatch_B.md").read_text()
    assert "# Dispatch: B" in content
    assert "## program.md" in content
    assert "Build a chat app" in content
    assert "## strategy.md" in content
    assert "fix error handling" in content


def test_prepare_dispatch_with_note(tmp_path):
    """Includes O's note in dispatch file."""
    (tmp_path / "program.md").write_text("# Goals")

    prepare_dispatch(str(tmp_path), "C", ["program.md"],
                     note="trend is flat, consider pivot")

    content = (tmp_path / "dispatch_C.md").read_text()
    assert "## Note from O" in content
    assert "trend is flat" in content


def test_prepare_dispatch_missing_file(tmp_path):
    """Handles missing files gracefully."""
    (tmp_path / "program.md").write_text("# Goals")

    prepare_dispatch(str(tmp_path), "B", ["program.md", "nonexistent.md"])

    content = (tmp_path / "dispatch_B.md").read_text()
    assert "## program.md" in content
    assert "## nonexistent.md" in content
    assert "(file not found)" in content


def test_prepare_dispatch_truncates_results_tsv(tmp_path):
    """Truncates results.tsv to header + last 20 lines."""
    header = "\t".join(HEADER_FIELDS)
    lines = [header] + [f"c{i}\tbuild\tauth\t-\t-\tkeep\tbuild {i}" for i in range(30)]
    (tmp_path / "results.tsv").write_text("\n".join(lines))

    prepare_dispatch(str(tmp_path), "B", ["results.tsv"])

    content = (tmp_path / "dispatch_B.md").read_text()
    # Should have header + last 20 lines = 21 lines in the results section
    results_section = content.split("## results.tsv\n")[1].strip()
    result_lines = [l for l in results_section.split("\n") if l.strip()]
    assert len(result_lines) == 21  # header + 20 data lines


def test_prepare_dispatch_truncates_run_log(tmp_path):
    """Truncates run.log to last 50 lines."""
    lines = [f"[2024-01-01] log line {i}" for i in range(100)]
    (tmp_path / "run.log").write_text("\n".join(lines))

    prepare_dispatch(str(tmp_path), "B", ["run.log"])

    content = (tmp_path / "dispatch_B.md").read_text()
    log_section = content.split("## run.log\n")[1].strip()
    log_lines = [l for l in log_section.split("\n") if l.strip()]
    assert len(log_lines) == 50


def test_prepare_dispatch_target_c(tmp_path):
    """Creates dispatch_C.md for target C."""
    (tmp_path / "eval.yml").write_text("dimensions:\n  - name: Quality")

    path = prepare_dispatch(str(tmp_path), "C", ["eval.yml"])
    assert path == str(tmp_path / "dispatch_C.md")
    assert (tmp_path / "dispatch_C.md").exists()


# ---------------------------------------------------------------------------
# build_manifest tests
# ---------------------------------------------------------------------------

def test_build_manifest_creates_file(tmp_path):
    """build_manifest writes manifest.md."""
    # Minimal setup: results.tsv with one row
    tsv = tmp_path / "results.tsv"
    tsv.write_text(
        "\t".join(HEADER_FIELDS) + "\n"
        "a1b\tplan\t-\t-\t-\tkeep\tspec\n"
    )
    (tmp_path / "spec.md").write_text("- [ ] Auth — login system\n")

    # Mock started_at to avoid runtime limit issues
    (tmp_path / "started_at").write_text(str(time.time()))

    manifest = build_manifest(str(tmp_path))

    assert (tmp_path / "manifest.md").exists()
    assert "# Evolve Manifest" in manifest
    assert "## Status" in manifest
    assert "phase:" in manifest
    assert "feature:" in manifest
    assert "should_stop:" in manifest


def test_build_manifest_structured_status(tmp_path):
    """Manifest contains correct structured status data."""
    tsv = tmp_path / "results.tsv"
    header = "\t".join(HEADER_FIELDS)
    tsv.write_text(
        f"{header}\n"
        "a1b\tplan\t-\t-\t-\tkeep\tspec\n"
        "b2c\tbuild\tauth\t-\t-\tkeep\tbuild\n"
        "c3d\teval\tauth\t8/9\t8.5\tpass\tpass\n"
        "d4e\tbuild\tchat\t-\t-\tkeep\tbuild chat\n"
    )
    (tmp_path / "spec.md").write_text(
        "- [ ] auth — login\n"
        "- [ ] chat — messaging\n"
    )
    (tmp_path / "started_at").write_text(str(time.time()))

    manifest = build_manifest(str(tmp_path))

    assert "round: 4" in manifest
    assert "phase: eval" in manifest  # build/keep -> eval
    assert "chat" in manifest
    assert "completed: ['auth']" in manifest
    assert "should_stop: no" in manifest


def test_build_manifest_haiku_fallback(tmp_path):
    """Manifest works even if Haiku API fails (deterministic fallback)."""
    tsv = tmp_path / "results.tsv"
    tsv.write_text(
        "\t".join(HEADER_FIELDS) + "\n"
        "a1b\tplan\t-\t-\t-\tkeep\tspec\n"
    )
    (tmp_path / "spec.md").write_text("- [ ] Auth\n")
    (tmp_path / "started_at").write_text(str(time.time()))

    # Even without API key / network, should produce a valid manifest
    manifest = build_manifest(str(tmp_path))
    assert "# Evolve Manifest" in manifest
    assert "## Summary" in manifest


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


def test_manifest_summary_invalidated_on_spec_change(tmp_path, monkeypatch):
    import prepare as prepare_mod
    evolve = _manifest_env(tmp_path)
    monkeypatch.setattr(prepare_mod, "_haiku_summarize",
                        lambda s, f: "summary v1")
    build_manifest(evolve)
    (Path(evolve) / "spec.md").write_text("- [ ] F01\n- [ ] F99-new\n")
    monkeypatch.setattr(prepare_mod, "_haiku_summarize",
                        lambda s, f: "summary v2")
    manifest = build_manifest(evolve)
    assert "summary v2" in manifest            # spec.md edit invalidates


def test_manifest_summary_cache_tolerates_wrong_shape(tmp_path, monkeypatch):
    import prepare as prepare_mod
    evolve = _manifest_env(tmp_path)
    (Path(evolve) / "manifest_summary.json").write_text('["not", "a", "dict"]')
    monkeypatch.setattr(prepare_mod, "_haiku_summarize",
                        lambda s, f: "fresh summary")
    manifest = build_manifest(evolve)          # must not raise
    assert "fresh summary" in manifest


# ---------------------------------------------------------------------------
# _parse_uncompleted_features tests
# ---------------------------------------------------------------------------

def test_parse_uncompleted_features(tmp_path):
    """Parses spec.md and returns uncompleted features."""
    (tmp_path / "spec.md").write_text(
        "- [x] Auth — done\n"
        "- [ ] Chat — pending\n"
        "- [ ] Admin — pending\n"
    )
    result = _parse_uncompleted_features(str(tmp_path), {"Auth"})
    assert result == ["Chat", "Admin"]


def test_parse_uncompleted_features_no_spec(tmp_path):
    """Returns empty list when spec.md missing."""
    result = _parse_uncompleted_features(str(tmp_path), set())
    assert result == []


# ---------------------------------------------------------------------------
# prepare_dispatch validation tests
# ---------------------------------------------------------------------------

def test_prepare_dispatch_invalid_target(tmp_path):
    """Rejects invalid target values."""
    with pytest.raises(ValueError, match="Invalid dispatch target"):
        prepare_dispatch(str(tmp_path), "X", ["program.md"])


def test_prepare_dispatch_line_range(tmp_path):
    """Extracts specific line range from a file."""
    lines = [f"Line {i}" for i in range(1, 101)]  # 100 lines
    (tmp_path / "big.md").write_text("\n".join(lines))

    prepare_dispatch(str(tmp_path), "B", ["big.md:10-15"])

    content = (tmp_path / "dispatch_B.md").read_text()
    assert "## big.md:10-15" in content
    assert "Line 10" in content
    assert "Line 15" in content
    assert "Line 9" not in content
    assert "Line 16" not in content


def test_prepare_dispatch_line_range_single(tmp_path):
    """Supports single line spec like 'file.md:42'."""
    lines = [f"Line {i}" for i in range(1, 51)]
    (tmp_path / "doc.md").write_text("\n".join(lines))

    prepare_dispatch(str(tmp_path), "C", ["doc.md:42"])

    content = (tmp_path / "dispatch_C.md").read_text()
    assert "Line 42" in content
    assert "Line 41" not in content
    assert "Line 43" not in content


def test_prepare_dispatch_line_range_with_full_file(tmp_path):
    """Line range and full files can be mixed."""
    (tmp_path / "small.md").write_text("Full content here")
    lines = [f"L{i}" for i in range(1, 201)]
    (tmp_path / "big.md").write_text("\n".join(lines))

    prepare_dispatch(str(tmp_path), "B", ["small.md", "big.md:50-60"])

    content = (tmp_path / "dispatch_B.md").read_text()
    assert "Full content here" in content
    assert "L50" in content
    assert "L60" in content
    assert "L49" not in content


# ---------------------------------------------------------------------------
# Section extraction tests
# ---------------------------------------------------------------------------

def test_extract_section_basic():
    """Extracts a section from heading to next same-level heading."""
    doc = """# Intro
Some intro

## Feature A
Feature A content
More A content

## Feature B
Feature B content
"""
    result = _extract_section(doc, "Feature A")
    assert "Feature A content" in result
    assert "More A content" in result
    assert "Feature B" not in result
    assert "Intro" not in result


def test_extract_section_nested():
    """Includes nested subheadings within the section."""
    doc = """## F07 Pattern Mirror
F07 overview

### Design Goals
Goal 1

### Implementation
Code here

## F08 Something Else
Other stuff
"""
    result = _extract_section(doc, "F07")
    assert "F07 overview" in result
    assert "Goal 1" in result
    assert "Code here" in result
    assert "F08" not in result


def test_extract_section_not_found():
    """Returns message when section not found."""
    result = _extract_section("# Only Section\nContent", "Nonexistent")
    assert "not found" in result


def test_extract_section_case_insensitive():
    """Section matching is case-insensitive."""
    doc = "## My Feature\nContent here\n## Other\nOther content"
    result = _extract_section(doc, "my feature")
    assert "Content here" in result


def test_prepare_dispatch_section_syntax(tmp_path):
    """Supports #section syntax in file list."""
    doc = """# Product Design

## F01 Login
Login specs here

## F02 Canvas
Canvas Card C specs
Canvas Card D specs

## F03 Dashboard
Dashboard specs
"""
    (tmp_path / "design.md").write_text(doc)

    prepare_dispatch(str(tmp_path), "B", ["design.md#F02"])

    content = (tmp_path / "dispatch_B.md").read_text()
    assert "Canvas Card C specs" in content
    assert "Canvas Card D specs" in content
    assert "Login specs" not in content
    assert "Dashboard specs" not in content


def test_prepare_dispatch_mixed_specs(tmp_path):
    """Mixes full files, line ranges, and section specs."""
    (tmp_path / "small.md").write_text("Small file")
    lines = [f"Line {i}" for i in range(1, 51)]
    (tmp_path / "big.md").write_text("\n".join(lines))
    (tmp_path / "doc.md").write_text("# Intro\nIntro\n## Target\nTarget content\n## Other\nOther")

    prepare_dispatch(str(tmp_path), "C", [
        "small.md",
        "big.md:10-15",
        "doc.md#Target",
    ])

    content = (tmp_path / "dispatch_C.md").read_text()
    assert "Small file" in content
    assert "Line 10" in content
    assert "Line 16" not in content
    assert "Target content" in content
    assert "Other" not in content  # section extraction excludes other sections


def test_parse_file_spec_plain():
    """Plain filename returns no slicer."""
    name, slicer = _parse_file_spec("program.md")
    assert name == "program.md"
    assert slicer is None


def test_parse_file_spec_line_range():
    """Line range returns slicer function."""
    name, slicer = _parse_file_spec("doc.md:10-20")
    assert name == "doc.md"
    assert slicer is not None
    content = "\n".join(f"L{i}" for i in range(1, 31))
    result = slicer(content)
    assert "L10" in result
    assert "L20" in result
    assert "L9" not in result


def test_parse_file_spec_section():
    """Section spec returns slicer function."""
    name, slicer = _parse_file_spec("design.md#F07")
    assert name == "design.md"
    assert slicer is not None


def test_parse_file_spec_malformed_range():
    """Malformed ranges like ':-5' are treated as plain filenames."""
    name, slicer = _parse_file_spec("file.md:-5")
    assert name == "file.md:-5"
    assert slicer is None


def test_parse_file_spec_trailing_dash():
    """Trailing dash like ':10-' is treated as plain filename."""
    name, slicer = _parse_file_spec("file.md:10-")
    assert name == "file.md:10-"
    assert slicer is None


def test_parse_file_spec_empty_range():
    """Empty range ':' is treated as plain filename."""
    name, slicer = _parse_file_spec("file.md:")
    assert name == "file.md:"
    assert slicer is None


# ---------------------------------------------------------------------------
# scan_all_features
# ---------------------------------------------------------------------------

def test_scan_all_features_empty(tmp_path):
    """No spec.md, no results -> empty list."""
    assert scan_all_features(str(tmp_path)) == []


def test_scan_all_features_not_started(tmp_path):
    (tmp_path / "spec.md").write_text("- [ ] F01\n- [ ] F02\n")
    _make_tsv_at(tmp_path, [])
    result = scan_all_features(str(tmp_path))
    assert len(result) == 2
    assert result[0]["name"] == "F01"
    assert result[0]["state"] == "not_started"
    assert result[1]["name"] == "F02"
    assert result[1]["state"] == "not_started"


def test_scan_all_features_mixed_states(tmp_path):
    (tmp_path / "spec.md").write_text("- [ ] F01\n- [ ] F02\n- [ ] F03\n")
    rows = [
        {"commit": "a1", "phase": "build", "feature": "F01",
         "scores": "-", "total": "-", "status": "keep", "summary": "built"},
        {"commit": "a2", "phase": "eval", "feature": "F01",
         "scores": "8", "total": "8", "status": "pass", "summary": "pass"},
        {"commit": "a3", "phase": "build", "feature": "F02",
         "scores": "-", "total": "-", "status": "keep", "summary": "built"},
    ]
    _make_tsv_at(tmp_path, rows)
    result = scan_all_features(str(tmp_path))
    assert result[0]["state"] == "completed"
    assert result[1]["state"] == "needs_eval"
    assert result[2]["state"] == "not_started"


def test_scan_all_features_fail_state(tmp_path):
    (tmp_path / "spec.md").write_text("- [ ] F01\n")
    rows = [
        {"commit": "a1", "phase": "build", "feature": "F01",
         "scores": "-", "total": "-", "status": "keep", "summary": "built"},
        {"commit": "a2", "phase": "eval", "feature": "F01",
         "scores": "5", "total": "5", "status": "fail", "summary": "low"},
    ]
    _make_tsv_at(tmp_path, rows)
    result = scan_all_features(str(tmp_path))
    assert result[0]["state"] == "needs_build"
    assert result[0]["consecutive_fails"] == 1


def test_scan_all_features_in_progress(tmp_path):
    (tmp_path / "spec.md").write_text("- [ ] F01\n")
    _make_tsv_at(tmp_path, [])
    feat_dir = tmp_path / "F01"
    feat_dir.mkdir()
    (feat_dir / "lock").write_text(json.dumps({
        "agent": "C", "heartbeat": time.time(), "feature": "F01"
    }))
    result = scan_all_features(str(tmp_path))
    assert result[0]["in_progress"] == "C"


def test_scan_all_features_stale_lock(tmp_path):
    (tmp_path / "spec.md").write_text("- [ ] F01\n")
    _make_tsv_at(tmp_path, [])
    feat_dir = tmp_path / "F01"
    feat_dir.mkdir()
    (feat_dir / "lock").write_text(json.dumps({
        "agent": "B", "heartbeat": time.time() - 300, "feature": "F01"
    }))
    result = scan_all_features(str(tmp_path))
    assert result[0]["in_progress"] is None


# ---------------------------------------------------------------------------
# Build lock
# ---------------------------------------------------------------------------

def test_acquire_build_lock_fresh(tmp_path):
    result = acquire_build_lock(str(tmp_path))
    assert result["acquired"] is True
    assert result["token"] is not None
    assert (tmp_path / "build_lock").exists()


def test_acquire_build_lock_blocked(tmp_path):
    (tmp_path / "build_lock").write_text(json.dumps({
        "pid": 12345, "feature": "F01", "token": "other",
        "heartbeat": time.time()
    }))
    result = acquire_build_lock(str(tmp_path))
    assert result["acquired"] is False
    assert "F01" in result["reason"]


def test_acquire_build_lock_stale(tmp_path):
    # build_lock uses BUILD_LOCK_STALE_SECONDS (30 min), not the 120s
    # LOCK_STALE_SECONDS -- the merge cascade it guards can run for
    # minutes, so only a heartbeat older than 30 min counts as stale.
    (tmp_path / "build_lock").write_text(json.dumps({
        "pid": 12345, "feature": "F01", "token": "old",
        "heartbeat": time.time() - (BUILD_LOCK_STALE_SECONDS + 60)
    }))
    result = acquire_build_lock(str(tmp_path))
    assert result["acquired"] is True


def test_build_lock_not_stolen_during_long_merge(tmp_path):
    import json, time
    bl = acquire_build_lock(str(tmp_path))
    # simulate a merge running for > LOCK_STALE_SECONDS but < BUILD_LOCK_STALE_SECONDS
    lock_path = tmp_path / "build_lock"
    data = json.loads(lock_path.read_text())
    data["heartbeat"] = time.time() - 300   # 5 min old
    lock_path.write_text(json.dumps(data))
    second = acquire_build_lock(str(tmp_path))
    assert second["acquired"] is False       # NOT stolen at 5 min
    release_build_lock(str(tmp_path), bl["token"])


def test_release_build_lock_with_token(tmp_path):
    result = acquire_build_lock(str(tmp_path))
    assert (tmp_path / "build_lock").exists()
    release_build_lock(str(tmp_path), result["token"])
    assert not (tmp_path / "build_lock").exists()


def test_release_build_lock_wrong_token(tmp_path):
    """Release with wrong token should NOT delete the lock."""
    result = acquire_build_lock(str(tmp_path))
    release_build_lock(str(tmp_path), "wrong-token")
    assert (tmp_path / "build_lock").exists()  # Lock preserved


# ---------------------------------------------------------------------------
# Feature lock
# ---------------------------------------------------------------------------

def test_acquire_feature_lock_c(tmp_path):
    result = acquire_feature_lock(str(tmp_path), "F01", "C")
    assert result["acquired"] is True
    assert result["token"] is not None
    assert (tmp_path / "F01" / "lock").exists()


def test_acquire_feature_lock_b_does_not_take_build_lock(tmp_path):
    lock = acquire_feature_lock(str(tmp_path), "F01", "B")
    assert lock["acquired"] is True
    assert not (tmp_path / "build_lock").exists()
    release_feature_lock(str(tmp_path), "F01", lock["token"])


def test_parallel_b_agents_on_different_features(tmp_path):
    l1 = acquire_feature_lock(str(tmp_path), "F01", "B")
    l2 = acquire_feature_lock(str(tmp_path), "F02", "B")
    assert l1["acquired"] is True
    assert l2["acquired"] is True          # B no longer globally exclusive
    # same feature still exclusive
    l3 = acquire_feature_lock(str(tmp_path), "F01", "B")
    assert l3["acquired"] is False
    release_feature_lock(str(tmp_path), "F01", l1["token"])
    release_feature_lock(str(tmp_path), "F02", l2["token"])


def test_b_not_blocked_by_merge_build_lock(tmp_path):
    # merge_feature holds build_lock; B dispatch must not be blocked by it
    bl = acquire_build_lock(str(tmp_path))
    assert bl["acquired"] is True
    lock = acquire_feature_lock(str(tmp_path), "F01", "B")
    assert lock["acquired"] is True
    release_feature_lock(str(tmp_path), "F01", lock["token"])
    release_build_lock(str(tmp_path), bl["token"])


def test_acquire_feature_lock_path_traversal(tmp_path):
    result = acquire_feature_lock(str(tmp_path), "../escape", "C")
    assert result["acquired"] is False
    assert "Invalid" in result["reason"]


def test_release_feature_lock_backward_compat_releases_stored_build_token(tmp_path):
    """Old-format lock files (written by pre-fix sessions) may still carry a
    build_token. release_feature_lock must still release it for backward
    compatibility, even though current acquire_feature_lock never sets one.
    """
    feat_dir = tmp_path / "F01"
    feat_dir.mkdir()
    bl = acquire_build_lock(str(tmp_path))
    assert bl["acquired"] is True
    (feat_dir / "lock").write_text(json.dumps({
        "pid": os.getpid(), "token": "feat-token", "build_token": bl["token"],
        "agent": "B", "feature": "F01", "heartbeat": time.time(),
    }))
    release_feature_lock(str(tmp_path), "F01", "feat-token")
    assert not (feat_dir / "lock").exists()
    assert not (tmp_path / "build_lock").exists()


def test_release_feature_lock_wrong_token(tmp_path):
    """Release with wrong token should NOT delete the lock."""
    acquire_feature_lock(str(tmp_path), "F01", "B")
    release_feature_lock(str(tmp_path), "F01", "wrong-token")
    assert (tmp_path / "F01" / "lock").exists()  # Lock preserved


def test_release_feature_lock_c_keeps_build_lock(tmp_path):
    # Simulate B holding build lock on F02, C releasing F01
    (tmp_path / "build_lock").write_text(json.dumps({
        "pid": 12345, "feature": "F02", "token": "b-token",
        "heartbeat": time.time()
    }))
    result = acquire_feature_lock(str(tmp_path), "F01", "C")
    release_feature_lock(str(tmp_path), "F01", result["token"])
    assert not (tmp_path / "F01" / "lock").exists()
    assert (tmp_path / "build_lock").exists()  # B's lock preserved


# ---------------------------------------------------------------------------
# prepare_dispatch with feature parameter
# ---------------------------------------------------------------------------

def test_prepare_dispatch_feature_subdir(tmp_path):
    (tmp_path / "program.md").write_text("# Program\nGoal: test")
    path = prepare_dispatch(str(tmp_path), "B", ["program.md"],
                           note="test", feature="F01")
    assert "F01" in path
    assert (tmp_path / "F01" / "dispatch_B.md").exists()
    content = (tmp_path / "F01" / "dispatch_B.md").read_text()
    assert "# Program" in content
    assert "test" in content


def test_prepare_dispatch_feature_reads_from_root(tmp_path):
    """Feature dispatch reads shared files from evolve root, not feature subdir."""
    (tmp_path / "program.md").write_text("shared content")
    feat_dir = tmp_path / "F01"
    feat_dir.mkdir()
    (feat_dir / "strategy.md").write_text("feature strategy")
    path = prepare_dispatch(str(tmp_path), "C",
                           ["program.md", "F01/strategy.md"],
                           feature="F01")
    content = Path(path).read_text()
    assert "shared content" in content
    assert "feature strategy" in content


# ---------------------------------------------------------------------------
# Helper: make TSV at specific path
# ---------------------------------------------------------------------------

def _make_tsv_at(base_path, rows):
    """Create results.tsv at base_path with header + rows."""
    tsv_path = base_path / "results.tsv"
    with open(tsv_path, "w", newline="") as f:
        f.write('\t'.join(HEADER_FIELDS) + '\n')
        for row in rows:
            f.write('\t'.join(str(row.get(h, '-')) for h in HEADER_FIELDS) + '\n')
    return str(tsv_path)


def test_append_result_pairwise_survives_second_append(tmp_path):
    # Regression: header written by DictWriter ends in \r\n; sniffing it
    # back must not leave a trailing \r on the last fieldname.
    path = str(tmp_path / "results.tsv")
    append_result(path, {
        "commit": "a", "phase": "eval", "feature": "F01",
        "scores": "7/7", "total": "7.0", "status": "fail",
        "summary": "first", "pairwise": "log:same",
    })
    append_result(path, {
        "commit": "b", "phase": "eval", "feature": "F01",
        "scores": "8/8", "total": "8.0", "status": "fail",
        "summary": "second", "pairwise": "log:better",
    })
    rows = Path(path).read_text().strip().split("\n")
    assert rows[2].rstrip("\r").split("\t")[-1] == "log:better"


# ---------------------------------------------------------------------------
# analyze_trajectory: pairwise preference, noise detection, cascade_fail skip
# ---------------------------------------------------------------------------

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


def test_generate_report_candidate_pass_does_not_complete_parent(tmp_path):
    path = str(tmp_path / "results.tsv")
    append_result(path, {"commit": "a", "phase": "eval", "feature": "F01",
                         "scores": "6/6", "total": "6.0", "status": "fail",
                         "summary": "stuck"})
    append_result(path, {"commit": "b", "phase": "eval",
                         "feature": "F01@cand2", "scores": "9/9",
                         "total": "9.0", "status": "pass",
                         "summary": "candidate passed"})
    report = generate_report(path)
    assert "✓ F01" not in report            # not shown as passed
    assert "Complete" not in report         # run not complete
    assert "candidate passed" in report     # visible in iteration record


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


def test_truncate_evidence_small_cap_still_truncates():
    from prepare import _truncate_evidence
    content = "X" * 5000
    out = _truncate_evidence(content, 1000)
    assert len(out) < len(content)
    assert "truncated" in out
    out500 = _truncate_evidence(content, 500)
    assert len(out500) < 1000 + 100          # ~cap plus marker, never inflated


def test_truncate_evidence_default_split_unchanged():
    from prepare import _truncate_evidence
    content = "H" * 2000 + "M" * 20000 + "T" * 6000
    out = _truncate_evidence(content, 6000)
    assert out.startswith("H" * 1000)         # head still 1000 at default cap
    assert out.endswith("T" * 5000)           # tail still 5000 at default cap


# ---------------------------------------------------------------------------
# MANIFEST_MODEL tests
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Cache-friendly dispatch ordering tests
# ---------------------------------------------------------------------------

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


def test_get_evaluator_env_override(monkeypatch):
    monkeypatch.setenv("EVOLVE_EVALUATOR", "claude")
    monkeypatch.setattr("prepare.shutil.which",
                        lambda n: "/usr/bin/" + n if n == "claude" else None)
    assert get_evaluator() == "claude"


def test_get_evaluator_env_override_missing_cli(monkeypatch):
    monkeypatch.setenv("EVOLVE_EVALUATOR", "claude")
    monkeypatch.setattr("prepare.shutil.which", lambda n: None)
    assert get_evaluator() is None


def test_get_evaluator_no_override_uses_priority(monkeypatch):
    monkeypatch.delenv("EVOLVE_EVALUATOR", raising=False)
    monkeypatch.setattr("prepare.shutil.which",
                        lambda n: "/usr/bin/" + n if n == "codex" else None)
    assert get_evaluator() == "codex"
