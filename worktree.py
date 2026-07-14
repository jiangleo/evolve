"""
worktree.py -- Git worktree isolation for parallel Builders.

Each feature's B works in .evolve/worktrees/{slug} on branch
{base}--{slug}, where {base} is the evolve/<tag> branch of the MAIN
working tree. All functions here must be called from the main working
tree, not from inside a worktree.

Agent MUST NOT modify this file.

Import convention: imports from prepare/cascade happen lazily inside
function bodies (prepare re-exports this module at its end).
"""

import re
import subprocess
from pathlib import Path
from typing import Optional


def _repo_root(evolve_dir: str) -> str:
    """The project repo root is the parent of .evolve/."""
    return str(Path(evolve_dir).resolve().parent)


def _git(args: list, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd,
                          capture_output=True, text=True)


def feature_slug(feature: str) -> str:
    """Sanitize a feature name into a branch/dir-safe slug."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", feature).strip("-.")
    return slug or "feature"


def base_branch(evolve_dir: str) -> str:
    """Current branch of the main working tree (the evolve/<tag> branch)."""
    proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], _repo_root(evolve_dir))
    if proc.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def feature_branch(evolve_dir: str, feature: str) -> str:
    return f"{base_branch(evolve_dir)}--{feature_slug(feature)}"


def worktree_path(evolve_dir: str, feature: str) -> str:
    return str(Path(evolve_dir) / "worktrees" / feature_slug(feature))


def create_feature_worktree(evolve_dir: str, feature: str,
                            from_branch: Optional[str] = None) -> dict:
    """Create (or reuse) the worktree + branch for a feature.

    Returns {"path": str, "branch": str, "created": bool}.
    Raises RuntimeError on git failure.
    """
    root = _repo_root(evolve_dir)
    branch = feature_branch(evolve_dir, feature)
    path = worktree_path(evolve_dir, feature)

    if Path(path).exists():
        return {"path": path, "branch": branch, "created": False}

    Path(path).parent.mkdir(parents=True, exist_ok=True)

    if _git(["rev-parse", "--verify", "refs/heads/" + branch],
            root).returncode != 0:
        start = from_branch or base_branch(evolve_dir)
        proc = _git(["branch", branch, start], root)
        if proc.returncode != 0:
            raise RuntimeError(f"git branch failed: {proc.stderr.strip()}")

    proc = _git(["worktree", "add", path, branch], root)
    if proc.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")

    return {"path": path, "branch": branch, "created": True}


def remove_feature_worktree(evolve_dir: str, feature: str,
                            delete_branch: bool = True) -> None:
    """Remove a feature's worktree (and branch). Idempotent.

    Removing an already-missing worktree/branch is a no-op, but real
    git failures (e.g. dirty/locked worktree) raise RuntimeError with
    the underlying stderr.
    """
    root = _repo_root(evolve_dir)
    path = worktree_path(evolve_dir, feature)

    proc = _git(["worktree", "remove", "--force", path], root)
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        stderr_lower = stderr.lower()
        already_gone = (
            not Path(path).exists()
            or "is not a working tree" in stderr_lower
            or "no such file or directory" in stderr_lower
        )
        if not already_gone:
            raise RuntimeError(f"git worktree remove failed: {stderr}")

    # Best-effort advisory cleanup; failures here are not actionable.
    _git(["worktree", "prune"], root)

    if delete_branch:
        branch = feature_branch(evolve_dir, feature)
        proc = _git(["branch", "-D", branch], root)
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            if "not found" not in stderr.lower():
                raise RuntimeError(f"git branch -D failed: {stderr}")


def _write_merge_conflict(evolve_dir: str, feature: str,
                          reason: str, detail: str) -> None:
    feat_dir = Path(evolve_dir) / feature
    feat_dir.mkdir(parents=True, exist_ok=True)
    (feat_dir / "merge_conflict.md").write_text(
        f"# Merge Gate Failure\n\nreason: {reason}\n\n"
        f"## Detail\n\n```\n{detail}\n```\n"
    )


def merge_feature(evolve_dir: str, feature: str, cascade_stages: list = None,
                  health_check=None, branch: str = None) -> dict:
    """Merge a feature branch into the base branch behind the integration
    gate: after merging, the deterministic cascade re-runs on the merged
    tree. Gate failure fully reverts the merge.

    branch: source branch override (population branching merges a
    candidate branch under the parent feature's name). Defaults to the
    feature's own branch.

    Holds build_lock for the whole merge — the only critical section it
    still protects.

    Returns {"status": "merged"|"gate_fail"|"locked", "detail": str}.
    """
    from prepare import acquire_build_lock, release_build_lock
    from cascade import run_cascade

    root = _repo_root(evolve_dir)
    source = branch or feature_branch(evolve_dir, feature)

    bl = acquire_build_lock(evolve_dir)
    if not bl["acquired"]:
        return {"status": "locked", "detail": bl["reason"]}

    try:
        proc = _git(["merge", "--no-ff", "-m",
                     f"merge: {feature} (evolve integration)", source], root)
        if proc.returncode != 0:
            _git(["merge", "--abort"], root)
            detail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
            _write_merge_conflict(evolve_dir, feature,
                                  "merge conflict", detail)
            return {"status": "gate_fail", "detail": detail}

        gate = run_cascade(evolve_dir, feature, cascade_stages or [],
                           cwd=root, health_check=health_check)
        if gate["status"] != "passed":
            _git(["reset", "--hard", "ORIG_HEAD"], root)
            detail = (f"integration cascade failed at stage "
                      f"'{gate['failed_stage']}':\n{gate['output_tail']}")
            _write_merge_conflict(evolve_dir, feature,
                                  "integration cascade regression", detail)
            return {"status": "gate_fail", "detail": detail}

        remove_feature_worktree(evolve_dir, feature, delete_branch=True)
        return {"status": "merged", "detail": ""}
    finally:
        release_build_lock(evolve_dir, bl["token"])


def prune_stale_worktrees(evolve_dir: str) -> list:
    """Remove worktree debris: git-level pruning plus worktrees whose
    feature is already completed. Returns list of removed slugs.

    A "completed" feature (eval pass in results.tsv) does not imply its
    branch is merged into the base branch -- a session can crash between
    eval pass and merge, leaving committed work only on the feature
    branch. Only remove a completed entry's worktree+branch when
    `git merge-base --is-ancestor` confirms the branch is fully contained
    in the base branch; otherwise leave the worktree and branch intact
    for the orchestrator to merge later. Per-entry removal failures are
    swallowed (best-effort): one entry's failure never aborts cleanup of
    the others.
    """
    from prepare import scan_all_features

    root = _repo_root(evolve_dir)
    _git(["worktree", "prune"], root)

    removed = []
    wt_root = Path(evolve_dir) / "worktrees"
    if not wt_root.exists():
        return removed

    completed = {feature_slug(f["name"])
                 for f in scan_all_features(evolve_dir)
                 if f["state"] == "completed"}

    base = base_branch(evolve_dir)

    for entry in wt_root.iterdir():
        if not (entry.is_dir() and entry.name in completed):
            continue

        branch = feature_branch(evolve_dir, entry.name)
        merged = _git(
            ["merge-base", "--is-ancestor", f"refs/heads/{branch}", base],
            root).returncode == 0
        if not merged:
            continue

        try:
            remove_feature_worktree(evolve_dir, entry.name)
            removed.append(entry.name)
        except Exception:
            pass
    return removed
