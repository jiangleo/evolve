#!/usr/bin/env python3
"""
UserPromptSubmit hook for Evolve.

Called by Claude Code BEFORE AI processes the prompt.
If prompt contains "/evolve" and .evolve/ exists, updates manifest.md via Haiku.
O reads manifest.md to decide dispatch — Hook does NOT inject dispatch decisions.

Install: add to .claude/settings.json hooks.UserPromptSubmit
"""

import json
import sys
from pathlib import Path


def main():
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    prompt = hook_input.get("prompt", "")

    # Only activate for /evolve
    if "/evolve" not in prompt:
        sys.exit(0)

    # Find project dir
    project_dir = Path(hook_input.get("cwd", "."))
    evolve_dir = project_dir / ".evolve"

    if not evolve_dir.exists():
        sys.exit(0)

    # Import prepare.py from the skill directory
    skill_dir = project_dir / ".claude" / "skills" / "evolve"
    if not skill_dir.exists():
        # Try direct project root (for development)
        skill_dir = project_dir

    sys.path.insert(0, str(skill_dir))
    from prepare import build_manifest, acquire_lock

    # Acquire lock
    lock = acquire_lock(str(evolve_dir))
    if not lock["acquired"]:
        # Another session running — still update manifest (read-only operation)
        pass

    # Update manifest.md (Haiku summarizes current state)
    build_manifest(str(evolve_dir))

    # No additionalContext injection — O reads manifest.md itself
    sys.exit(0)


if __name__ == "__main__":
    main()
