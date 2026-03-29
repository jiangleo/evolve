"""
prepare.py -- Generic evaluation engine for Evolve skill.
Agent MUST NOT modify this file.

Generic engine: append_result, read_progress, generate_report,
                load_adapter, load_eval_config.
Domain-specific logic lives in .evolve/adapter.py (auto-generated during Init).
Reference adapters in adapters/ are for Agent to read during Init, not imported at runtime.
"""

import csv
import importlib.util
import json
import os
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADER_FIELDS = ["commit", "phase", "feature", "scores", "total", "status", "summary"]
VALID_PHASES = {"plan", "build", "contract", "eval"}
VALID_STATUSES = {"keep", "pass", "fail", "crash", "skip", "reset"}

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

    Returns list of dicts with keys: name, type, threshold, and optionally cmd.
    """
    path = Path(eval_yml_path)
    if not path.exists():
        raise FileNotFoundError(f"eval.yml not found: {eval_yml_path}")

    content = path.read_text()
    dimensions = []
    current = None

    for line in content.split('\n'):
        stripped = line.strip()
        if stripped == '' or stripped.startswith('#'):
            continue
        if stripped.startswith('- name:'):
            if current:
                dimensions.append(current)
            name = stripped.split(':', 1)[1].strip()
            current = {"name": name, "type": "llm-judged", "threshold": 7.0}
        elif current and stripped.startswith('type:'):
            val = stripped.split(':', 1)[1].strip()
            if val not in ("deterministic", "llm-judged"):
                raise ValueError(
                    f"Invalid type '{val}' for dimension '{current['name']}'. "
                    f"Must be 'deterministic' or 'llm-judged'."
                )
            current["type"] = val
        elif current and stripped.startswith('cmd:'):
            current["cmd"] = stripped.split(':', 1)[1].strip()
        elif current and stripped.startswith('threshold:'):
            current["threshold"] = float(stripped.split(':', 1)[1].strip())

    if current:
        dimensions.append(current)

    return dimensions

# ---------------------------------------------------------------------------
# TSV Helpers
# ---------------------------------------------------------------------------

def append_result(results_tsv: str, row: dict) -> None:
    """Append one row to results.tsv. Creates file with header if it doesn't exist."""
    path = Path(results_tsv)
    write_header = not path.exists() or path.stat().st_size == 0

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER_FIELDS, delimiter="\t",
                                extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def read_progress(results_tsv: str) -> dict:
    """
    Read results.tsv and return current phase + decision info.

    State machine:
    - No data rows -> init
    - Last row plan/keep -> build
    - Last row build/keep -> eval
    - Last row build/crash -> build (retry or skip)
    - Last row eval/pass -> build (next feature)
    - Last row eval/fail -> build (fix) or reset
    - Last row eval/skip -> build (next feature)
    - Last row contract/pass -> build (start coding)
    - Last row contract/fail -> build (rewrite contract)
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
        "skipped_features": [],
        "last_pass_commit": None,
    }

    if not rows:
        return result

    # Collect completed and skipped features
    for row in rows:
        phase = row.get("phase", "")
        status = row.get("status", "")
        feature = row.get("feature", "-")
        commit = row.get("commit", "")

        if phase == "eval" and status == "pass":
            if feature not in result["completed_features"] and feature != "-":
                result["completed_features"].append(feature)
            result["last_pass_commit"] = commit
        elif status == "skip":
            if feature not in result["skipped_features"] and feature != "-":
                result["skipped_features"].append(feature)

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
        elif row.get("phase") in ("plan", "contract"):
            break
    result["has_been_reset"] = has_been_reset

    # Determine current phase from last row
    if last_phase == "plan" and last_status == "keep":
        result["phase"] = "build"
    elif last_phase == "build" and last_status == "keep":
        result["phase"] = "eval"
        result["current_feature"] = last_feature if last_feature != "-" else None
    elif last_phase == "build" and last_status == "crash":
        result["phase"] = "build"
        result["current_feature"] = last_feature if last_feature != "-" else None
    elif last_phase == "contract" and last_status == "pass":
        result["phase"] = "build"
        result["current_feature"] = last_feature if last_feature != "-" else None
    elif last_phase == "contract" and last_status == "fail":
        result["phase"] = "build"
        result["current_feature"] = last_feature if last_feature != "-" else None
    elif last_phase == "eval" and last_status == "pass":
        result["phase"] = "build"
    elif last_phase == "eval" and last_status == "fail":
        result["phase"] = "build"
        result["current_feature"] = last_feature if last_feature != "-" else None
    elif last_phase == "eval" and last_status == "skip":
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
        feat = row.get("feature", "-")
        if feat == "-":
            continue
        if feat not in features:
            features[feat] = {"rows": [], "final_status": None,
                              "final_total": None, "pass_round": None}
        features[feat]["rows"].append(row)
        features[feat]["final_status"] = row.get("status")
        if row.get("total", "-") != "-":
            features[feat]["final_total"] = row.get("total")
        if row.get("phase") == "eval" and row.get("status") == "pass":
            features[feat]["pass_round"] = sum(
                1 for r in features[feat]["rows"] if r.get("phase") == "eval"
            )

    completed = [f for f, i in features.items() if i["final_status"] == "pass"]
    skipped = [f for f, i in features.items() if i["final_status"] == "skip"]
    total_features = len(features)

    # Find current feature (last non-completed, non-skipped)
    current_feat = None
    for feat, info in features.items():
        if info["final_status"] not in ("pass", "skip"):
            current_feat = feat

    # Build report
    lines = ["# Evolve Progress", ""]

    # Status line
    if not features:
        lines.append("## Status: Waiting to start")
    elif len(completed) + len(skipped) == total_features and total_features > 0:
        lines.append(f"## Status: Complete -- Round {total_rounds} | All passed")
    else:
        lines.append(f"## Status: In Progress -- Round {total_rounds}")

    lines.append("")

    # Overview
    lines.append("## Overview")
    if current_feat:
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
# Lock (concurrency guard for /loop)
# ---------------------------------------------------------------------------

LOCK_STALE_SECONDS = 120  # 2 minutes -- if heartbeat older than this, lock is stale


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
