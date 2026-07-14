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
