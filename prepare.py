"""
prepare.py -- Generic evaluation engine for Evolve skill.
Agent MUST NOT modify this file.

Generic engine: append_result, read_progress, generate_report,
                load_adapter, load_eval_config.
Domain-specific logic lives in .evolve/adapter.py (auto-generated during Init).
Reference adapters in adapters/ are for Agent to read during Init, not imported at runtime.
"""

import csv
import hashlib
import importlib.util
import json
import os
import shutil
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADER_FIELDS = ["commit", "phase", "feature", "scores", "total",
                 "status", "summary", "pairwise"]
VALID_PHASES = {"plan", "build", "eval"}
VALID_STATUSES = {"keep", "pass", "fail", "crash", "reset",
                  "cascade_fail", "forced"}

HARD_LIMITS = {
    "max_rounds_total": 100,
    "max_rounds_per_feature": 30,
    "max_consecutive_crashes": 5,
    "max_consecutive_fails": 10,
    "max_flat_after_pivot": 3,
    "max_runtime_hours": 24,
    "max_branching_rounds_per_feature": 1,
    "candidates_per_branching": 3,
}

INDEPENDENT_EVALUATORS = ["agent", "codex", "claude"]

# H (Helper) model — previously Haiku, now Sonnet 4.6 for better context
# scoping on large docs.  EVOLVE_HAIKU_MODEL env var still honored for
# backward compat.
HELPER_MODEL = os.environ.get(
    "EVOLVE_HELPER_MODEL",
    os.environ.get("EVOLVE_HAIKU_MODEL", "claude-sonnet-4-6"),
)
# Manifest summary is a <=300-token compression job — small-model work.
# H's own agent stays on HELPER_MODEL; only this one call routes down.
MANIFEST_MODEL = os.environ.get(
    "EVOLVE_MANIFEST_MODEL", "claude-haiku-4-5-20251001")
AGENT_MODEL = os.environ.get("EVOLVE_AGENT_MODEL", "gpt-5.4-high")

# Previous Round Evidence cap (chars). 0 disables truncation.
try:
    EVIDENCE_CAP = int(os.environ.get("EVOLVE_EVIDENCE_CAP", "6000"))
except ValueError:
    EVIDENCE_CAP = 6000  # malformed env value must not break the engine

# Dispatch section ordering: stable-content-first is prompt-cache
# friendly (a volatile first section invalidates any prefix cache at
# byte 1). Stability judged on the parsed filename's basename.
STABLE_DISPATCH_FILES = {"program.md", "eval.yml", "spec.md", "adapter.py"}

REQUIRED_ADAPTER_FUNCTIONS = ["setup", "run_checks", "teardown"]


# ---------------------------------------------------------------------------
# Adapter Loading
# ---------------------------------------------------------------------------

def load_adapter(adapter_path: str):
    """
    Load project-specific adapter from a file path.

    Uses importlib.util for path-based loading (no sys.path manipulation).
    Validates that required functions exist.
    Defaults prerequisites to [] if not declared.

    Returns the imported adapter module.
    Raises FileNotFoundError if adapter file missing.
    Raises ValueError if required functions missing.
    """
    path = Path(adapter_path)
    if not path.exists():
        raise FileNotFoundError(f"Adapter not found: {adapter_path}")

    spec = importlib.util.spec_from_file_location("evolve_adapter", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    missing = [fn for fn in REQUIRED_ADAPTER_FUNCTIONS if not hasattr(module, fn)]
    if missing:
        raise ValueError(
            f"Adapter '{path.name}' missing required functions: {missing}"
        )

    if not hasattr(module, 'prerequisites'):
        module.prerequisites = []

    return module




# ---------------------------------------------------------------------------
# Eval Config Loading
# ---------------------------------------------------------------------------

def load_eval_config(eval_yml_path: str) -> list[dict]:
    """Parse eval.yml into list of dimension dicts.

    Simple line-based parser for the fixed eval.yml schema.
    No PyYAML dependency -- only handles the constrained eval.yml format:

        dimensions:
          - name: <name>
            type: deterministic|llm-judged
            cmd: <command>          # optional, only for deterministic
            threshold: <float>      # optional, default 7.0
            description: >          # optional, multi-line description
              ...
            scoring_rubric:         # optional, anchor points for LLM scoring
              1: "..."
              5: "..."
              8: "..."
              10: "..."
            checks:                 # optional, deterministic check items
              - check description 1
              - check description 2

    Returns list of dicts with keys: name, type, threshold,
    and optionally cmd, description, scoring_rubric, checks.
    """
    path = Path(eval_yml_path)
    if not path.exists():
        raise FileNotFoundError(f"eval.yml not found: {eval_yml_path}")

    content = path.read_text()
    dimensions = []
    current = None
    in_rubric = False
    in_checks = False
    in_description = False

    for line in content.split('\n'):
        stripped = line.strip()

        # Skip blanks and comments at top level
        if stripped == '' or stripped.startswith('#'):
            if in_description and current:
                in_description = False
            continue

        # New dimension
        if stripped.startswith('- name:'):
            in_rubric = False
            in_checks = False
            in_description = False
            if current:
                dimensions.append(current)
            name = stripped.split(':', 1)[1].strip()
            current = {"name": name, "type": "llm-judged", "threshold": 3.5}
            continue

        if not current:
            continue

        # Detect indent level to know if we're still in a dimension
        raw_indent = len(line) - len(line.lstrip())

        # Rubric entries: "  1: ..." or "  10: ..."
        if in_rubric:
            m = _RUBRIC_RE.match(stripped)
            if m:
                score = int(m.group(1))
                text = m.group(2).strip().strip('"').strip("'")
                current.setdefault("scoring_rubric", {})[score] = text
                continue
            else:
                in_rubric = False

        # Check list entries: "  - ..."
        if in_checks:
            if stripped.startswith('- '):
                current.setdefault("checks", []).append(stripped[2:].strip())
                continue
            else:
                in_checks = False

        # Multi-line description continuation
        if in_description:
            if raw_indent >= 6 and not stripped.endswith(':'):
                current["description"] = current.get("description", "") + " " + stripped
                continue
            else:
                in_description = False

        # Field parsing
        if stripped.startswith('type:'):
            val = stripped.split(':', 1)[1].strip()
            if val not in ("deterministic", "llm-judged"):
                raise ValueError(
                    f"Invalid type '{val}' for dimension '{current['name']}'. "
                    f"Must be 'deterministic' or 'llm-judged'."
                )
            current["type"] = val
        elif stripped.startswith('cmd:'):
            current["cmd"] = stripped.split(':', 1)[1].strip()
        elif stripped.startswith('threshold:'):
            current["threshold"] = float(stripped.split(':', 1)[1].strip())
        elif stripped.startswith('description:'):
            desc = stripped.split(':', 1)[1].strip()
            if desc == '>' or desc == '|':
                in_description = True
                current["description"] = ""
            elif desc:
                current["description"] = desc
        elif stripped.startswith('scoring_rubric:'):
            in_rubric = True
        elif stripped.startswith('checks:'):
            in_checks = True

    if current:
        dimensions.append(current)

    return dimensions


import re
_RUBRIC_RE = re.compile(r'^(\d+)\s*:\s*(.+)$')


# ---------------------------------------------------------------------------
# Trajectory Analysis
# ---------------------------------------------------------------------------

def _pairwise_net(pw: str):
    """Parse 'log:better/ui:same/db:worse' -> net int (+1 per better,
    -1 per worse). Returns None if unparseable/absent."""
    if not pw or pw.strip() in ("-", ""):
        return None
    net, seen = 0, False
    for part in pw.split("/"):
        verdict = part.split(":")[-1].strip().lower()
        if verdict == "better":
            net += 1
            seen = True
        elif verdict == "worse":
            net -= 1
            seen = True
        elif verdict == "same":
            seen = True
    return net if seen else None


def analyze_trajectory(results_tsv: str, feature: str, window: int = 3) -> dict:
    """
    Extract recent eval scores for a feature, determine trend.

    Returns:
        {"trend": "rising"|"flat"|"falling"|"insufficient"|"noisy",
         "scores": [float, ...], "rounds": int, "latest": float}

    Logic:
        - Fewer than window eval rows -> "insufficient"
        - cascade_fail rows are skipped entirely (void round: broken
          build, not a real judgment; excluded from the score series)
        - When pairwise verdicts (better/same/worse) are present for
          every row in the window (except the first, which compares
          against a pre-window round), they take precedence over the
          raw score delta: net better/worse across the window decides
          "rising"/"falling"/"flat"
        - A sign contradiction between the score delta (latest -
          earliest) and the pairwise net yields "noisy"
        - Otherwise, falls back to the score delta alone:
          latest - earliest > +0.5 -> "rising";
          latest - earliest < -0.5 -> "falling"; else "flat"
    Only reads eval phase rows, ignores build/crash.
    """
    path = Path(results_tsv)
    if not path.exists():
        return {"trend": "insufficient", "scores": [], "rounds": 0,
                "latest": 0.0}

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))

    entries = []   # (score, pairwise_net_or_None)
    for r in rows:
        if r.get("phase") != "eval" or r.get("feature") != feature:
            continue
        if r.get("status") == "cascade_fail":
            continue   # void round: broken build, not a real judgment
        try:
            score = float(r["total"])
        except (ValueError, TypeError, KeyError):
            continue
        entries.append((score, _pairwise_net(r.get("pairwise", ""))))

    scores = [e[0] for e in entries]
    if len(scores) < window:
        return {"trend": "insufficient", "scores": scores,
                "rounds": len(scores),
                "latest": scores[-1] if scores else 0.0}

    recent = entries[-window:]
    recent_scores = [e[0] for e in recent]
    diff = recent_scores[-1] - recent_scores[0]

    # Pairwise verdicts are round-vs-previous-round; the first window row's
    # verdict compares against a pre-window round, so use rounds 2..window.
    nets = [e[1] for e in recent[1:]]
    if all(n is not None for n in nets) and nets:
        pairwise_sum = sum(nets)
        contradiction = (diff > 0.5 and pairwise_sum < 0) or \
                        (diff < -0.5 and pairwise_sum > 0)
        if contradiction:
            trend = "noisy"
        elif pairwise_sum > 0:
            trend = "rising"
        elif pairwise_sum < 0:
            trend = "falling"
        else:
            trend = "flat"
    else:
        if diff > 0.5:
            trend = "rising"
        elif diff < -0.5:
            trend = "falling"
        else:
            trend = "flat"

    return {"trend": trend, "scores": recent_scores,
            "rounds": len(scores), "latest": recent_scores[-1]}


# ---------------------------------------------------------------------------
# Stop Conditions
# ---------------------------------------------------------------------------

def should_stop(results_tsv: str, feature: str) -> tuple:
    """
    Called BEFORE AI is dispatched. Returns (stop: bool, reason: str).
    AI does not participate in this decision.
    """
    progress = read_progress(results_tsv)
    trajectory = analyze_trajectory(results_tsv, feature)

    # Time-based limit: check started_at file (written by first acquire_lock)
    started_at_path = Path(results_tsv).parent / "started_at"
    if started_at_path.exists():
        try:
            started = float(started_at_path.read_text().strip())
            elapsed_hours = (time.time() - started) / 3600
            if elapsed_hours >= HARD_LIMITS["max_runtime_hours"]:
                return True, f"Runtime limit reached ({HARD_LIMITS['max_runtime_hours']}h)"
        except (ValueError, OSError):
            pass

    if progress["total_iterations"] >= HARD_LIMITS["max_rounds_total"]:
        return True, "Total round limit reached"

    if progress.get("feature_iterations", 0) >= HARD_LIMITS["max_rounds_per_feature"]:
        return True, f"{feature}: per-feature round limit reached"

    if progress["consecutive_crashes"] >= HARD_LIMITS["max_consecutive_crashes"]:
        return True, f"{feature}: consecutive crashes"

    if progress["consecutive_fails"] >= HARD_LIMITS["max_consecutive_fails"]:
        return True, f"{feature}: consecutive eval failures"

    pivots = progress.get("pivots_on_this_feature", 0)
    if trajectory["trend"] == "flat" and pivots >= HARD_LIMITS["max_flat_after_pivot"]:
        return (True,
                f"{feature}: pivoted {HARD_LIMITS['max_flat_after_pivot']} "
                f"times, still no improvement")

    return False, ""


# ---------------------------------------------------------------------------
# Independent Evaluator
# ---------------------------------------------------------------------------

def get_evaluator() -> str | None:
    """Return the independent evaluator CLI to use. None = unavailable.

    EVOLVE_EVALUATOR forces a specific CLI (e.g. "claude" for the all-Claude
    profile, or when a higher-priority CLI is installed but broken). The
    forced CLI must exist on PATH; otherwise None. Without the override,
    tries INDEPENDENT_EVALUATORS in priority order.
    """
    forced = os.environ.get("EVOLVE_EVALUATOR")
    if forced:
        return forced if shutil.which(forced) is not None else None
    for name in INDEPENDENT_EVALUATORS:
        if shutil.which(name) is not None:
            return name
    return None


def validate_eval_result(result: dict) -> None:
    """Validate an eval round result. Raises ValueError if invalid.

    Enforced invariants (AI cannot skip these):
    1. An independent evaluator was called.
    2. The deterministic cascade ran and passed ("passed"), or the project
       declares no cascade in eval.yml ("empty"). A cascade_fail round must
       be recorded with status=cascade_fail and never reaches the judge.
    """
    if not result.get("independent_evaluator_used"):
        raise ValueError("Eval invalid: no independent evaluator was called")
    if result.get("cascade") not in ("passed", "empty"):
        raise ValueError(
            "Eval invalid: deterministic cascade did not pass "
            "(expected result['cascade'] in {'passed', 'empty'})"
        )


# ---------------------------------------------------------------------------
# TSV Helpers
# ---------------------------------------------------------------------------

def append_result(results_tsv: str, row: dict) -> None:
    """Append one row to results.tsv. Creates file with header if needed.

    Header-adaptive for backward compatibility: appending to an existing
    file uses THAT file's header (old 7-column files keep their shape);
    new files get the full HEADER_FIELDS including 'pairwise'.
    """
    path = Path(results_tsv)
    write_header = not path.exists() or path.stat().st_size == 0

    if write_header:
        fieldnames = HEADER_FIELDS
    else:
        with open(path, newline="") as f:
            first = f.readline().rstrip("\r\n")
        fieldnames = first.split("\t") if first else HEADER_FIELDS

    out = dict(row)
    if "pairwise" in fieldnames:
        out.setdefault("pairwise", "-")

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t",
                                extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(out)


def read_progress(results_tsv: str) -> dict:
    """
    Read results.tsv and return current phase + decision info.

    State machine (V2):
    - No data rows -> init
    - Last row plan/keep -> build
    - Last row build/keep -> eval (dispatch C)
    - Last row build/crash -> build (fix)
    - Last row eval/pass -> build (next feature)
    - Last row eval/fail -> build (C already updated strategy.md)
    """
    path = Path(results_tsv)
    rows = []

    if path.exists() and path.stat().st_size > 0:
        with open(path, newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            rows = list(reader)

    result = {
        "phase": "init",
        "current_feature": None,
        "next_feature": None,
        "consecutive_fails": 0,
        "consecutive_crashes": 0,
        "base_commit": None,
        "total_iterations": len(rows),
        "completed_features": [],
        "forced_features": [],
        "skipped_features": [],
        "last_pass_commit": None,
        "feature_iterations": 0,
        "pivots_on_this_feature": 0,
    }

    if not rows:
        return result

    # Collect completed features (no skip in V2)
    for row in rows:
        phase = row.get("phase", "")
        status = row.get("status", "")
        feature = row.get("feature", "-")
        commit = row.get("commit", "")

        if feature != "-" and "@cand" in feature:
            continue   # candidate rows never complete a spec feature

        if phase == "eval" and status == "pass":
            if feature not in result["completed_features"] and feature != "-":
                result["completed_features"].append(feature)
            result["last_pass_commit"] = commit
        elif phase == "eval" and status == "forced":
            if feature not in result["forced_features"] and feature != "-":
                result["forced_features"].append(feature)

    last = rows[-1]
    last_phase = last.get("phase", "")
    last_status = last.get("status", "")
    last_feature = last.get("feature", "-")

    # Find base_commit for current feature
    for row in reversed(rows):
        if row.get("phase") == "eval" and row.get("status") == "pass":
            result["base_commit"] = row.get("commit")
            break

    # Count consecutive fails/crashes for current feature; detect resets
    has_been_reset = False
    for row in reversed(rows):
        if row.get("feature") != last_feature:
            break
        if row.get("status") == "reset":
            has_been_reset = True
            continue
        if row.get("phase") == "eval" and row.get("status") == "fail":
            result["consecutive_fails"] += 1
        elif row.get("phase") == "build" and row.get("status") == "crash":
            result["consecutive_crashes"] += 1
        elif row.get("phase") == "eval" and row.get("status") == "pass":
            break
        elif row.get("phase") == "build" and row.get("status") == "keep":
            continue
        elif row.get("phase") == "plan":
            break
    result["has_been_reset"] = has_been_reset

    # Count feature iterations (all rows for current feature)
    if last_feature and last_feature != "-":
        result["feature_iterations"] = sum(
            1 for r in rows if r.get("feature") == last_feature
        )

    # Determine current phase from last row (V2: no contract, no skip)
    if last_phase == "plan" and last_status == "keep":
        result["phase"] = "build"
    elif last_phase == "build" and last_status == "keep":
        result["phase"] = "eval"
        result["current_feature"] = last_feature if last_feature != "-" else None
    elif last_phase == "build" and last_status == "crash":
        result["phase"] = "build"
        result["current_feature"] = last_feature if last_feature != "-" else None
    elif last_phase == "eval" and last_status == "pass":
        result["phase"] = "build"
    elif last_phase == "eval" and last_status == "fail":
        result["phase"] = "build"
        result["current_feature"] = last_feature if last_feature != "-" else None
    elif last_phase == "eval" and last_status == "forced":
        result["phase"] = "build"
    else:
        result["phase"] = "init"

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def generate_report(results_tsv: str) -> str:
    """Generate structured progress report.

    Format:
        # Evolve Progress
        ## Status: In Progress -- Round N | Goal: All features >= threshold
        ## Overview
        ## Feature Progress
        ## Current Feature Iteration Record (if in progress)
        ## Elapsed
    """
    path = Path(results_tsv)
    rows = []
    if path.exists() and path.stat().st_size > 0:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))

    total_rounds = len(rows)

    # Group by feature
    features = {}  # preserves insertion order (Python 3.7+)
    for row in rows:
        raw_feat = row.get("feature", "-")
        if raw_feat == "-":
            continue
        feat = raw_feat.split("@cand")[0]   # fold candidate rows into parent
        is_candidate = raw_feat != feat
        if feat not in features:
            features[feat] = {"rows": [], "final_status": None,
                              "final_total": None, "pass_round": None}
        features[feat]["rows"].append(row)
        if is_candidate:
            # Candidate eval/pass rows only feed the iteration record;
            # they must never flip the parent's final status before the
            # winning branch is actually merged.
            continue
        features[feat]["final_status"] = row.get("status")
        if row.get("total", "-") != "-":
            features[feat]["final_total"] = row.get("total")
        if row.get("phase") == "eval" and row.get("status") == "pass":
            features[feat]["pass_round"] = sum(
                1 for r in features[feat]["rows"]
                if r.get("phase") == "eval" and "@cand" not in r.get("feature", "")
            )

    completed = [f for f, i in features.items() if i["final_status"] == "pass"]
    forced = [f for f, i in features.items() if i["final_status"] == "forced"]
    skipped = [f for f, i in features.items() if i["final_status"] == "skip"]
    total_features = len(features)

    # Find current feature (last non-completed, non-skipped)
    current_feat = None
    for feat, info in features.items():
        if info["final_status"] not in ("pass", "forced", "skip"):
            current_feat = feat

    # Build report
    lines = ["# Evolve Progress", ""]

    # Status line
    if not features:
        lines.append("## Status: Waiting to start")
    elif len(completed) + len(forced) + len(skipped) == total_features \
            and total_features > 0:
        lines.append(f"## Status: Complete -- Round {total_rounds} | All passed")
    else:
        lines.append(f"## Status: In Progress -- Round {total_rounds}")

    lines.append("")

    # Overview
    lines.append("## Overview")
    if forced:
        lines.append(f"  Passed: {len(completed)} true + {len(forced)} "
                     f"forced / {total_features} features"
                     + (f" | Current: {current_feat} (best "
                        f"{features[current_feat]['final_total'] or '-'})"
                        if current_feat else ""))
    elif current_feat:
        best = features[current_feat]["final_total"] or "-"
        lines.append(f"  Passed: {len(completed)}/{total_features} features | "
                     f"Current: {current_feat} (best {best})")
    elif total_features > 0:
        lines.append(f"  Passed: {len(completed)}/{total_features} features")
    else:
        lines.append("  No feature data")
    lines.append("")

    # Feature progress
    if features:
        lines.append("## Feature Progress")
        for feat, info in features.items():
            if info["final_status"] == "pass":
                rnd = info["pass_round"] or "?"
                score = info["final_total"] or "-"
                lines.append(f"  \u2713 {feat}    -- passed round {rnd} ({score})")
            elif info["final_status"] == "forced":
                lines.append(f"  \u2691 {feat}    -- forced (waived, not a true pass)")
            elif info["final_status"] == "skip":
                lines.append(f"  \u2717 {feat}    -- skipped")
            elif feat == current_feat:
                eval_count = sum(1 for r in info["rows"]
                                 if r.get("phase") == "eval")
                last_summary = (info["rows"][-1].get("summary", "")
                                if info["rows"] else "")
                lines.append(f"  \u25b6 {feat}    -- {eval_count} attempts, "
                             f"last: \"{last_summary}\"")
            else:
                lines.append(f"  \u00b7 {feat}    -- not started")
        lines.append("")

    # Current feature iteration record
    if current_feat and features[current_feat]["rows"]:
        eval_rows = [r for r in features[current_feat]["rows"]
                     if r.get("phase") == "eval"]
        if eval_rows:
            lines.append("## Current Feature Iteration Record")
            lines.append("  Round | Score | Key Feedback")
            for i, r in enumerate(eval_rows, 1):
                total_score = r.get("total", "-")
                summary = r.get("summary", "-")
                if r.get("status") == "crash":
                    lines.append(f"  {i}   | crash | {summary}")
                else:
                    lines.append(f"  {i}   | {total_score}  | {summary}")
            lines.append("")

    # Elapsed
    lines.append("## Elapsed")
    lines.append(f"  Rounds completed: {total_rounds}")

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Manifest (Haiku-powered summary for O's decision-making)
# ---------------------------------------------------------------------------

def _parse_uncompleted_features(evolve_dir: str, completed: set) -> list:
    """Parse spec.md, return list of uncompleted feature names in order."""
    spec_path = Path(evolve_dir) / "spec.md"
    if not spec_path.exists():
        return []

    result = []
    for line in spec_path.read_text().split("\n"):
        line = line.strip()
        if line.startswith("- [ ]"):
            feat_name = line[5:].strip().split("—")[0].split("–")[0].strip()
            if feat_name and feat_name not in completed:
                result.append(feat_name)
    return result


def _find_current_feature(evolve_dir: str, progress: dict) -> str:
    """Find the first uncompleted feature from spec.md."""
    feature = progress.get("current_feature")
    if feature:
        return feature

    remaining = _parse_uncompleted_features(
        evolve_dir, set(progress.get("completed_features", []))
    )
    return remaining[0] if remaining else "unknown"


def _haiku_summarize(status_text: str, raw_files: dict) -> str:
    """Call Haiku for intelligent summary. Falls back to deterministic."""
    try:
        import anthropic
        client = anthropic.Anthropic(timeout=8.0)

        files_text = ""
        for name, content in raw_files.items():
            files_text += f"\n--- {name} ---\n{content}\n"

        response = client.messages.create(
            model=MANIFEST_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": (
                "Summarize this evolve run state for the orchestrator to decide next dispatch.\n\n"
                f"Status:\n{status_text}\n\nFiles:\n{files_text}\n\n"
                "Write 3-5 lines: what happened, key issues, what the next agent needs. Be concise."
            )}],
        )
        return response.content[0].text
    except Exception as e:
        import sys as _sys
        print(f"[manifest] Haiku fallback: {type(e).__name__}: {e}", file=_sys.stderr)
        # Deterministic fallback
        parts = []
        for name, content in raw_files.items():
            first = content.strip().split('\n')[0][:100] if content.strip() else "(empty)"
            parts.append(f"- {name}: {first}")
        return "\n".join(parts) if parts else "(no summary available)"


def build_manifest(evolve_dir: str) -> str:
    """
    Generate manifest.md: structured status + parallel feature state + Haiku summary.
    Called by Hook before O runs. O reads manifest to decide dispatch.
    """
    evolve_path = Path(evolve_dir)
    results_tsv = str(evolve_path / "results.tsv")

    progress = read_progress(results_tsv)
    feature = _find_current_feature(evolve_dir, progress)
    trajectory = analyze_trajectory(results_tsv, feature)
    stop, stop_reason = should_stop(results_tsv, feature)

    # Remaining features from spec.md
    remaining = _parse_uncompleted_features(
        evolve_dir, set(progress.get("completed_features", []))
    )

    # Parallel feature scan
    features = scan_all_features(evolve_dir)

    # Build lock status — probe WITHOUT holding: acquiring for the status
    # line and never releasing poisoned merges for BUILD_LOCK_STALE_SECONDS.
    bl = acquire_build_lock(evolve_dir)
    if bl["acquired"]:
        release_build_lock(evolve_dir, bl["token"])
        build_lock_status = "free"
    else:
        build_lock_status = f"locked ({bl['reason']})"

    # Structured status
    status_lines = [
        f"round: {progress['total_iterations']}",
        f"phase: {progress['phase']}",
        f"feature: {feature}",
        f"feature_round: {progress.get('feature_iterations', 0)}",
        f"trajectory: {trajectory['trend']} (latest={trajectory['latest']})",
        f"consecutive_fails: {progress['consecutive_fails']}",
        f"consecutive_crashes: {progress['consecutive_crashes']}",
        f"completed: {progress.get('completed_features', [])}",
        f"remaining: {remaining}",
        f"build_lock: {build_lock_status}",
        f"should_stop: {'yes — ' + stop_reason if stop else 'no'}",
    ]
    status_text = "\n".join(status_lines)

    # Feature states for parallel dispatch
    feat_lines = []
    for f in features:
        agent = f" [{f['in_progress']}]" if f["in_progress"] else ""
        feat_lines.append(
            f"  {f['name']}: {f['state']}{agent} "
            f"(evals={f['eval_count']}, fails={f['consecutive_fails']})"
        )
    feature_state_text = "\n".join(feat_lines) if feat_lines else "  (no features)"

    # Gather raw text for Haiku
    raw_files = {}
    # Read per-feature strategy files
    for f in features:
        if f["state"] not in ("completed", "not_started"):
            strat_path = evolve_path / f["name"] / "strategy.md"
            if strat_path.exists():
                content = strat_path.read_text()
                if len(content) > 1000:
                    content = content[-1000:]
                raw_files[f"strategy({f['name']})"] = content
    # Fallback: legacy strategy.md at root
    legacy_strat = evolve_path / "strategy.md"
    if legacy_strat.exists() and not raw_files:
        content = legacy_strat.read_text()
        if len(content) > 2000:
            content = content[-2000:]
        raw_files["strategy.md"] = content

    # results.tsv last 10 lines
    tsv_path = evolve_path / "results.tsv"
    if tsv_path.exists():
        lines = tsv_path.read_text().strip().split("\n")
        raw_files["results.tsv (recent)"] = "\n".join(lines[-10:])

    # run.log last 30 lines
    log_path = evolve_path / "run.log"
    if log_path.exists():
        log_lines = log_path.read_text().strip().split("\n")
        raw_files["run.log (tail)"] = "\n".join(log_lines[-30:])

    # Summary caching: the summary narrates raw_files + (round, phase,
    # feature). Volatile lock/timing state (build_lock, should_stop) can
    # reach the summarizer via status_text without changing the
    # fingerprint — acceptable because the authoritative Status section
    # directly above is always recomputed fresh. The
    # structured sections above are ALWAYS recomputed — only the LLM call
    # is skipped on a fingerprint hit.
    spec_path = evolve_path / "spec.md"
    fingerprint_src = json.dumps({
        "round": progress["total_iterations"],
        "phase": progress["phase"],
        "feature": feature,
        "spec": spec_path.read_text() if spec_path.exists() else "",
        "raw": raw_files,
    }, sort_keys=True)
    fingerprint = hashlib.sha256(fingerprint_src.encode()).hexdigest()

    cache_path = evolve_path / "manifest_summary.json"
    summary = None
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if isinstance(cached, dict) and \
                    cached.get("fingerprint") == fingerprint:
                summary = cached.get("summary")
        except (json.JSONDecodeError, OSError):
            pass
    if summary is None:
        summary = _haiku_summarize(status_text, raw_files)
        try:
            cache_path.write_text(json.dumps(
                {"fingerprint": fingerprint, "summary": summary}))
        except OSError:
            pass

    manifest = (
        f"# Evolve Manifest\n\n"
        f"## Status\n{status_text}\n\n"
        f"## Feature States\n{feature_state_text}\n\n"
        f"## Summary\n{summary}\n"
    )
    (evolve_path / "manifest.md").write_text(manifest)
    return manifest


def _parse_file_spec(file_spec: str):
    """
    Parse a file spec into (filename, slicer_fn_or_None).

    Supported formats:
        "file.md"              → full file
        "file.md:100-200"      → lines 100-200 (1-based)
        "file.md:42"           → single line 42
        "file.md#Section Name" → heading-based section extraction
    """
    # Line range: "file.md:100-200" or "file.md:42"
    if ":" in file_spec:
        name, range_str = file_spec.rsplit(":", 1)
        # Validate: must be digits and optional dash, e.g. "100-200" or "42"
        # Reject malformed specs like ":-5" or ":"
        if (range_str.replace("-", "").isdigit()
                and range_str
                and not range_str.startswith("-")
                and not range_str.endswith("-")):
            parts = range_str.split("-", 1)
            start = int(parts[0])
            end = int(parts[1]) if len(parts) > 1 else start
            def line_slicer(content, s=start, e=end):
                all_lines = content.split("\n")
                return "\n".join(all_lines[s - 1:e])
            return name, line_slicer

    # Section: "file.md#Section Name"
    if "#" in file_spec:
        name, section = file_spec.split("#", 1)
        def section_slicer(content, sec=section):
            return _extract_section(content, sec)
        return name, section_slicer

    return file_spec, None


def _extract_section(content: str, section_name: str) -> str:
    """
    Extract a markdown section by heading name.

    Finds the heading containing section_name, returns everything
    from that heading to the next heading of equal or higher level.
    """
    lines = content.split("\n")
    start_idx = None
    start_level = None

    for i, line in enumerate(lines):
        if line.startswith("#") and section_name.lower() in line.lower():
            start_idx = i
            start_level = len(line) - len(line.lstrip("#"))
            break

    if start_idx is None:
        return f"(section '{section_name}' not found)"

    # Find the end: next heading of same or higher level
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        line = lines[i]
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level <= start_level:
                end_idx = i
                break

    return "\n".join(lines[start_idx:end_idx]).rstrip()


def _truncate_evidence(content: str, cap: int) -> str:
    """Head+tail truncation for previous-round judge output.

    Judge files put dimension scores at the head and conclusions/rationale
    at the tail; the middle is process transcript. Keep the first 1,000
    chars + the last (cap - 1000), with an explicit marker. cap <= 0
    disables truncation.
    """
    if cap <= 0 or len(content) <= cap:
        return content
    head_len = min(1000, cap // 2)
    tail_len = cap - head_len
    head, tail = content[:head_len], content[-tail_len:]
    return (f"{head}\n\n[... truncated {len(content) - cap} chars ...]\n\n"
            f"{tail}")


def prepare_dispatch(evolve_dir: str, target: str, file_list: list,
                     note: str = "", feature: str = None) -> str:
    """
    Assemble dispatch file for target agent from O's file list.
    Pure file I/O — O decides what to include, this function just reads and writes.

    target: "B" | "C"
    file_list: filenames relative to evolve_dir (e.g. ["program.md", "strategy.md"])
    note: optional instruction from O (appended as a section)
    feature: if set, dispatch file is written to evolve_dir/{feature}/dispatch_{target}.md

    Returns path to dispatch file.
    """
    if target not in ("B", "C"):
        raise ValueError(f"Invalid dispatch target: {target!r}. Must be 'B' or 'C'.")

    evolve_path = Path(evolve_dir)

    if feature:
        if ".." in feature or feature.startswith("/"):
            raise ValueError(f"Invalid feature name: {feature!r}")
        output_dir = evolve_path / feature
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        output_dir = evolve_path

    def _is_stable(file_spec: str) -> bool:
        filename, _ = _parse_file_spec(file_spec)
        return Path(filename).name in STABLE_DISPATCH_FILES

    ordered_specs = ([s for s in file_list if _is_stable(s)] +
                     [s for s in file_list if not _is_stable(s)])

    sections = [f"# Dispatch: {target}\n"]

    for file_spec in ordered_specs:
        filename, content_slice = _parse_file_spec(file_spec)

        filepath = evolve_path / filename
        if not filepath.exists():
            sections.append(f"## {file_spec}\n(file not found)\n")
            continue

        content = filepath.read_text()

        if content_slice:
            content = content_slice(content)
        # Smart truncation for known large files (only if no explicit spec)
        elif filename == "results.tsv":
            lines = content.strip().split("\n")
            if len(lines) > 21:
                content = "\n".join(lines[:1] + lines[-20:])
        elif filename == "run.log":
            lines = content.strip().split("\n")
            if len(lines) > 50:
                content = "\n".join(lines[-50:])

        sections.append(f"## {file_spec}\n{content}\n")

    # Volatile sections LAST (cache-friendly ordering)
    if note:
        sections.append(f"## Note from O\n{note}\n")

    # C only: previous round's judge output enables pairwise verdicts.
    if target == "C" and feature:
        for eval_name in ("eval_codex.md", "eval_agent.md", "eval_claude.md"):
            prev_eval = evolve_path / feature / eval_name
            if prev_eval.exists():
                sections.append(
                    "## Previous Round Evidence\n"
                    "For EVERY dimension, judge this round against the "
                    "previous one below and emit `pairwise: "
                    "better|same|worse` per dimension (recorded in "
                    "results.tsv's pairwise column). Pass/fail stays on "
                    "absolute scores; pairwise feeds trajectory analysis.\n\n"
                    f"{_truncate_evidence(prev_eval.read_text(), EVIDENCE_CAP)}\n"
                )
                break

    dispatch_path = output_dir / f"dispatch_{target}.md"
    dispatch_path.write_text("\n".join(sections))
    return str(dispatch_path)


# ---------------------------------------------------------------------------
# Context Preparation (DEPRECATED — use build_manifest + prepare_dispatch)
# ---------------------------------------------------------------------------

def prepare_context(evolve_dir: str) -> dict:
    """
    DEPRECATED: Use build_manifest() + prepare_dispatch() instead.

    One-shot context preparation. Called by hook BEFORE AI starts.
    Returns everything O needs to dispatch — AI makes zero tool calls for setup.

    Returns dict with:
        action:  "dispatch_B" | "dispatch_C" | "report_only" | "stop" | "not_evolve"
        reason:  why (for stop/report_only)
        phase:   "build" | "eval"
        feature: current feature name
        progress: read_progress() result
        files:   dict of file contents {name: content}
        report:  1-line progress summary
    """
    evolve_path = Path(evolve_dir)
    results_tsv = str(evolve_path / "results.tsv")

    # Not initialized yet
    if not evolve_path.exists() or not (evolve_path / "results.tsv").exists():
        return {"action": "not_evolve", "reason": "no .evolve/ directory"}

    # Lock
    lock = acquire_lock(evolve_dir)
    if not lock["acquired"]:
        progress = read_progress(results_tsv)
        report = generate_report(results_tsv)
        return {
            "action": "report_only",
            "reason": lock["reason"],
            "progress": progress,
            "report": report,
        }

    # Progress + phase
    progress = read_progress(results_tsv)

    # Read spec.md to find current feature
    spec_path = evolve_path / "spec.md"
    spec_content = spec_path.read_text() if spec_path.exists() else ""

    # Determine current feature
    feature = progress.get("current_feature")
    if not feature and spec_content:
        # Find first uncompleted feature from spec
        completed = set(progress.get("completed_features", []))
        for line in spec_content.split("\n"):
            line = line.strip()
            if line.startswith("- [ ]"):
                feat_name = line[5:].strip().split("—")[0].split("–")[0].strip()
                if feat_name and feat_name not in completed:
                    feature = feat_name
                    break

    if not feature:
        feature = "unknown"

    # Stop conditions
    stop, stop_reason = should_stop(results_tsv, feature)
    if stop:
        release_lock(evolve_dir)
        return {
            "action": "stop",
            "reason": stop_reason,
            "progress": progress,
            "report": generate_report(results_tsv),
        }

    # Check if all features done
    if spec_content:
        all_features = []
        for line in spec_content.split("\n"):
            line = line.strip()
            if line.startswith("- ["):
                feat_name = line[5:].strip().split("—")[0].split("–")[0].strip()
                if feat_name:
                    all_features.append(feat_name)
        completed = progress.get("completed_features", [])
        if all_features and all(f in completed for f in all_features):
            release_lock(evolve_dir)
            return {
                "action": "stop",
                "reason": "All features passed",
                "progress": progress,
                "report": generate_report(results_tsv),
            }

    # Determine action from phase
    phase = progress.get("phase", "init")
    if phase == "eval":
        action = "dispatch_C"
    else:
        action = "dispatch_B"

    # Read all context files
    files = {}
    for name in ["program.md", "spec.md", "eval.yml", "strategy.md"]:
        p = evolve_path / name
        if p.exists():
            files[name] = p.read_text()

    # results.tsv: last 20 lines
    tsv_path = evolve_path / "results.tsv"
    if tsv_path.exists():
        lines = tsv_path.read_text().strip().split("\n")
        files["results.tsv"] = "\n".join(lines[:1] + lines[-20:]) if len(lines) > 21 else "\n".join(lines)

    # Trajectory for current feature
    trajectory = analyze_trajectory(results_tsv, feature)

    return {
        "action": action,
        "phase": phase,
        "feature": feature,
        "progress": progress,
        "trajectory": trajectory,
        "files": files,
        "report": generate_report(results_tsv),
    }


# ---------------------------------------------------------------------------
# Parallel Feature Scanning
# ---------------------------------------------------------------------------

def scan_all_features(evolve_dir: str) -> list[dict]:
    """
    Parse spec.md and results.tsv to determine each feature's dispatch readiness.

    Returns list of dicts (in spec.md order):
        name:             feature name from spec.md
        state:            "not_started" | "needs_build" | "needs_eval" | "completed"
        in_progress:      None | "B" | "C" (from per-feature lock file)
        last_status:      last results.tsv status for this feature, or None
        eval_count:       number of eval rows for this feature
        consecutive_fails: consecutive eval fails (reset on pass or build/keep)
    """
    evolve_path = Path(evolve_dir)
    results_tsv = str(evolve_path / "results.tsv")

    # Parse all features from spec.md (only unchecked items)
    all_features = []
    spec_path = evolve_path / "spec.md"
    if spec_path.exists():
        for line in spec_path.read_text().split("\n"):
            line = line.strip()
            if line.startswith("- [ ]"):
                feat_name = line[5:].strip().split("—")[0].split("–")[0].strip()
                if feat_name:
                    all_features.append(feat_name)

    # Parse results.tsv for per-feature state
    rows = []
    tsv_path = Path(results_tsv)
    if tsv_path.exists() and tsv_path.stat().st_size > 0:
        with open(tsv_path, newline="") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))

    # Build per-feature info
    feature_rows = {}
    for row in rows:
        feat = row.get("feature", "-")
        if feat != "-":
            feature_rows.setdefault(feat, []).append(row)

    result = []
    for feat_name in all_features:
        feat_data = feature_rows.get(feat_name, [])
        info = {
            "name": feat_name,
            "state": "not_started",
            "in_progress": None,
            "last_status": None,
            "eval_count": 0,
            "consecutive_fails": 0,
        }

        if feat_data:
            last = feat_data[-1]
            last_phase = last.get("phase", "")
            last_status = last.get("status", "")
            info["last_status"] = last_status
            info["eval_count"] = sum(
                1 for r in feat_data if r.get("phase") == "eval"
            )

            # Count consecutive fails. Real round cadence is
            # build/keep -> eval/fail every round, so a build between
            # fails does not end the fail streak -- only eval/pass or
            # status=reset do (matches read_progress's counting semantics).
            for r in reversed(feat_data):
                if r.get("status") == "reset":
                    break
                if r.get("phase") == "eval" and r.get("status") == "fail":
                    info["consecutive_fails"] += 1
                elif r.get("phase") == "eval" and r.get("status") == "pass":
                    break
                elif r.get("phase") == "build" and r.get("status") == "keep":
                    continue

            # Determine state
            if last_phase == "eval" and last_status in ("pass", "forced"):
                info["state"] = "completed"
            elif last_phase == "build" and last_status == "keep":
                info["state"] = "needs_eval"
            elif last_phase == "build" and last_status == "crash":
                info["state"] = "needs_build"
            elif last_phase == "eval" and last_status == "fail":
                info["state"] = "needs_build"
            else:
                info["state"] = "needs_build"

        # Branching round in flight overrides build/eval states
        branching_json = evolve_path / feat_name / "branching.json"
        if info["state"] != "completed" and branching_json.exists():
            try:
                bstate = json.loads(branching_json.read_text())
                if not bstate.get("completed"):
                    info["state"] = "branching"
            except (json.JSONDecodeError, OSError):
                pass

        # Check per-feature lock
        feat_lock = evolve_path / feat_name / "lock"
        if feat_lock.exists():
            try:
                lock_data = json.loads(feat_lock.read_text())
                elapsed = time.time() - lock_data.get("heartbeat", 0)
                if elapsed < LOCK_STALE_SECONDS:
                    info["in_progress"] = lock_data.get("agent", "?")
            except (json.JSONDecodeError, OSError):
                pass

        result.append(info)

    return result


def _generate_lock_token() -> str:
    """Generate a random token for lock ownership verification."""
    import random
    return f"{os.getpid()}-{random.randint(100000, 999999)}-{time.time()}"


def acquire_build_lock(evolve_dir: str) -> dict:
    """
    Acquire the global B-exclusive lock. Only one B agent can run at a time.
    Uses atomic file creation (O_CREAT | O_EXCL) to prevent TOCTOU races.

    Uses BUILD_LOCK_STALE_SECONDS (not LOCK_STALE_SECONDS) because this
    lock guards merge_feature's integration cascade, which can run for
    minutes -- a 120s staleness window would let a second acquire steal
    the lock mid-merge.

    Returns {"acquired": True/False, "reason": ..., "feature": ..., "token": ...}.
    """
    lock_path = Path(evolve_dir) / "build_lock"

    # Check if existing lock is stale
    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text())
            elapsed = time.time() - data.get("heartbeat", 0)
            if elapsed < BUILD_LOCK_STALE_SECONDS:
                return {
                    "acquired": False,
                    "reason": f"B agent active on {data.get('feature', '?')} "
                              f"({int(elapsed)}s ago)",
                    "feature": data.get("feature"),
                    "token": None,
                }
            # Stale — remove and try atomic create
            lock_path.unlink(missing_ok=True)
        except (json.JSONDecodeError, OSError):
            lock_path.unlink(missing_ok=True)

    # Atomic creation
    token = _generate_lock_token()
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, json.dumps({
            "pid": os.getpid(),
            "token": token,
            "heartbeat": time.time(),
        }).encode())
        os.close(fd)
        return {"acquired": True, "reason": None, "feature": None, "token": token}
    except FileExistsError:
        # Another process won the race
        return {
            "acquired": False,
            "reason": "Lost lock race to another process",
            "feature": None,
            "token": None,
        }


def release_build_lock(evolve_dir: str, token: str = None) -> None:
    """Release the global B-exclusive lock. Verifies ownership via token if provided."""
    lock_path = Path(evolve_dir) / "build_lock"
    if not lock_path.exists():
        return
    if token:
        try:
            data = json.loads(lock_path.read_text())
            if data.get("token") != token:
                return  # Not our lock, don't delete
        except (json.JSONDecodeError, OSError):
            pass
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def acquire_feature_lock(evolve_dir: str, feature: str, agent: str) -> dict:
    """
    Acquire per-feature lock for B or C agent.
    Uses atomic file creation to prevent TOCTOU races.

    Per-feature isolation only: this does NOT touch the global build_lock.
    B and C agents alike take only the per-feature lock, so multiple B
    agents can run in parallel across different features (each in its own
    worktree). The build_lock is merge_feature's concern — it serializes
    only the true critical section (merging a feature branch into
    evolve/<tag>), not feature dispatch.
    Returns {"acquired": True/False, "reason": ..., "token": ...}.
    """
    # Sanitize feature name to prevent path traversal
    if ".." in feature or feature.startswith("/"):
        return {"acquired": False, "reason": f"Invalid feature name: {feature!r}",
                "token": None}

    feat_dir = Path(evolve_dir) / feature
    feat_dir.mkdir(parents=True, exist_ok=True)
    lock_path = feat_dir / "lock"

    # Check for stale feature lock
    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text())
            elapsed = time.time() - data.get("heartbeat", 0)
            if elapsed < LOCK_STALE_SECONDS:
                return {
                    "acquired": False,
                    "reason": f"{data.get('agent', '?')} active on {feature}",
                    "token": None,
                }
            lock_path.unlink(missing_ok=True)
        except (json.JSONDecodeError, OSError):
            lock_path.unlink(missing_ok=True)

    # Atomic creation
    token = _generate_lock_token()
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, json.dumps({
            "pid": os.getpid(),
            "token": token,
            "agent": agent,
            "feature": feature,
            "heartbeat": time.time(),
        }).encode())
        os.close(fd)
        return {"acquired": True, "reason": None, "token": token}
    except FileExistsError:
        return {
            "acquired": False,
            "reason": f"Lost feature lock race on {feature}",
            "token": None,
        }


def release_feature_lock(evolve_dir: str, feature: str, token: str = None) -> None:
    """
    Release per-feature lock. Verifies ownership via token if provided.

    Backward compat: lock files written by older sessions may still carry a
    stored build_token (from when acquire_feature_lock took the global
    build_lock for agent "B"). If present, it is released here too. New
    locks written by the current acquire_feature_lock never set build_token.
    """
    feat_lock = Path(evolve_dir) / feature / "lock"
    if not feat_lock.exists():
        return

    build_token = None
    try:
        data = json.loads(feat_lock.read_text())
        if token and data.get("token") != token:
            return  # Not our lock
        build_token = data.get("build_token")
    except (json.JSONDecodeError, OSError):
        pass

    try:
        feat_lock.unlink(missing_ok=True)
    except OSError:
        pass

    if build_token:
        release_build_lock(evolve_dir, build_token)


# ---------------------------------------------------------------------------
# Lock (concurrency guard for /loop)
# ---------------------------------------------------------------------------

LOCK_STALE_SECONDS = 120  # 2 minutes -- if heartbeat older than this, lock is stale
BUILD_LOCK_STALE_SECONDS = 1800  # merge cascade can run for minutes; steal only after 30 min


def acquire_lock(evolve_dir: str) -> dict:
    """
    Try to acquire the evolve lock.

    Returns {"acquired": True/False, "reason": ..., "owner": ...}.
    If another session's heartbeat is fresh (< LOCK_STALE_SECONDS), refuse.
    If lock is stale or absent, acquire it.
    """
    lock_path = Path(evolve_dir) / "lock"

    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text())
            heartbeat = data.get("heartbeat", data.get("started", 0))
            elapsed = time.time() - heartbeat
            if elapsed < LOCK_STALE_SECONDS:
                return {
                    "acquired": False,
                    "reason": (
                        f"Another session is active "
                        f"(phase={data.get('phase','?')}, "
                        f"heartbeat {int(elapsed)}s ago)"
                    ),
                    "owner": data,
                }
            # Stale lock -- take over
        except (json.JSONDecodeError, KeyError, OSError):
            pass  # Corrupted lock, take over

    # Write lock
    lock_data = {
        "pid": os.getpid(),
        "started": time.time(),
        "heartbeat": time.time(),
        "phase": "starting",
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock_data))

    # Write started_at on first-ever lock acquisition (loop start time)
    started_at_path = lock_path.parent / "started_at"
    if not started_at_path.exists():
        started_at_path.write_text(str(lock_data["started"]))

    # Best-effort worktree debris cleanup (crashed sessions leave none).
    # Must never block the loop -- swallow everything.
    try:
        from worktree import prune_stale_worktrees
        prune_stale_worktrees(evolve_dir)
    except Exception:
        pass

    return {"acquired": True, "reason": None, "owner": lock_data}


def update_lock(evolve_dir: str, phase: str, feature: str = None) -> None:
    """Update lock heartbeat + current phase. Call at every major step."""
    lock_path = Path(evolve_dir) / "lock"
    try:
        data = json.loads(lock_path.read_text()) if lock_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    data["heartbeat"] = time.time()
    data["phase"] = phase
    if feature:
        data["feature"] = feature
    lock_path.write_text(json.dumps(data))


def release_lock(evolve_dir: str) -> None:
    """Delete the lock file. Call when the session finishes."""
    lock_path = Path(evolve_dir) / "lock"
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Re-exports (new modules; imported at end of file to avoid circular imports)
# ---------------------------------------------------------------------------

from cascade import load_cascade_config, run_cascade, DEFAULT_STAGE_TIMEOUT  # noqa: E402,F401
from worktree import (create_feature_worktree, remove_feature_worktree,  # noqa: E402,F401
                      merge_feature, prune_stale_worktrees, feature_slug,
                      feature_branch, worktree_path, base_branch)
from population import (should_branch, spawn_candidates, select_candidate,  # noqa: E402,F401
                        can_force_pass, mark_forced_pass, parent_feature,
                        candidate_feature_id,
                        BRANCH_AFTER_CONSECUTIVE_FAILS)
