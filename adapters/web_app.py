"""
Web Application adapter -- reference implementation for Evolve skill.
Evaluates full-stack Web/Python applications.

This file is a REFERENCE for the Agent during Init. It is NOT imported at runtime.
The Agent reads this to understand how to generate .evolve/adapter.py.

Runtime scoring dimensions are declared in .evolve/eval.yml, not here.
"""

import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PORT_RANGE = range(8000, 8011)
TEST_TIMEOUT = 60
APP_START_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

prerequisites = [
    {
        "name": "codex",
        "check": "which codex",
        "install": "npm install -g @openai/codex",
        "scope": "global",
    },
]


# ---------------------------------------------------------------------------
# Environment Setup / Teardown
# ---------------------------------------------------------------------------

def setup(project_dir: str) -> dict:
    """Start the web application."""
    port = _find_free_port()
    if port == -1:
        return {"status": "crash", "info": {}, "error": "All ports 8000-8010 occupied"}

    cmd = _detect_app_start_cmd(project_dir, port)
    if cmd is None:
        return {"status": "crash", "info": {}, "error": "Cannot detect app start command"}

    try:
        proc = subprocess.Popen(
            cmd, cwd=project_dir,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
    except FileNotFoundError as e:
        return {"status": "crash", "info": {}, "error": f"Command not found: {e}"}

    url = f"http://localhost:{port}"
    deadline = time.time() + APP_START_TIMEOUT
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return {"status": "ready", "info": {"url": url, "pid": proc.pid}, "error": None}
        except (ConnectionRefusedError, OSError):
            if proc.poll() is not None:
                return {"status": "crash", "info": {},
                        "error": f"Process exited with code {proc.returncode}"}
            time.sleep(0.5)

    teardown({"pid": proc.pid})
    return {"status": "crash", "info": {},
            "error": f"App did not respond on port {port} within {APP_START_TIMEOUT}s"}


def run_checks(project_dir: str, feature: str) -> dict:
    """Run tests (deterministic scoring)."""
    test_result = _run_tests(project_dir)
    scores = {"Test Pass Rate": test_result["score"]}
    details = (f"Framework: {test_result['framework']}, "
               f"{test_result['passed']}/{test_result['total']} passed, "
               f"score: {test_result['score']}")
    return {"scores": scores, "details": details}


def teardown(info: dict) -> None:
    """Kill app process and its entire process group."""
    pid = info.get("pid")
    if pid is None:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        time.sleep(0.5)
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ---------------------------------------------------------------------------
# Test Runner (internal)
# ---------------------------------------------------------------------------

def _detect_test_framework(project_dir: str) -> str:
    p = Path(project_dir)
    if (p / "pyproject.toml").exists() or (p / "setup.py").exists() or (p / "setup.cfg").exists():
        return "pytest"
    pkg = p / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            deps = {**data.get("devDependencies", {}), **data.get("dependencies", {})}
            if "vitest" in deps:
                return "vitest"
            if "jest" in deps:
                return "jest"
            scripts = data.get("scripts", {})
            test_cmd = scripts.get("test", "")
            if "vitest" in test_cmd:
                return "vitest"
            if "jest" in test_cmd:
                return "jest"
        except (json.JSONDecodeError, KeyError):
            pass
    return "none"


def _parse_pytest_output(stdout: str) -> dict:
    total = passed = failed = 0
    failures = []
    all_matches = re.findall(r'=+\s*(.*?)\s*=+\s*$', stdout, re.MULTILINE)
    summary_match = all_matches[-1] if all_matches else None
    if summary_match:
        summary = summary_match
        p = re.search(r'(\d+) passed', summary)
        f = re.search(r'(\d+) failed', summary)
        e = re.search(r'(\d+) error', summary)
        if p: passed = int(p.group(1))
        if f: failed = int(f.group(1))
        if e: failed += int(e.group(1))
        total = passed + failed
    fail_pattern = re.compile(r'FAILED (.*?) - (.*?)$', re.MULTILINE)
    for match in fail_pattern.finditer(stdout):
        failures.append({"test": match.group(1), "error": match.group(2)})
    return {"total": total, "passed": passed, "failed": failed, "failures": failures}


def _parse_vitest_output(stdout: str) -> dict:
    total = passed = failed = 0
    failures = []
    p = re.search(r'(\d+) passed', stdout)
    f = re.search(r'(\d+) failed', stdout)
    if p: passed = int(p.group(1))
    if f: failed = int(f.group(1))
    total = passed + failed
    return {"total": total, "passed": passed, "failed": failed, "failures": failures}


def _run_tests(project_dir: str, timeout: int = TEST_TIMEOUT) -> dict:
    framework = _detect_test_framework(project_dir)
    if framework == "none":
        return {
            "framework": "none", "total": 0, "passed": 0, "failed": 0,
            "pass_rate": 0.0, "score": 0, "failures": [], "stdout": ""
        }
    cmds = {
        "pytest": ["python", "-m", "pytest", "-v", "--tb=short", "--no-header"],
        "vitest": ["npx", "vitest", "run", "--reporter=verbose"],
        "jest": ["npx", "jest", "--verbose", "--forceExit"],
    }
    cmd = cmds[framework]
    try:
        proc = subprocess.run(
            cmd, cwd=project_dir, capture_output=True, text=True, timeout=timeout
        )
        stdout = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", errors="replace")
        err = (e.stderr or b"").decode("utf-8", errors="replace")
        stdout = out + err + f"\n[TIMEOUT after {timeout}s]"
    if framework == "pytest":
        parsed = _parse_pytest_output(stdout)
    else:
        parsed = _parse_vitest_output(stdout)
    total = parsed["total"]
    passed = parsed["passed"]
    pass_rate = passed / total if total > 0 else 0.0
    score = round(pass_rate * 10, 1)
    lines = stdout.split('\n')
    truncated = '\n'.join(lines[-200:]) if len(lines) > 200 else stdout
    return {
        "framework": framework, "total": total, "passed": passed,
        "failed": parsed["failed"], "pass_rate": round(pass_rate, 3),
        "score": score, "failures": parsed["failures"], "stdout": truncated,
    }


# ---------------------------------------------------------------------------
# App Detection (internal)
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    for port in DEFAULT_PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return -1


def _detect_app_start_cmd(project_dir: str, port: int) -> list[str] | None:
    p = Path(project_dir)
    pyproject = p / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text()
        if "fastapi" in content.lower() or "uvicorn" in content.lower():
            for candidate in ["main:app", "app.main:app", "src.main:app", "app:app"]:
                module_path = candidate.split(":")[0].replace(".", "/") + ".py"
                if (p / module_path).exists():
                    return ["uvicorn", candidate, "--host", "0.0.0.0", "--port", str(port)]
        if "flask" in content.lower() or "gunicorn" in content.lower():
            for candidate in ["main:app", "app:app", "wsgi:app"]:
                module_path = candidate.split(":")[0].replace(".", "/") + ".py"
                if (p / module_path).exists():
                    return ["gunicorn", "-b", f"0.0.0.0:{port}", candidate]
    pkg = p / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            scripts = data.get("scripts", {})
            if "dev" in scripts:
                return ["npm", "run", "dev", "--", "--port", str(port)]
            if "start" in scripts:
                return ["npm", "start"]
        except (json.JSONDecodeError, KeyError):
            pass
    return None
