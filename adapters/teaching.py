"""
Teaching Evaluation adapter -- reference implementation for Evolve skill.
Evaluates teaching document quality using Kirkpatrick 4-level model.

This file is a REFERENCE for the Agent during Init. It is NOT imported at runtime.
The Agent reads this to understand how to generate .evolve/adapter.py.

All dimensions are LLM-judged (no deterministic scoring).
"""

from pathlib import Path


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
    {
        "name": "teaching_docs",
        "check": "test -d teaching_docs",
        "scope": "project",
    },
]


# ---------------------------------------------------------------------------
# Environment Setup / Teardown
# ---------------------------------------------------------------------------

def setup(project_dir: str) -> dict:
    """Create student workspace directory."""
    workspace = Path(project_dir) / ".evolve" / "student-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return {
        "status": "ready",
        "info": {"workspace": str(workspace)},
        "error": None,
    }


def run_checks(project_dir: str, feature: str) -> dict:
    """
    Teaching has no deterministic scoring -- all dimensions are LLM-judged.
    Return empty scores.
    """
    return {"scores": {}, "details": "All dimensions are LLM-judged by evaluator"}


def teardown(info: dict) -> None:
    """No cleanup needed for teaching evaluation."""
    pass
