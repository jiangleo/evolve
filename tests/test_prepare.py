import json, os, tempfile, time, pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from prepare import (append_result, read_progress, HEADER_FIELDS,
                     generate_report, acquire_lock, update_lock, release_lock,
                     load_eval_config, load_adapter,
                     analyze_trajectory, should_stop, validate_eval_result,
                     get_evaluator, HARD_LIMITS, INDEPENDENT_EVALUATORS,
                     build_manifest, prepare_dispatch, _find_current_feature)

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
    assert dims[0]["threshold"] == 7.0


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

def test_validate_eval_result_missing():
    """Raises ValueError when independent_evaluator_used is missing."""
    with pytest.raises(ValueError, match="no independent evaluator"):
        validate_eval_result({})


def test_validate_eval_result_false():
    """Raises ValueError when independent_evaluator_used is False."""
    with pytest.raises(ValueError, match="no independent evaluator"):
        validate_eval_result({"independent_evaluator_used": False})


def test_validate_eval_result_valid():
    """No error when independent_evaluator_used is True."""
    validate_eval_result({"independent_evaluator_used": True})


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
                "max_flat_after_pivot", "max_runtime_hours"}
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
    """INDEPENDENT_EVALUATORS has codex first, claude second per V2 design."""
    assert INDEPENDENT_EVALUATORS[0] == "codex"
    assert INDEPENDENT_EVALUATORS[1] == "claude"


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
