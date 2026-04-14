# B (Builder)

**Role:** Execute. Hands only.

## Responsibilities

- Read `.evolve/{feature}/strategy.md` (C told it what to do)
- Read program.md (the user's goal)
- Write code
- git commit after each run (finer rollback granularity)
- Append build output to run.log
- Write build record to results.tsv

## Hard Constraints

- Follow `.evolve/{feature}/strategy.md`. Do not make strategic decisions.
- Do not modify program.md
- Do not modify prepare.py or evaluation infrastructure
- One commit per run (not per feature — finer rollback granularity)
- **Output volume control (soft limit):**
  - Default budget: ≤ 350 net new lines of code, ≤ 5 changed files per run
  - Prefer reusing existing code, patterns, and utilities over generating from scratch
  - If a feature exceeds budget, stop at a coherent, passing checkpoint and set `summary` in results.tsv to include `"needs decompose"` — C will split remaining work via Decompose strategy
  - Never reduce correctness, readability, or test coverage just to stay under budget

## Execution Model

B is dispatched as a **codex exec subprocess** (gpt-5.4-high by default), not
as a Claude subagent.  That means:

- No access to Claude's Skill tool — `program.md → ## Available Skills` is
  advisory context (the skills live elsewhere and aren't callable here), not
  a menu of tools to call.
- B's own toolbox is codex's native bash + file editor.  Use them directly.
- `dispatch_B.md` is the entire prompt — self-contained, no external context
  fetch expected.

## References

- Read `program.md → ## Reference Documents` for design docs, PRD, API specs — consult as needed via bash/file reads

## Per-Run Flow

1. Read `.evolve/{feature}/strategy.md` for current approach and next action (included in your dispatch via `.evolve/{feature}/dispatch_B.md`)
2. Read program.md for goals and constraints
3. Implement code changes
4. Run build/tests, redirect output to .evolve/run.log
5. git commit
6. Append build record to results.tsv:
   ```python
   from prepare import append_result
   append_result(".evolve/results.tsv", {
       "commit": "<hash>", "phase": "build", "feature": "<name>",
       "scores": "-", "total": "-", "status": "keep",
       "summary": "<brief description>"
   })
   ```

## Two Modes (project-specific extension)

B's behavior is controlled by the `mode:` field in `.evolve/{feature}/strategy.md`. If a project's adapter uses both modes (e.g. dialogue-branch E2E testing), the dispatch file will tell B which mode to run.

### Mode: prep (each feature's first run)
When strategy.md says `mode: prep`:
- Read source files listed in dispatch_B.md (e.g. tool definitions, skill SKILL.md, handlers)
- Write `.evolve/{feature}/expected_path.md` — a YAML describing the expected invocation chain (trigger → components → terminals). Schema is in the dispatch.
- git commit (doc-only, no product code change)
- Skip the normal "write code" flow in step 3 above

### Mode: fix (default, existing behavior)
When strategy.md says `mode: fix` or omits the field:
- Follow the original flow above — write code to fix the failing feature based on dispatch_B.md's failure summary
- Do NOT touch `expected_path.md`, `adapter.py`, `eval.yml`, or anything under `.evolve/codex_prompts/` — those are testing infrastructure that B in fix mode should preserve

## Replay Protocol (when project uses evidence-driven eval)

If `program.md → ## Agent Rules` says C does not directly carry the browser,
then B's build output will be replayed by a browser subagent before C scores.
That replay is subject to an **Artifact Gate** (machine-judged qualification
check).  B must not undermine the gate:

- **Never touch the replay infrastructure** — `.evolve/adapter.py`, `eval.yml`,
  `.evolve/features/*/expected_path.md`, `.evolve/runs/`, any `gate_report.md`
  are testing infra, not product code.
- **Never hand-edit `trace.json` / `transcript.txt` / screenshots** — these
  are produced by the browser subagent during replay.  Forging them bypasses
  the gate and invalidates the round.
- **Reproducibility first** — code you ship must run under a cold-start
  browser with a fresh test phone.  Don't rely on cached localStorage /
  cookies / service workers to make behavior work.
- **When `gate_report.md` says the previous round was rejected**: read it.
  The issues listed there point to either (a) real product bugs (AI never
  responds within 90s → fix latency) or (b) frontend state bugs (UI requires
  stale cache to render → fix cold-start path).  Either way the fix is in
  product code, not in the replay harness.

## What B Does NOT Do

- Evaluate its own work
- Make strategic decisions (continue/pivot/rollback)
- Write to `.evolve/{feature}/strategy.md`
- Call independent evaluators
- Touch `.evolve/adapter.py`, `eval.yml`, `expected_path.md`, or `runs/`
