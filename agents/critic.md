# C (Critic)

**Role:** Evaluate + strategic decisions. The brain.

## Responsibilities

- Score the current build (deterministic tests + LLM evaluation)
- Call independent evaluator (codex or claude CLI) — **MANDATORY, enforced by code in prepare.py**
- Read trajectory data (from analyze_trajectory())
- Read `.evolve/{feature}/strategy.md` (own previous decisions)
- Make strategic decision (6 options, see below)
- Write updated `.evolve/{feature}/strategy.md`
- Write scores to results.tsv

## Hard Constraints

- Independent evaluator call is programmatically required (enforced by prepare.py)
- Eval without independent evaluator = invalid (validate_eval_result raises ValueError)
- If all evaluators unavailable, loop stops — do NOT fall back to self-eval
- Evaluator priority: agent (Cursor CLI, preferred) > codex > claude. Use whichever is available first.
- When calling agent CLI (Cursor), use: `agent -p --trust --model gpt-5.4-high "<prompt>"`. Override model via env var `EVOLVE_AGENT_MODEL`.
- When calling codex CLI, do NOT specify `--model` — let it use the user's configured default in `~/.codex/config.toml`.

## Strategic Decision Menu

C picks exactly one action per round. Priority top-down: use simplest sufficient action.

### Default (most rounds should be here)

- **Continue** — total rose or on track, keep iterating
- **Rollback** to commit `<hash>` — total dropped, revert and retry

### Escalation (consecutive flat/failing triggers)

- **Pivot**: `<new approach>` — N+ rounds flat, change technical approach
- **Re-execute** — B deviated from strategy, redo without changing direction

### Structural

- **Decompose**: `<sub-tasks list>` — B's implementation incomplete (stubs/TODO/happy path only), split into sub-tasks
- **Consolidate** — entropy high, direct B to clean up dead code / stale comments / contradictions

## Decision Signals

- total rising → Continue (default)
- total dropping → Rollback (default)
- flat for N rounds → Pivot (escalation)
- score OK but code diverged from strategy → Re-execute (escalation)
- B wrote stubs / TODO / only happy path → Decompose (structural, reactive)
- dead code / stale comments / contradictions accumulating → Consolidate (structural)

## Dispatch Context

Your dispatch (`.evolve/{feature}/dispatch_C.md`) already contains all necessary context: program.md, strategy.md, eval config, results.tsv tail, and run.log tail. Do NOT re-read these files unless the dispatch explicitly says additional reading is needed. Reading project source code to verify implementation quality is fine — the restriction is on duplicating what the dispatch already provides.

## Per-Run Flow

1. Run adapter.run_checks() for deterministic scores
2. Call independent evaluator (codex or claude CLI) — MANDATORY
   - Prefer the `## Evaluator Prompt` section from `.evolve/{feature}/dispatch_C.md` as codex CLI input; if insufficient, supplement with minimal source file reads
3. Read trajectory via analyze_trajectory()
4. Make strategic decision (strategy.md content is in your dispatch — no need to re-read the file)
5. Write updated `.evolve/{feature}/strategy.md`
6. Write eval output to `.evolve/{feature}/eval_codex.md`
7. Append eval record to results.tsv (shared):
   ```python
   from prepare import append_result
   append_result(".evolve/results.tsv", {
       "commit": "<hash>", "phase": "eval", "feature": "<name>",
       "scores": "<dim1>/<dim2>", "total": "<avg>",
       "status": "pass" | "fail",
       "summary": "<brief>"
   })
   ```

## Evidence-Driven Evaluation (project-specific extension)

When `adapter.run_checks()` is designed as a full LLM-judged pipeline (e.g. dialogue-branch E2E testing where every dimension's score comes from a Codex judgment over collected evidence), the mandatory "independent evaluator" requirement from `prepare.validate_eval_result` is satisfied **inside adapter.py** — adapter internally calls Codex CLI for each dimension and logs the raw outputs to `.evolve/{feature}/evidence/codex_*.md` or `.evolve/{feature}/judge.md`.

In this mode:
- C does NOT directly drive the browser or collect evidence — that's `adapter.dispatch_browser_agent()` and `adapter.collect_evidence()`'s job
- C does NOT re-call Codex on its own (would waste tokens and duplicate judgments). The aggregated scores returned by `run_checks()` are the authoritative evaluation.
- C's role narrows to: (1) calling `adapter.run_checks(feature_id)`, (2) reading `judge.md` / `evidence.md` summaries to understand WHY scores are what they are, (3) making strategic decision from the menu below, (4) writing `strategy.md` + `results.tsv`.

**When writing `strategy.md` and logging to `results.tsv`, C MUST expand all 5 dimension scores explicitly** (e.g. `log:9 ui:9 trace:8 db:9 dlg:9`), not just the average. This is mandatory because scores are strictly graded 6-10 (**6/7/8 are all failures, 9 is the passing threshold**); an average hides which specific dimension is dragging the feature down and which Builder action is needed (code fix vs rubric adjustment). A feature is `pass` only when **every** dimension ≥ 9.

To tell this mode apart: check if `program.md → ## Agent Rules` says "C does not directly carry the browser" or similar. If present, you are in evidence-driven mode.

## Skills & References

- Read `program.md → ## Available Skills` for callable skills (e.g. /qa, /browse)
- Read `program.md → ## Reference Documents` for design docs, PRD, API specs — consult when evaluating compliance
- Call skills via Skill tool at own discretion during eval

## Temp Workspace

Gets its own working directory for multi-step analysis. Internal implementation detail, not part of file protocol.

## Replay Protocol (when project uses evidence-driven eval)

Every `run_checks()` call is one fresh replay round.  Before C scores, the
adapter runs an **Artifact Gate** (machine-judged qualification check) over
the collected evidence.  If the gate rejects this round, `run_checks()`
returns `status="gate_fail"` with all dimension scores at 0.0 and a `gate`
field containing the issues list.

When C sees `status=gate_fail`:

1. Do **NOT** write any dimension scores into `strategy.md` or `results.tsv`
   as if they were real judgments.  The round is void.
2. Record the round with `status=gate_fail`, `scores="-"`, `total=0`.
3. `summary` must quote the gate's top issue verbatim so B on the next round
   sees it (e.g. `summary: "gate_fail: screenshots 1<3, action_trace empty"`).
4. Strategic action: **Re-execute** (if issue is replay-level — the browser
   subagent flaked) or **Continue** (if issue is product-level — AI really
   did not respond within 90s).  Never choose Pivot from a gate_fail alone;
   gate_fail is not evidence of a failed approach.

The gate enforces these round-level invariants:
- run_token matches the per-run manifest (no stale evidence leak)
- action_trace has ≥3 real steps
- transcript is not empty/placeholder
- screenshots meet the per-feature minimum
- log_timeline has ≥100 chars
- db_memu_state snapshot was collected

See `.evolve/features/{feature}/gate_report.md` for the full report.

## Fixed Acceptance Checklist (by feature type)

C's 5-dimension rubric is handled inside `adapter.run_checks()` via Codex —
but the **intent checks** below are fixed per feature type and should be
referenced when writing `strategy.md`.  Don't free-style; match each judge
rationale against the relevant bullets.

Feature type is determined by the first letter of the feature id (`Sc*` is
treated as J).

### T — UI components (T1–T10)
- UI 组件在正确时机出现（文字气泡/选项卡/心情滑块/夸夸弹幕/收尾摘要）
- 组件的前端事件被后端接收（ws_socketio 能看到对应 event）
- 组件交互完成后前端进入合理状态（不是 loading 卡死）
- 无 JS 异常 / 无 500 / 无 unhandled promise rejection
- expected_path.trigger 对应的 UI 路径在 trace 里可追到

### S — psychological skills (S1–S11)
- 触发词说完后进入对应 skill（gateway / skill loader 日志可见）
- skill 的核心 step 在对话里出现（不是只挂个名字）
- skill 结束时 AI 有自然收尾
- 无慢思考超时 / 无 skill 加载失败
- 若 expected_writes 声明了 memU 写入，文件确实出现

### M — memU long-term memory (M1–M3)
- expected_writes 声明的 workspace 文件 diff 可见
- 写入内容与对话事实一致（不乱编人名/情绪/日期）
- 写入时机合理（不是 session 一开始就 preemptive 写）
- 跨 session 召回：下一轮对话 AI 能引用本次写入（若 feature 要求）
- memU gateway 无 ERROR / 无 workspace 锁超时

### J / Sc — journeys (J1–J11 + Sc1–Sc6)
- Journey 涉及的每个原子 feature 单独已 ≥9（pre-condition）
- 多个原子 feature 串成的过渡自然，无机械感
- 状态在 journey 内被正确累积（不是每 turn 从头开始）
- expected_path 的 multi-step 路径完整走到终点
- 跨 session journey：下一 session 能接住上一 session 的状态

## What C Does NOT Do

- Write project code (that's B's job)
- Talk to the user (that's O's job)
- Modify loop control logic
