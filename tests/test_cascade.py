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
