#!/usr/bin/env python3
"""
UserPromptSubmit hook for Evolve.

Called by Claude Code BEFORE AI processes the prompt.
If prompt contains "/evolve", computes full dispatch context and injects it.
Otherwise exits silently.

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
    from prepare import prepare_context

    # Compute full context
    ctx = prepare_context(str(evolve_dir))

    # Format as readable text for AI
    lines = ["=== EVOLVE DISPATCH ==="]
    lines.append(f"action: {ctx['action']}")

    if ctx["action"] == "not_evolve":
        lines.append("No .evolve/ directory. Run /evolve to initialize.")
        _output(lines)
        return

    if ctx["action"] == "report_only":
        lines.append(f"reason: {ctx['reason']}")
        lines.append("")
        lines.append(ctx["report"])
        _output(lines)
        return

    if ctx["action"] == "stop":
        lines.append(f"reason: {ctx['reason']}")
        lines.append("")
        lines.append(ctx["report"])
        _output(lines)
        return

    # dispatch_B or dispatch_C
    lines.append(f"phase: {ctx['phase']}")
    lines.append(f"feature: {ctx['feature']}")

    progress = ctx.get("progress", {})
    completed = progress.get("completed_features", [])
    total_iter = progress.get("total_iterations", 0)
    lines.append(f"round: {total_iter}")
    lines.append(f"completed: {completed}")

    trajectory = ctx.get("trajectory", {})
    if trajectory:
        lines.append(f"trajectory: {trajectory.get('trend', '?')} (latest={trajectory.get('latest', 0)})")

    lines.append("")

    # Inject file contents
    for name, content in ctx.get("files", {}).items():
        lines.append(f"=== {name} ===")
        lines.append(content)
        lines.append("")

    _output(lines)


def _output(lines):
    """Output as additionalContext JSON."""
    text = "\n".join(lines)
    result = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }
    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
