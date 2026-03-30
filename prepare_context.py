#!/usr/bin/env python3
"""
UserPromptSubmit hook for Evolve.

Called by Claude Code BEFORE AI processes the prompt.
Minimal hook: just checks if /evolve is active. All real work is done by
H agent (manifest, context prep) and O agent (dispatch decisions).

Install: add to .claude/settings.json hooks.UserPromptSubmit
"""

import json
import sys
from pathlib import Path


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    prompt = hook_input.get("prompt", "")

    if "/evolve" not in prompt:
        sys.exit(0)

    # Just verify .evolve/ exists — all real work is done by H and O agents
    project_dir = Path(hook_input.get("cwd", "."))
    evolve_dir = project_dir / ".evolve"

    if not evolve_dir.exists():
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
