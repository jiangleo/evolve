# Design: Deterministic Cascade, Population Branching, Worktree Isolation

Date: 2026-07-10
Status: approved
中文版: [2026-07-10-cascade-population-worktree-design.zh.md](./2026-07-10-cascade-population-worktree-design.zh.md)

## Motivation

Three gaps versus current industry-best harness practice (Anthropic harness
design, AlphaEvolve-style population search, worktree-isolated parallel
builders):

1. **LLM-judge over-reliance.** Every eval round pays for a 5-dimension LLM
   judge even when the build is trivially broken (service 500, corrupted
   `.next` cache), producing all-zero rounds that masquerade as product
   regressions. Absolute 1-10 scores also drift round-to-round, so
   `analyze_trajectory()` reacts to judge noise, not real trends.
2. **Single-lineage grinding.** A stuck feature is retried on one trajectory
   until `forced_pass` waives it. In a real 35/35 session, ~16 of 35 passes
   were forced — "goal met" silently became "goal waived".
3. **Serialized builders.** `build_lock` allows only one B at a time,
   throttling multi-feature runs; parallel work would collide in one working
   tree.

## Guiding principle

Guarantees live in Python that runs before/around the AI (the existing
`validate_eval_result` / `should_stop` pattern), never only in agent
markdown. New code goes in new modules — `cascade.py`, `worktree.py`,
`population.py` in the skill root — with `prepare.py` re-exporting the
public functions so the documented `from prepare import ...` interface stays
stable. prepare.py (1,340 lines) must not grow materially.

## Part 1 — Deterministic cascade + pairwise trajectory

### Cascade (`cascade.py`)

- `eval.yml` gains an optional top-level `cascade:` section:

  ```yaml
  cascade:
    - name: build
      cmd: npm run build
      timeout: 300
    - name: lint
      cmd: npx eslint src/
      timeout: 120
    - name: test
      cmd: npx vitest run
      timeout: 600
  ```

- `load_eval_config()` parses it; absent section → empty cascade (behavior
  unchanged for old projects).
- New `run_cascade(evolve_dir, feature, stages) -> dict` executes stages in
  order, **fail-fast**: first failing stage aborts the round. Output tail of
  the failing stage goes to `.evolve/{feature}/cascade_fail.md` and to the
  round summary.
- **Stage 0 is implicit and always present:** a service-alive check derived
  from `adapter.setup()` (adapter may declare `health_check()` returning
  ok/fail; default is "setup() did not crash"). This codifies the
  "verify service 200 before dispatching C" rule in code.
- New results.tsv status `cascade_fail` (added to `VALID_STATUSES`):
  scores `-`, total `0`, summary quotes the failing stage. A `cascade_fail`
  round is void for trajectory purposes (like the chat adapter's
  `gate_fail`, which this generalizes).
- **Enforcement:** `validate_eval_result()` additionally requires the eval
  result to carry `cascade: passed` (or an explicit empty-cascade marker)
  before any LLM-judged scores are accepted. C cannot skip the cascade.

### Pairwise trajectory

- `prepare_dispatch()` includes a `## Previous Round Evidence` section in
  `dispatch_C.md` (path to previous `eval_*.md` / evidence dir) when a
  previous eval exists.
- The judge must emit, per dimension, `pairwise: better|same|worse` in a
  parseable block. C records it.
- `results.tsv` gains an **optional 8th column** `pairwise` (e.g.
  `log:better/ui:same/db:worse`). Readers (`read_progress`,
  `analyze_trajectory`, `generate_report`) accept both 7- and 8-column rows;
  `append_result` writes 8 columns for eval rows, `-` otherwise.
- Pass/fail is **unchanged**: absolute scores vs per-dimension threshold.
- `analyze_trajectory()` prefers pairwise verdicts over raw score deltas
  when present. Contradiction rule: if the score delta and the pairwise
  majority disagree in sign (score up but majority `worse`, or vice versa),
  trend = `noisy` and the round contributes no trajectory signal — judge
  drift cannot trigger a false Pivot/Rollback.

## Part 2 — Worktree isolation (`worktree.py`)

- Each feature's B works in `.evolve/worktrees/{feature}` on branch
  `evolve/<tag>--{feature}` (sibling ref: git cannot nest a branch
  under an existing branch name), created by
  `create_feature_worktree(evolve_dir, feature) -> path`.
- **build_lock semantics change:** no longer "one B at a time". It now
  serializes only the true critical section — merging into `evolve/<tag>`.
  Multiple B's run in parallel across features (concurrency cap stays 5,
  set in loop.md guidance).
- C evaluates **inside the worktree** during iteration (adapter functions
  already take `project_dir`).
- **Merge on pass + integration gate:** `merge_feature(evolve_dir, feature)
  -> dict`:
  1. take build_lock, merge feature branch into `evolve/<tag>`;
  2. re-run the deterministic cascade on the merged tree;
  3. gate passes → keep merge, remove worktree + branch, feature
     `completed`;
  4. gate fails (conflict or cascade regression) → revert the merge, write
     `.evolve/{feature}/merge_conflict.md` (what broke, conflicting files),
     feature returns to `needs_build`.
- **Resource conflicts:** `adapters/base.py` gains optional
  `allocate_slot(n) -> dict` (env overrides for parallel instance n);
  `web_app.py` demonstrates per-slot PORT offsets. Adapters without it are
  assumed conflict-free (docs/teaching adapters).
- **Leak cleanup:** `acquire_lock()` best-effort prunes stale worktrees:
  a completed feature's worktree+branch is removed ONLY when the branch is
  already merged into the base (`git merge-base --is-ancestor`) — an
  unmerged pass must survive a crash so the orchestrator can still merge
  it. Per-entry failures are isolated (one bad worktree never aborts the
  rest).

## Part 3 — Population branching (`population.py`) + gated forced_pass

### Escalation ladder (replaces flat "≥5 rounds → forced_pass")

```
consecutive_fails ≥ 3   → Mentor advice (unchanged, existing behavior)
consecutive_fails ≥ 6   → BRANCH: spawn N=3 candidates
all candidates fail     → forced_pass becomes AVAILABLE (still requires
                          explicit user approval; O asks)
```

Interaction with the existing "mentor advice #3 → BLOCKER" rule
(critic.md): branching is inserted **before** BLOCKER. The advice-#3 check
no longer marks BLOCKER directly; instead it makes the feature
branching-eligible. BLOCKER is reached only when branching has failed AND
the user declines forced_pass — it remains the terminal skip state.

### Mechanics

- `spawn_candidates(evolve_dir, feature, n=3) -> list[dict]` forks N
  worktrees `.evolve/worktrees/{feature}-cand{i}` on branches
  `evolve/<tag>--{feature}-cand{i}` from the feature's current branch. Each
  candidate's `strategy.md` is seeded with a **distinct approach**, drawn
  from Mentor hypotheses and C's untried Pivot options; O writes the seeds.
- O dispatches N parallel B→C chains, one per candidate worktree (reuses
  Part 2 machinery; candidates count against the 5-concurrency cap).
- Candidate rounds are recorded in results.tsv with feature id
  `F01@cand2` so history is auditable but `read_progress` groups them under
  the parent feature.
- `select_candidate(evolve_dir, feature) -> dict` picks the winner:
  1. must pass the deterministic cascade;
  2. highest **minimum-dimension** score;
  3. tie-break: highest total, then pairwise vs the incumbent lineage.
  Winner merges through the normal integration gate; loser worktrees and
  branches are deleted.
- Budget: `HARD_LIMITS["max_branching_rounds_per_feature"] = 1` and
  `HARD_LIMITS["candidates_per_branching"] = 3` (config-overridable via
  program.md).
- `scan_all_features()` gains state `branching` (candidates in flight).

### forced_pass gating

- `can_force_pass(evolve_dir, feature) -> (bool, reason)`: True only when a
  branching round completed with no winner.
- `mark_forced_pass(evolve_dir, feature, user_approved: bool)` is the only
  sanctioned path; it checks the gate and appends a row with new status
  `forced` (added to `VALID_STATUSES`).
- `read_progress()` / `generate_report()` count `forced` separately:
  reports show `passed: M true + K forced / T` — waived features are never
  presented as passes.

## Documentation updates

- `loop.md`: dispatch flow (parallel B, worktree lifecycle, branching
  phase, merge-on-pass), concurrency rules, file permission matrix rows for
  new files.
- `agents/critic.md`: cascade-first per-run flow, pairwise output format,
  cascade_fail handling (mirrors existing gate_fail protocol).
- `agents/orchestrator.md`: escalation ladder, candidate seeding duty,
  forced_pass gate ("ask user only when can_force_pass is True").
- `agents/builder.md`: work happens in the feature worktree; never touch
  `evolve/<tag>` directly.
- `README.md` / `README-en.md`: principles, progress display with
  true/forced split, updated test badge.

## Testing

Unit tests with throwaway git repos in tmp dirs (consistent with the
existing fast suite):

- cascade: stage order, fail-fast short-circuit, implicit health stage,
  `validate_eval_result` rejection without cascade marker;
- pairwise: 7/8-column TSV round-trip, trajectory preference for pairwise,
  contradiction → `noisy`;
- worktree: create/remove, merge-on-pass happy path, integration-gate
  revert on conflict, stale-worktree pruning;
- population: candidate spawn/seed, `F01@cand*` grouping in read_progress,
  select_candidate ordering rules, budget enforcement;
- forced_pass: gate closed before branching, open after all-fail, `forced`
  counted separately in reports.

## Out of scope

- Multi-branching rounds per feature (budget stays 1 by default).
- Cross-feature candidate sharing or MAP-Elites-style archives.
- Changing the evaluator CLI priority or judge model selection.
- Migrating existing `.evolve/` state dirs (new runs only; old results.tsv
  files remain readable via 7-column compatibility).
