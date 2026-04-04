"""
Content Product adapter -- reference implementation for Evolve skill.
Evaluates products with design (sub-PRD) and build (skill/code) phases.

This file is a REFERENCE for the Agent during Init. It is NOT imported at runtime.
The Agent reads this to understand how to generate .evolve/adapter.py.

Pattern:
  - Feature names use suffixes: F1-design, F1-build, F2-design, F2-build, ...
  - Design phase → checks sub-PRD quality (structure, depth, executable content)
  - Build phase → checks implementation (files, frontmatter, tool calls)
  - Named features (e.g. "knowledge-base") → custom checks

All dimensions are LLM-judged. Deterministic checks provide structural signals
to the evaluator via the details field.
"""

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

prerequisites = [
    {
        "name": "codex-cli",
        "check": "codex --version",
        "install": "npm install -g @openai/codex",
        "scope": "global",
    },
]


# ---------------------------------------------------------------------------
# Configuration (customize per project)
# ---------------------------------------------------------------------------

# Map feature base names to implementation directory names
FEATURE_MAP = {
    # "F1": "quiz-skill",
    # "F2": "diagnosis-skill",
}

# Where implementation artifacts live (relative to project root)
SKILLS_DIR = Path("skills")

# Where sub-PRDs are stored (relative to .evolve/)
SUB_PRD_DIR = Path("sub-prd")

# Required frontmatter fields for skill files
REQUIRED_FRONTMATTER = ["name", "description", "triggers"]


# ---------------------------------------------------------------------------
# Setup / Teardown
# ---------------------------------------------------------------------------

def setup(project_dir: str) -> dict:
    """Verify project structure exists."""
    errors = []

    skills_path = Path(project_dir) / SKILLS_DIR
    if not skills_path.exists():
        errors.append(f"Skills directory not found: {SKILLS_DIR}")

    if errors:
        return {"status": "crash", "info": {}, "error": "; ".join(errors)}

    # Ensure sub-prd directory exists
    sub_prd = Path(project_dir) / ".evolve" / SUB_PRD_DIR
    sub_prd.mkdir(parents=True, exist_ok=True)

    return {
        "status": "ready",
        "info": {"skills_dir": str(skills_path), "sub_prd_dir": str(sub_prd)},
        "error": None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter from markdown as a simple dict."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            fm[key.strip()] = val.strip()
    return fm


def _check_sub_prd(project_dir: str, feature_base: str) -> list[str]:
    """Check sub-PRD document quality."""
    details = []
    sub_prd_file = Path(project_dir) / ".evolve" / SUB_PRD_DIR / f"{feature_base}.md"

    if not sub_prd_file.exists():
        details.append(f"sub-PRD not found: sub-prd/{feature_base}.md")
        return details

    text = sub_prd_file.read_text(encoding="utf-8")
    char_count = len(text)
    sections = [l for l in text.split("\n") if l.startswith("## ")]
    h3_sections = [l for l in text.split("\n") if l.startswith("### ")]

    details.append(f"sub-PRD exists: sub-prd/{feature_base}.md")
    details.append(f"  chars: {char_count}")
    details.append(f"  h2 sections: {len(sections)}")
    details.append(f"  h3 sections: {len(h3_sections)}")

    if char_count < 500:
        details.append("  WARNING: too short (<500 chars)")
    if len(sections) < 3:
        details.append("  WARNING: too few sections (<3)")

    # Check for executable content markers
    has_code = "```" in text
    has_table = "|" in text and "---" in text
    if not (has_code or has_table):
        details.append("  WARNING: no code blocks or tables found")

    return details


def _check_skill(project_dir: str, feature_base: str) -> list[str]:
    """Check skill/code implementation quality."""
    details = []
    skill_name = FEATURE_MAP.get(feature_base, feature_base)
    skill_dir = Path(project_dir) / SKILLS_DIR / skill_name

    if not skill_dir.exists():
        details.append(f"Implementation not found: {SKILLS_DIR}/{skill_name}/")
        return details

    files = [f for f in skill_dir.rglob("*") if f.is_file()]
    details.append(f"Implementation exists: {SKILLS_DIR}/{skill_name}/")
    details.append(f"  files: {len(files)}")

    # Check for SKILL.md (if this is a skill-based project)
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        text = skill_md.read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)
        if fm:
            missing = [f for f in REQUIRED_FRONTMATTER if f not in fm]
            if missing:
                details.append(f"  WARNING: SKILL.md missing frontmatter: {', '.join(missing)}")
            else:
                details.append("  SKILL.md frontmatter complete")

    return details


# ---------------------------------------------------------------------------
# Run Checks
# ---------------------------------------------------------------------------

def run_checks(project_dir: str, feature: str) -> dict:
    """
    Route checks by feature suffix.

    Feature naming convention:
      - "F1-design", "F2-design", ...  → sub-PRD quality
      - "F1-build", "F2-build", ...    → implementation quality
      - other names                     → custom (override as needed)
    """
    details_parts = []

    if feature.endswith("-design"):
        feature_base = feature.replace("-design", "")
        details_parts.append(f"=== Design check: {feature} ===")
        details_parts.extend(_check_sub_prd(project_dir, feature_base))

    elif feature.endswith("-build"):
        feature_base = feature.replace("-build", "")
        details_parts.append(f"=== Build check: {feature} ===")
        details_parts.extend(_check_skill(project_dir, feature_base))

        # Verify design phase passed (sub-PRD exists)
        sub_prd = Path(project_dir) / ".evolve" / SUB_PRD_DIR / f"{feature_base}.md"
        if sub_prd.exists():
            details_parts.append(f"  sub-PRD exists (design passed)")
        else:
            details_parts.append(f"  WARNING: no sub-PRD found, build lacks design spec")

    else:
        details_parts.append(f"=== Custom check: {feature} ===")
        details_parts.append("No specific checks — all dimensions LLM-judged")

    # All dimensions are llm-judged, return empty scores
    return {"scores": {}, "details": "\n".join(details_parts)}


def teardown(info: dict) -> None:
    """No cleanup needed."""
    pass
