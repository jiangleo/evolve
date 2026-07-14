import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from worktree import (feature_slug, base_branch, feature_branch,
                      worktree_path, create_feature_worktree,
                      remove_feature_worktree)


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True,
                          capture_output=True, text=True)


def _git_repo(tmp_path):
    """Init a repo on branch evolve/demo with one commit and .evolve/."""
    _run(["git", "init", "-b", "evolve/demo", "."], tmp_path)
    _run(["git", "config", "user.email", "t@example.com"], tmp_path)
    _run(["git", "config", "user.name", "t"], tmp_path)
    (tmp_path / "app.txt").write_text("v1\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "init"], tmp_path)
    evolve = tmp_path / ".evolve"
    evolve.mkdir()
    return str(evolve)


def test_feature_slug_sanitizes():
    assert feature_slug("F01 Pattern Mirror") == "F01-Pattern-Mirror"
    assert feature_slug("a/b:c") == "a-b-c"


def test_branch_and_path_naming(tmp_path):
    evolve = _git_repo(tmp_path)
    assert base_branch(evolve) == "evolve/demo"
    assert feature_branch(evolve, "F01 auth") == "evolve/demo--F01-auth"
    assert worktree_path(evolve, "F01 auth").endswith(
        ".evolve/worktrees/F01-auth")


def test_create_and_remove_worktree(tmp_path):
    evolve = _git_repo(tmp_path)
    wt = create_feature_worktree(evolve, "F01 auth")
    assert wt["created"] is True
    assert Path(wt["path"], "app.txt").exists()
    # branch exists
    r = subprocess.run(["git", "rev-parse", "--verify",
                        "evolve/demo--F01-auth"],
                       cwd=tmp_path, capture_output=True)
    assert r.returncode == 0
    # idempotent
    again = create_feature_worktree(evolve, "F01 auth")
    assert again["created"] is False
    assert again["path"] == wt["path"]

    remove_feature_worktree(evolve, "F01 auth")
    assert not Path(wt["path"]).exists()
    r = subprocess.run(["git", "rev-parse", "--verify",
                        "evolve/demo--F01-auth"],
                       cwd=tmp_path, capture_output=True)
    assert r.returncode != 0            # branch deleted too


def test_create_from_explicit_branch(tmp_path):
    evolve = _git_repo(tmp_path)
    _run(["git", "branch", "other-base"], tmp_path)
    wt = create_feature_worktree(evolve, "F02", from_branch="other-base")
    assert wt["branch"] == "evolve/demo--F02"
    assert Path(wt["path"]).exists()


def test_remove_nonexistent_worktree_is_noop(tmp_path):
    evolve = _git_repo(tmp_path)
    # never created — must not raise
    remove_feature_worktree(evolve, "GHOST")


def test_remove_is_idempotent(tmp_path):
    evolve = _git_repo(tmp_path)
    create_feature_worktree(evolve, "F01")
    remove_feature_worktree(evolve, "F01")
    remove_feature_worktree(evolve, "F01")   # second call must not raise


from worktree import merge_feature


def _commit_in_worktree(wt_path, fname, content, msg):
    p = Path(wt_path) / fname
    p.write_text(content)
    _run(["git", "add", "."], wt_path)
    _run(["git", "commit", "-m", msg], wt_path)


def test_merge_feature_happy_path(tmp_path):
    evolve = _git_repo(tmp_path)
    wt = create_feature_worktree(evolve, "F01")
    _commit_in_worktree(wt["path"], "feature.txt", "done\n", "feat: F01")

    result = merge_feature(evolve, "F01")
    assert result["status"] == "merged"
    assert (tmp_path / "feature.txt").exists()      # landed on evolve/demo
    assert not Path(wt["path"]).exists()            # worktree cleaned up


def test_merge_feature_integration_gate_reverts(tmp_path):
    evolve = _git_repo(tmp_path)
    wt = create_feature_worktree(evolve, "F01")
    _commit_in_worktree(wt["path"], "feature.txt", "done\n", "feat: F01")

    head_before = _run(["git", "rev-parse", "HEAD"], tmp_path).stdout.strip()
    result = merge_feature(evolve, "F01", cascade_stages=[
        {"name": "smoke", "cmd": "false", "timeout": 10}])
    assert result["status"] == "gate_fail"
    head_after = _run(["git", "rev-parse", "HEAD"], tmp_path).stdout.strip()
    assert head_after == head_before                # merge reverted
    assert not (tmp_path / "feature.txt").exists()
    conflict = tmp_path / ".evolve" / "F01" / "merge_conflict.md"
    assert conflict.exists()
    assert "smoke" in conflict.read_text()


def test_merge_feature_conflict_aborts(tmp_path):
    evolve = _git_repo(tmp_path)
    wt = create_feature_worktree(evolve, "F01")
    _commit_in_worktree(wt["path"], "app.txt", "worktree version\n", "feat")
    # conflicting change on the base branch
    (tmp_path / "app.txt").write_text("main version\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "conflicting"], tmp_path)

    result = merge_feature(evolve, "F01")
    assert result["status"] == "gate_fail"
    assert (tmp_path / "app.txt").read_text() == "main version\n"
    assert (tmp_path / ".evolve" / "F01" / "merge_conflict.md").exists()


from worktree import prune_stale_worktrees


def test_prune_removes_completed_feature_worktrees(tmp_path):
    evolve = _git_repo(tmp_path)
    # spec.md so scan_all_features sees the features
    (Path(evolve) / "spec.md").write_text("- [ ] F01\n- [ ] F02\n")
    header = "commit\tphase\tfeature\tscores\ttotal\tstatus\tsummary"
    (Path(evolve) / "results.tsv").write_text(
        header + "\n"
        "a\teval\tF01\t9/9\t9.0\tpass\tdone\n"
        "b\tbuild\tF02\t-\t-\tkeep\twip\n"
    )
    wt1 = create_feature_worktree(evolve, "F01")
    wt2 = create_feature_worktree(evolve, "F02")

    removed = prune_stale_worktrees(evolve)
    assert "F01" in removed
    assert not Path(wt1["path"]).exists()      # completed -> pruned
    assert Path(wt2["path"]).exists()          # in progress -> kept


def test_acquire_lock_prunes_best_effort(tmp_path):
    # acquire_lock on a NON-git dir must still work (prune errors swallowed)
    from prepare import acquire_lock, release_lock
    evolve = tmp_path / ".evolve"
    evolve.mkdir()
    lock = acquire_lock(str(evolve))
    assert lock["acquired"] is True
    release_lock(str(evolve))


def test_prune_keeps_unmerged_completed_branch(tmp_path):
    evolve = _git_repo(tmp_path)
    (Path(evolve) / "spec.md").write_text("- [ ] F01\n")
    header = "commit\tphase\tfeature\tscores\ttotal\tstatus\tsummary"
    (Path(evolve) / "results.tsv").write_text(
        header + "\n" + "a\teval\tF01\t9/9\t9.0\tpass\tdone\n")
    wt = create_feature_worktree(evolve, "F01")
    _commit_in_worktree(wt["path"], "unmerged.txt", "work\n", "feat: F01")

    removed = prune_stale_worktrees(evolve)
    assert removed == []                       # unmerged -> kept
    assert Path(wt["path"]).exists()


def test_prune_isolates_per_entry_failures(tmp_path, monkeypatch):
    evolve = _git_repo(tmp_path)
    (Path(evolve) / "spec.md").write_text("- [ ] F01\n- [ ] F02\n")
    header = "commit\tphase\tfeature\tscores\ttotal\tstatus\tsummary"
    (Path(evolve) / "results.tsv").write_text(
        header + "\n"
        "a\teval\tF01\t9/9\t9.0\tpass\tdone\n"
        "b\teval\tF02\t9/9\t9.0\tpass\tdone\n")
    create_feature_worktree(evolve, "F01")
    wt2 = create_feature_worktree(evolve, "F02")

    import worktree as wt_mod
    real_remove = wt_mod.remove_feature_worktree

    def flaky_remove(evolve_dir, feature, delete_branch=True):
        if feature == "F01":
            raise RuntimeError("simulated locked worktree")
        return real_remove(evolve_dir, feature, delete_branch)

    monkeypatch.setattr(wt_mod, "remove_feature_worktree", flaky_remove)
    removed = prune_stale_worktrees(evolve)   # must not raise
    assert removed == ["F02"]
    assert not Path(wt2["path"]).exists()
