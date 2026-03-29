"""
Adapter interface for Evolve skill.

This file documents the interface that every adapter must implement.
During Init, the Agent reads this file + reference implementations
(web_app.py, teaching.py) to understand the interface, then auto-generates
a project-specific adapter at .evolve/adapter.py.

Adapters are NOT imported as a package at runtime. The engine loads
.evolve/adapter.py directly via importlib.util (see prepare.load_adapter).

## Interface

Module-level attributes:
    prerequisites: list[dict]  -- what to check/install before running

Required functions:
    setup(project_dir) -> dict
    run_checks(project_dir, feature) -> dict
    teardown(info) -> None

Evaluation dimensions are declared in .evolve/eval.yml, NOT in the adapter.
The adapter only handles runtime execution (setup, check, teardown).
"""


# ---------------------------------------------------------------------------
# Prerequisites Declaration
# ---------------------------------------------------------------------------
# Each entry: {"name": str, "check": str, "install": str|dict, "scope": str}
#   - check: shell command to test availability (e.g. "node --version")
#   - install: install command (str) or platform dict {"darwin": ..., "linux": ...}
#   - scope: "project" (can auto-install) or "global" (only prompt user)

prerequisites = []


# ---------------------------------------------------------------------------
# Required Functions
# ---------------------------------------------------------------------------

def setup(project_dir: str) -> dict:
    """
    Prepare the evaluation environment.

    Called once before run_checks for each eval round.
    Examples: start a web server, create a workspace directory.

    Returns:
        {"status": "ready"|"crash", "info": dict, "error": str|None}

    The "info" dict is passed to teardown() later.
    """
    raise NotImplementedError


def run_checks(project_dir: str, feature: str) -> dict:
    """
    Run deterministic checks (tests, benchmarks, file validation, etc.).

    Only score dimensions where type=deterministic in eval.yml.
    LLM-judged dimensions are handled by the evaluator, not the adapter.

    Returns:
        {
            "scores": {dimension_name: float},  # only deterministic dimensions
            "details": str                       # human-readable summary
        }

    Return empty scores dict if no deterministic dimensions (e.g. teaching adapter).
    """
    raise NotImplementedError


def teardown(info: dict) -> None:
    """
    Clean up after evaluation. `info` is from setup()["info"].

    Examples: kill a web server process, remove temp files.
    """
    raise NotImplementedError
