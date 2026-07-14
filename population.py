"""
population.py -- Population branching for stuck features.

Escalation ladder (loop.md documents the full flow):
    consecutive_fails >= 3  -> Mentor advice (existing behavior)
    consecutive_fails >= 6  -> BRANCH: spawn N candidate worktrees, each
                               seeded with a DISTINCT approach
    all candidates fail     -> forced_pass becomes AVAILABLE (user approval
                               still required; enforced by can_force_pass)

Candidate rounds are recorded in results.tsv with feature id
"{feature}@cand{i}" — auditable, but never spec.md features.

Agent MUST NOT modify this file.

Import convention: imports from prepare/worktree happen lazily inside
function bodies (prepare re-exports this module at its end).
"""

import json
from pathlib import Path

CAND_SEP = "@cand"
BRANCH_AFTER_CONSECUTIVE_FAILS = 6


def parent_feature(feature_id: str) -> str:
    """'F01@cand2' -> 'F01'; plain ids pass through."""
    return feature_id.split(CAND_SEP)[0]


def candidate_feature_id(feature: str, i: int) -> str:
    return f"{feature}{CAND_SEP}{i}"


def _state_path(evolve_dir: str, feature: str) -> Path:
    return Path(evolve_dir) / feature / "branching.json"


def _branching_state(evolve_dir: str, feature: str):
    """Read branching.json, or None if no branching round was started."""
    path = _state_path(evolve_dir, feature)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_branching_state(evolve_dir: str, feature: str,
                           state: dict) -> None:
    path = _state_path(evolve_dir, feature)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def should_branch(evolve_dir: str, feature: str) -> tuple:
    """Decide whether the escalation ladder has reached the branching
    step. Returns (branch: bool, reason: str). Code-enforced — O calls
    this instead of deciding by feel.
    """
    from prepare import scan_all_features, HARD_LIMITS

    info = next((f for f in scan_all_features(evolve_dir)
                 if f["name"] == feature), None)
    if info is None:
        return False, f"unknown feature: {feature}"

    state = _branching_state(evolve_dir, feature)
    rounds_used = state.get("round", 0) if state else 0
    if rounds_used >= HARD_LIMITS["max_branching_rounds_per_feature"]:
        return False, "branching budget exhausted"

    fails = info["consecutive_fails"]
    if fails >= BRANCH_AFTER_CONSECUTIVE_FAILS:
        return True, f"{fails} consecutive fails (>= " \
                     f"{BRANCH_AFTER_CONSECUTIVE_FAILS})"
    return False, (f"{fails} consecutive fails (< "
                   f"{BRANCH_AFTER_CONSECUTIVE_FAILS})")


def spawn_candidates(evolve_dir: str, feature: str,
                     approaches: list) -> list:
    """Fork N candidate worktrees for a stuck feature, each seeded with a
    distinct approach (O draws approaches from Mentor hypotheses and C's
    untried Pivot options).

    Returns [{"cand_id", "feature_id", "path", "branch", "approach"}].
    Raises ValueError if approaches is empty.
    """
    from prepare import HARD_LIMITS
    from worktree import (create_feature_worktree, feature_branch,
                          feature_slug, base_branch, _repo_root, _git)

    if not approaches:
        raise ValueError("spawn_candidates requires at least one approach")

    n = min(len(approaches), HARD_LIMITS["candidates_per_branching"])
    root = _repo_root(evolve_dir)

    # Fork from the feature's own branch if it exists, else the base branch.
    incumbent = feature_branch(evolve_dir, feature)
    if _git(["rev-parse", "--verify", "refs/heads/" + incumbent],
            root).returncode != 0:
        incumbent = base_branch(evolve_dir)

    state = _branching_state(evolve_dir, feature) or {"round": 0}
    candidates = []
    for i in range(1, n + 1):
        cand_name = f"{feature_slug(feature)}-cand{i}"
        wt = create_feature_worktree(evolve_dir, cand_name,
                                     from_branch=incumbent)
        feature_id = candidate_feature_id(feature, i)
        cand_dir = Path(evolve_dir) / feature_id
        cand_dir.mkdir(parents=True, exist_ok=True)
        (cand_dir / "strategy.md").write_text(
            f"# Candidate {i} strategy (seeded)\n\n"
            f"## Approach\n\n{approaches[i - 1]}\n\n"
            f"## Rules\n\n- Work ONLY in worktree {wt['path']}\n"
            f"- Record results.tsv rows with feature id {feature_id}\n"
        )
        candidates.append({"cand_id": i, "feature_id": feature_id,
                           "path": wt["path"], "branch": wt["branch"],
                           "approach": approaches[i - 1]})

    _write_branching_state(evolve_dir, feature, {
        "round": state.get("round", 0) + 1,
        "completed": False,
        "winner_outcome": None,
        "candidates": [{"cand_id": c["cand_id"],
                        "feature_id": c["feature_id"],
                        "branch": c["branch"],
                        "approach": c["approach"]} for c in candidates],
    })
    return candidates


def _min_dim(scores_str: str):
    """'7/8/9' -> 7.0. Returns None if unparseable."""
    try:
        vals = [float(s) for s in scores_str.split("/")]
        return min(vals) if vals else None
    except (ValueError, AttributeError):
        return None


def _last_eval_row(rows: list, feature_id: str):
    for r in reversed(rows):
        if r.get("feature") == feature_id and r.get("phase") == "eval":
            return r
    return None


def select_candidate(evolve_dir: str, feature: str) -> dict:
    """Pick the branching round's winner from results.tsv candidate rows.

    Rules (spec §3):
      1. cascade_fail rows disqualify that candidate;
      2. a candidate whose last eval status == "pass" wins outright;
      3. otherwise the candidate with the highest MINIMUM dimension score
         (tie-break: highest total) is ADOPTED as the new lineage — but
         only if it beats the incumbent's last min-dim;
      4. else outcome "none" (forced_pass gate opens; see can_force_pass).

    On "pass" AND "adopt": resets the incumbent feature branch to the
    winner's branch BEFORE marking branching.json completed — so the
    standard merge_feature(feature) path works afterwards and EVERY
    candidate branch can be safely cleaned up here (otherwise a
    pass-winner's branch would be deleted before O merges it). If the
    branch reset fails, this raises RuntimeError and leaves
    branching.json un-completed (round stays resumable, no candidate
    branch is deleted) instead of silently orphaning the winner's commits.
    Only once the reset has succeeded are branching.json marked completed
    and winner_outcome recorded. On "adopt" additionally appends a
    status=reset row so fail counting restarts.
    """
    import csv as _csv
    from prepare import append_result
    from worktree import (feature_branch, feature_slug, _repo_root, _git,
                          remove_feature_worktree)

    state = _branching_state(evolve_dir, feature)
    if not state or not state.get("candidates"):
        return {"outcome": "none", "cand_id": None, "feature_id": None,
                "detail": "no branching round in progress"}

    tsv = Path(evolve_dir) / "results.tsv"
    rows = []
    if tsv.exists():
        with open(tsv, newline="") as f:
            rows = list(_csv.DictReader(f, delimiter="\t"))

    incumbent_row = _last_eval_row(
        rows, feature) if rows else None
    incumbent_min = _min_dim(incumbent_row["scores"]) if incumbent_row \
        else None

    best = None   # (min_dim, total, cand)
    winner = None
    for cand in state["candidates"]:
        last = _last_eval_row(rows, cand["feature_id"])
        if last is None or last.get("status") == "cascade_fail":
            continue
        if last.get("status") == "pass":
            winner = ("pass", cand)
            break
        min_dim = _min_dim(last.get("scores", ""))
        if min_dim is None:
            continue
        try:
            total = float(last.get("total", "0"))
        except (ValueError, TypeError):
            total = 0.0
        if best is None or (min_dim, total) > (best[0], best[1]):
            best = (min_dim, total, cand)

    if winner is None and best is not None and \
            (incumbent_min is None or best[0] > incumbent_min):
        winner = ("adopt", best[2])

    outcome = winner[0] if winner else "none"
    cand = winner[1] if winner else None

    if outcome in ("pass", "adopt"):
        # Point the incumbent feature branch at the winner. After this,
        # the winner's commits live on the feature branch, so all
        # candidate branches (including the winner's) are safe to delete
        # below, and O merges via the standard merge_feature(feature).
        #
        # This MUST happen (and succeed) before branching.json is marked
        # completed and before candidate cleanup runs: if the branch
        # reset fails silently, the cleanup loop below would delete every
        # candidate branch — including the winner's — orphaning its
        # commits. Raising here leaves branching.json un-completed, so
        # the round stays resumable and no candidate branch is touched.
        root = _repo_root(evolve_dir)
        incumbent_branch = feature_branch(evolve_dir, feature)
        if _git(["rev-parse", "--verify", "refs/heads/" + incumbent_branch],
                root).returncode == 0:
            # Feature worktree must not hold the branch while we move it.
            remove_feature_worktree(evolve_dir, feature,
                                    delete_branch=False)
            proc = _git(["branch", "-f", incumbent_branch, cand["branch"]],
                        root)
        else:
            proc = _git(["branch", incumbent_branch, cand["branch"]], root)
        if proc.returncode != 0:
            raise RuntimeError(
                f"git branch reset failed: {proc.stderr.strip()}")

    state["completed"] = True
    state["winner_outcome"] = outcome
    state["winner"] = cand["feature_id"] if cand else None
    _write_branching_state(evolve_dir, feature, state)

    if outcome == "adopt":
        append_result(str(tsv), {
            "commit": "-", "phase": "build", "feature": feature,
            "scores": "-", "total": "-", "status": "reset",
            "summary": f"adopted candidate {cand['cand_id']} lineage "
                       f"({cand['branch']})",
        })

    # Candidate cleanup: every candidate worktree+branch goes. Safe even
    # for the winner — its commits now live on the incumbent branch.
    for c in state["candidates"]:
        cand_name = f"{feature_slug(feature)}-cand{c['cand_id']}"
        remove_feature_worktree(evolve_dir, cand_name)

    return {"outcome": outcome,
            "cand_id": cand["cand_id"] if cand else None,
            "feature_id": cand["feature_id"] if cand else None,
            "detail": f"winner_outcome={outcome}"}


def can_force_pass(evolve_dir: str, feature: str) -> tuple:
    """forced_pass gate: open ONLY after a completed branching round
    produced no passing winner. Returns (ok: bool, reason: str)."""
    state = _branching_state(evolve_dir, feature)
    if state is None:
        return False, "gate closed: no branching round attempted yet"
    if not state.get("completed"):
        return False, "gate closed: branching round still in flight"
    if state.get("winner_outcome") == "pass":
        return False, "gate closed: branching produced a passing winner"
    return True, ("branching round completed with no passing candidate "
                  f"(winner_outcome={state.get('winner_outcome')})")


def mark_forced_pass(evolve_dir: str, feature: str,
                     user_approved: bool) -> None:
    """The ONLY sanctioned path to a status=forced row.

    Raises ValueError if the gate is closed or approval is missing.
    """
    ok, reason = can_force_pass(evolve_dir, feature)
    if not ok:
        raise ValueError(f"forced_pass gate: {reason}")
    if user_approved is not True:
        raise ValueError("forced_pass requires explicit user approval")

    from prepare import append_result
    append_result(str(Path(evolve_dir) / "results.tsv"), {
        "commit": "-", "phase": "eval", "feature": feature,
        "scores": "-", "total": "-", "status": "forced",
        "summary": f"forced_pass approved by user ({reason})",
    })
