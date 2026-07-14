import json
import subprocess
from pathlib import Path

import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from population import (parent_feature, candidate_feature_id,
                        should_branch, BRANCH_AFTER_CONSECUTIVE_FAILS,
                        spawn_candidates, select_candidate,
                        can_force_pass, mark_forced_pass)
from prepare import HARD_LIMITS

HEADER = "commit\tphase\tfeature\tscores\ttotal\tstatus\tsummary"


def _evolve(tmp_path, features=("F01",), rows=()):
    evolve = tmp_path / ".evolve"
    evolve.mkdir(exist_ok=True)
    (evolve / "spec.md").write_text(
        "".join(f"- [ ] {f}\n" for f in features))
    (evolve / "results.tsv").write_text(
        HEADER + "\n" + "".join("\t".join(r) + "\n" for r in rows))
    return str(evolve)


def _fail_rows(feature, n):
    return [[f"c{i}", "eval", feature, "6/6", "6.0", "fail", f"r{i}"]
            for i in range(n)]


def test_candidate_id_roundtrip():
    cid = candidate_feature_id("F01", 2)
    assert cid == "F01@cand2"
    assert parent_feature(cid) == "F01"
    assert parent_feature("F01") == "F01"


def test_hard_limits_extended():
    assert HARD_LIMITS["max_branching_rounds_per_feature"] == 1
    assert HARD_LIMITS["candidates_per_branching"] == 3


def test_should_branch_below_threshold(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 3))
    branch, reason = should_branch(evolve, "F01")
    assert branch is False


def test_should_branch_at_threshold(tmp_path):
    evolve = _evolve(tmp_path,
                     rows=_fail_rows("F01", BRANCH_AFTER_CONSECUTIVE_FAILS))
    branch, reason = should_branch(evolve, "F01")
    assert branch is True
    assert "consecutive fails" in reason


def test_should_branch_budget_exhausted(tmp_path):
    evolve = _evolve(tmp_path,
                     rows=_fail_rows("F01", BRANCH_AFTER_CONSECUTIVE_FAILS))
    state_dir = Path(evolve) / "F01"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "branching.json").write_text(json.dumps(
        {"round": 1, "completed": True, "winner_outcome": "none"}))
    branch, reason = should_branch(evolve, "F01")
    assert branch is False
    assert "budget" in reason


def test_should_branch_with_realistic_build_interleaved_history(tmp_path):
    # Real cadence: build/keep then eval/fail every round. The fail streak
    # must accumulate across builds, or branching can never trigger.
    rows = []
    for i in range(BRANCH_AFTER_CONSECUTIVE_FAILS):
        rows.append([f"b{i}", "build", "F01", "-", "-", "keep", f"build {i}"])
        rows.append([f"e{i}", "eval", "F01", "6/6", "6.0", "fail", f"round {i}"])
    evolve = _evolve(tmp_path, rows=rows)
    branch, reason = should_branch(evolve, "F01")
    assert branch is True


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True,
                          capture_output=True, text=True)


def _git_evolve(tmp_path, rows=()):
    _run(["git", "init", "-b", "evolve/demo", "."], tmp_path)
    _run(["git", "config", "user.email", "t@example.com"], tmp_path)
    _run(["git", "config", "user.name", "t"], tmp_path)
    (tmp_path / "app.txt").write_text("v1\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "init"], tmp_path)
    evolve = tmp_path / ".evolve"
    evolve.mkdir()
    (evolve / "spec.md").write_text("- [ ] F01\n")
    (evolve / "results.tsv").write_text(
        HEADER + "\n" + "".join("\t".join(r) + "\n" for r in rows))
    return str(evolve)


def test_spawn_candidates_creates_worktrees_and_seeds(tmp_path):
    evolve = _git_evolve(tmp_path)
    cands = spawn_candidates(evolve, "F01",
                             ["approach A", "approach B", "approach C"])
    assert len(cands) == 3
    for i, cand in enumerate(cands, 1):
        assert cand["feature_id"] == f"F01@cand{i}"
        assert Path(cand["path"], "app.txt").exists()
        strategy = Path(evolve) / cand["feature_id"] / "strategy.md"
        assert cand["approach"] in strategy.read_text()
    state = json.loads((Path(evolve) / "F01" / "branching.json").read_text())
    assert state["round"] == 1
    assert state["completed"] is False
    assert len(state["candidates"]) == 3


def test_spawn_candidates_caps_at_budget(tmp_path):
    evolve = _git_evolve(tmp_path)
    cands = spawn_candidates(evolve, "F01", ["a", "b", "c", "d", "e"])
    assert len(cands) == HARD_LIMITS["candidates_per_branching"]


def test_spawn_candidates_requires_approaches(tmp_path):
    evolve = _git_evolve(tmp_path)
    with pytest.raises(ValueError):
        spawn_candidates(evolve, "F01", [])


def _min_dim_rows():
    """3 candidates: cand1 cascade_fail, cand2 min-dim 6, cand3 min-dim 7."""
    return [
        ["a", "eval", "F01", "6/6", "6.0", "fail", "incumbent"],
        ["b", "eval", "F01@cand1", "-", "0", "cascade_fail", "broken"],
        ["c", "eval", "F01@cand2", "6/9", "7.5", "fail", "half"],
        ["d", "eval", "F01@cand3", "7/8", "7.5", "fail", "solid"],
    ]


def _spawn_three(evolve):
    return spawn_candidates(evolve, "F01", ["a1", "a2", "a3"])


def test_select_candidate_pass_wins_outright(tmp_path):
    evolve = _git_evolve(tmp_path, rows=_min_dim_rows() + [
        ["e", "eval", "F01@cand2", "9/9", "9.0", "pass", "threshold met"],
    ])
    _spawn_three(evolve)
    result = select_candidate(evolve, "F01")
    assert result["outcome"] == "pass"
    assert result["feature_id"] == "F01@cand2"
    state = json.loads((Path(evolve) / "F01" / "branching.json").read_text())
    assert state["completed"] is True
    assert state["winner_outcome"] == "pass"


def test_select_candidate_adopts_best_min_dim(tmp_path):
    evolve = _git_evolve(tmp_path, rows=_min_dim_rows())
    _spawn_three(evolve)
    result = select_candidate(evolve, "F01")
    assert result["outcome"] == "adopt"           # cand3: min-dim 7 > 6
    assert result["feature_id"] == "F01@cand3"
    # adopt appends a reset row for the parent feature
    tsv = (Path(evolve) / "results.tsv").read_text()
    assert "reset" in tsv and "adopted candidate 3" in tsv


def test_select_candidate_none_when_no_improvement(tmp_path):
    rows = [
        ["a", "eval", "F01", "7/7", "7.0", "fail", "incumbent"],
        ["b", "eval", "F01@cand1", "5/6", "5.5", "fail", "worse"],
        ["c", "eval", "F01@cand2", "6/6", "6.0", "fail", "worse"],
        ["d", "eval", "F01@cand3", "-", "0", "cascade_fail", "broken"],
    ]
    evolve = _git_evolve(tmp_path, rows=rows)
    _spawn_three(evolve)
    result = select_candidate(evolve, "F01")
    assert result["outcome"] == "none"
    state = json.loads((Path(evolve) / "F01" / "branching.json").read_text())
    assert state["winner_outcome"] == "none"


def test_select_candidate_pass_resets_existing_incumbent(tmp_path):
    from worktree import create_feature_worktree, feature_branch
    evolve = _git_evolve(tmp_path, rows=[
        ["a", "eval", "F01", "6/6", "6.0", "fail", "incumbent"],
    ])
    # incumbent worktree+branch exist (normal pre-branching build activity)
    create_feature_worktree(evolve, "F01")
    cands = spawn_candidates(evolve, "F01", ["a1", "a2", "a3"])
    # give cand2 a distinct commit so the reset is observable
    cand2 = cands[1]
    p = Path(cand2["path"]) / "winner.txt"
    p.write_text("winning work\n")
    _run(["git", "add", "."], cand2["path"])
    _run(["git", "commit", "-m", "feat: winning approach"], cand2["path"])
    cand2_tip = _run(["git", "rev-parse", "HEAD"], cand2["path"]).stdout.strip()
    (Path(evolve) / "results.tsv").open("a").write(
        "e\teval\tF01@cand2\t9/9\t9.0\tpass\tthreshold met\n")

    result = select_candidate(evolve, "F01")
    assert result["outcome"] == "pass"
    # incumbent branch now points at the winner's tip
    branch = feature_branch(evolve, "F01")
    tip = _run(["git", "rev-parse", branch], tmp_path).stdout.strip()
    assert tip == cand2_tip
    # all candidate worktrees cleaned up
    for c in cands:
        assert not Path(c["path"]).exists()


def test_scan_resets_fail_count_on_reset_row(tmp_path):
    from prepare import scan_all_features
    rows = (_fail_rows("F01", 4) +
            [["r", "build", "F01", "-", "-", "reset", "adopted candidate 2"]])
    evolve = _evolve(tmp_path, rows=rows)
    info = next(f for f in scan_all_features(evolve) if f["name"] == "F01")
    assert info["consecutive_fails"] == 0


def _completed_branching(evolve, feature, outcome):
    d = Path(evolve) / feature
    d.mkdir(exist_ok=True)
    (d / "branching.json").write_text(json.dumps(
        {"round": 1, "completed": True, "winner_outcome": outcome,
         "candidates": [{"cand_id": 1, "feature_id": f"{feature}@cand1",
                         "branch": "x", "approach": "a"}]}))


def test_can_force_pass_requires_branching(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 8))
    ok, reason = can_force_pass(evolve, "F01")
    assert ok is False
    assert "branching" in reason


def test_can_force_pass_closed_while_in_flight(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 8))
    d = Path(evolve) / "F01"
    d.mkdir(exist_ok=True)
    (d / "branching.json").write_text(json.dumps(
        {"round": 1, "completed": False}))
    assert can_force_pass(evolve, "F01")[0] is False


def test_can_force_pass_closed_after_passing_winner(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 8))
    _completed_branching(evolve, "F01", "pass")
    assert can_force_pass(evolve, "F01")[0] is False


def test_can_force_pass_open_after_no_winner(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 8))
    _completed_branching(evolve, "F01", "none")
    ok, reason = can_force_pass(evolve, "F01")
    assert ok is True


def test_mark_forced_pass_gate_and_approval(tmp_path):
    evolve = _evolve(tmp_path, rows=_fail_rows("F01", 8))
    with pytest.raises(ValueError, match="gate"):
        mark_forced_pass(evolve, "F01", user_approved=True)
    _completed_branching(evolve, "F01", "none")
    with pytest.raises(ValueError, match="approval"):
        mark_forced_pass(evolve, "F01", user_approved=False)
    mark_forced_pass(evolve, "F01", user_approved=True)
    tsv = (Path(evolve) / "results.tsv").read_text()
    assert "forced" in tsv
