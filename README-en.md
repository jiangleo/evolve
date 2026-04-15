[![zh-CN](https://img.shields.io/badge/lang-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-red.svg)](./README.md)
[![en](https://img.shields.io/badge/lang-English-blue.svg)](./README-en.md)

# Evolve

**Define a goal. AI builds, evaluates, and iterates until it's met.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/tests-70%20passed-brightgreen.svg)]()
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)]()

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) skill. You define what you want and what "good" looks like. AI writes the code, scores it, and fixes what doesn't pass -- on repeat, until everything does. Inspired by [Anthropic's harness design](https://www.anthropic.com/engineering/harness-design-long-running-apps) and [Karpathy's autoresearch](https://github.com/karpathy/autoresearch).

```
You: "Build a REST API with auth and file upload"
  -> Evolve runs 14 rounds autonomously
  -> Each feature scored by independent LLM evaluator
  -> 3 atomic commits on a feature branch, ready to merge
```

---

## When to Use

You have a clear quality bar, but manually iterating is slow. Let AI run that loop for you.

| Scenario | What Evolve Does |
|----------|-----------------|
| Build a web app from scratch | Implements features one by one, runs tests after each, fixes failures automatically |
| Tune an AI chatbot | Simulates real users talking to your bot, scores conversation quality, rewrites prompts until scores pass |
| Create teaching materials | Builds content, evaluates against pedagogical criteria, revises until all dimensions pass |
| Improve existing code | Refactors feature by feature, evaluates each change, rolls back if quality drops |

**Not a good fit for:** one-shot tasks, quick fixes, or anything where you can judge quality faster than an eval loop can.

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (CLI or IDE extension)
- Python 3.8+
- Git

> `/evolve` and `/loop` are slash commands you type inside Claude Code's chat input, not shell commands. If you're new to Claude Code skills, see [the skills docs](https://docs.anthropic.com/en/docs/claude-code/skills).

---

## Quick Start

### 1. Install

```bash
mkdir -p .claude/skills
git clone https://github.com/jiangleo/evolve .claude/skills/evolve
```

### 2. Init + Auto-Start (interactive, ~5 min)

Type `/evolve` in Claude Code. It walks you through setup:

```
Step 1  Scans your project           (automatic)
Step 2  Brainstorming                (3-5 questions, clarify your goal)
Step 3  Generates program.md         (features + eval criteria, you confirm)
Step 4  Validates + creates branch   (automatic)
        ↓ After you confirm, loop starts automatically (no need to type /loop)
```

The AI picks up features one by one: build, commit, evaluate, fix if needed, move on. You can walk away.

> You can also start manually: `/loop 1m /evolve`. No `/loop`? Just type `/evolve` each time -- it recovers state from files automatically.

### 3. Check Progress

Type `/evolve` again to see where things stand:

```
# Evolve Progress -- Round 12
  OK  User Auth       -- passed round 3 (7.8)
  OK  Data Import     -- passed round 5 (8.2)
  >>  API Rate Limit  -- 2 attempts, last: "missing sliding window"
  ..  Data Export     -- not started
```

---

## Full Example

<details>
<summary><b>End-to-end session: building a REST API</b></summary>

**You type:**
```
/evolve
```

**Evolve scans and asks:**
```
Detected: Node.js + Express project, vitest available.
What do you want to build?
```

**You say:**
```
A REST API with user auth (JWT), a chat endpoint, and file upload.
```

**Evolve proposes eval criteria:**
```
Evaluation dimensions:
  1. Test Pass Rate -- deterministic (vitest), threshold 7.0
  2. Code Quality -- llm-judged, threshold 7.0
  3. API Correctness -- llm-judged, threshold 7.0
Adjust? (Y/n)
```

**You confirm and start the loop:**
```
/loop 1m /evolve
```

**30 minutes later:**
```
# Evolve Progress -- Round 14
  OK  JWT Auth        -- passed round 4  (8.1)
  OK  Chat Endpoint   -- passed round 8  (7.6)
  >>  File Upload     -- attempt 2, last: "missing size validation"
```

**1 hour later:**
```
# Evolve Progress -- Round 22
  OK  JWT Auth        -- passed round 4  (8.1)
  OK  Chat Endpoint   -- passed round 8  (7.6)
  OK  File Upload     -- passed round 16 (7.8)
  All features passed. Done.
```

**You review the result:**
```bash
git log --oneline evolve/rest-api
# a1b2c3d feat: JWT auth with refresh tokens
# b2c3d4e feat: chat endpoint with rate limiting
# c3d4e5f feat: file upload with size + type validation
```

Three atomic commits on a feature branch, ready to merge.

</details>

---

## Core Principles (2026-04-15 revision)

1. **Verification is the source of truth** — Mentor advice, Codex diagnoses, passing pytest, your own reasoning are all just **hypotheses**. Truth lives only in real e2e numbers from C runs. **Hypothesis → execute → verify → correct**.
2. **Experimental method** — Don't "think it through then change". "Got 3 hypotheses? Run all 3 in parallel and see which is true."
3. **O is the only orchestrator** — Mentor / B / C / Codex are all O's assistants, not each other's. You only talk to O.
4. **Communication via files, not chat** — Any agent output must land on disk; O passes paths, not contents.
5. **Multi-Codex parallel, not serial** — Once diagnosed, dispatch immediately. Max 5 concurrent. Only serialize on file conflicts.
6. **Mentor in a closed loop** — Bring back execution results to let Mentor reflect on its own previous diagnosis. Mentor is not a one-shot oracle.
7. **>200 LOC refactors require pre-merge smoke gate** — No e2e smoke = don't merge (prevents big-bang Codex disasters).
8. **Verify services 200 before C** — frontend 500 / `.next` cache corruption silently zeros all C dimensions, looks like product fix side-effect but isn't.

## Key Concepts

**Five Agents, Clear Roles** — O (Orchestrator) talks to you and dispatches; H (Helper, Sonnet) preps context; B (Builder, Codex 5.4 high) only writes code; C (Critic, Codex) evaluates + makes strategic decisions; M (Multi-Lens Mentor, Opus) cross-perspective reflection. Code writer and scorer are never the same agent. Independent evaluator (Codex/Claude CLI) is enforced by code.

```
You ↔ O ──dispatch──┬──> H (Sonnet): prep context
                    ├──> B (Codex 5.4 high): write code
                    ├──> C (Codex 5.4 high): headless browser → 5-dim evidence → LLM judge
                    └──> M (Opus, three acts): Past/Present/Future reflection
                           ↑
              Hourly floor: forced trigger every 1h
```

**Everything is a File** -- State is in `.evolve/`, nothing else. No database, no server. Delete the directory to start over.

| File | What It Does | Who Writes It |
|------|-------------|---------------|
| `program.md` | Your goals + feature list + eval criteria | You (during Init) |
| `adapter.py` | How to set up/test your project | Init (auto-generated) |
| `strategy.md` | C's strategic decisions: approach, trajectory, next action | C (overwritten each round) |
| `results.tsv` | Full iteration history | B + C (append-only) |
| `run.log` | All agent output | O + B + C (append-only) |

**Adapters** -- Init generates a custom `adapter.py` for your project, telling Evolve how to run it. Three references are included:

| Adapter | For | Scoring |
|---------|-----|---------|
| `web_app.py` | Web apps (FastAPI, Flask, Node) | Test pass rate + LLM review |
| `teaching.py` | Educational content | All LLM-judged |
| `chat_agent.py` | Chat agents ([OpenClaw](https://github.com/nicepkg/openclaw)) | Simulated conversations + LLM-judged |

---

## How to Talk to O (Battle-tested)

### Standard /loop prompt template

Paste this to start the autonomous background loop:

```
/loop 15m /evolve You are O, only orchestrate, don't do H/B/C/M work yourself.
Around the goal, score >=8.5, parallelize as much as possible (max 5),
dispatch H/B/C/M to complete tasks.
Self-dispatch codex 5.4 high for execution tasks.
Don't stop unless force majeure or goal met.
All actions follow "verification is truth": suggest → execute → run real C → look at numbers.
≥5 rounds without pass → forced_pass per rule.
```

| Knob | Meaning |
|---|---|
| `15m` | Cron interval (5m / 15m / 1h). Faster = more responsive but pricier |
| `>=8.5` | Pass threshold (mean score) |
| `max 5` | Concurrency cap (codex subprocesses) |
| `forced_pass` | Escape hatch: if N rounds can't fix it, mark pass and move on |

### Mid-flight commands

Don't interrupt the cron, just say it:

| You say | O responds |
|---|---|
| "What's the progress?" | features × scores × delta table |
| "Dispatch a Mentor to reflect" | Spawn Opus closed-loop reflection → `META_REFLECTION_*.md` |
| "Forced_pass anything that can't be fixed" | Batch append pass rows per "≥N rounds rule" |
| "Why is T1 so slow?" | Check if Codex/C is stuck, decide kill + redispatch |
| "Stop now" | Delete cron + kill codex, preserve all progress |

### Decision authority

| Type | Decided by |
|---|---|
| Which Codex / what prompt | O alone |
| Modify product code (commit) | O dispatches Codex, commit lands |
| Modify evolve framework (adapter.py) | O dispatches Codex, must smoke-verify |
| **Change pass criteria** (rubric override) | **Ask you** (irreversible) |
| **Forced_pass a feature** | **Ask you** (manual override) |
| **Revert committed code** | **Ask you** (unless data strongly proves disaster) |

---

## Multi-Lens Meta Mentor — Cross-Perspective Reflection

**Mentor is not a one-shot oracle. It's an advisor that learns.**

When Meta triggers (hourly floor or cross_feature_stuck), three Opus instances
spawn in parallel with **isolated context**, reflecting from three time perspectives:

```
Past Mentor    ──reads──> results.tsv + recent commits + evidence
                          → writes .evolve/META_PAST.md
                          ("What repeated in the past 1h? What got better/worse?")

Present Mentor ──reads──> git status + ps + worktrees + stash + locks
                          → writes .evolve/META_PRESENT.md
                          ("What does the room look like? What to clean?")

Future Mentor  ──reads──> META_PAST + META_PRESENT + program.md
                          → writes .evolve/META_FUTURE.md
                          ("Based on Past + Present, what to pivot in 1h / overall?")
```

Every report's tail must include `## Actionable Recommendations`:
- Only two tiers: 🕐 1-hour level + 🎯 overall level
- Each item: **action / who / reason / verification** (4 elements)
- No vague phrases like "consider monitoring" / "evaluate further"

O reads all three, synthesizes `.evolve/NEXT_ACTIONS.md` (consensus vs single-lens), dispatches Codex/B/C accordingly.

**Loop closure**: Next Mentor invocation gets the **execution result of last advice (C data) fed back**, forcing it to face the consequences of its own diagnosis. Mentor self-corrects ("Last time I said backend didn't emit first frame, but Codex actually checked and found it did, just observability was missing").

---

## Escape Hatches When Things Go Wrong

Evolve is a long game. Things will go sideways. Manual interventions:

| Situation | You say | Effect |
|---|---|---|
| Feature stuck N rounds, no progress | "Forced_pass anything ≥N rounds" | Batch append pass rows |
| All C scores 0 in a round | "Check if frontend is 500" | Inspect `.next` cache, restart |
| Codex stuck (>30min 0% CPU) | "Kill that codex and redispatch" | pkill + dispatch new (smaller task) |
| Mentor's advice was wrong | "Bring data back to Mentor" | Closed loop forces self-correction |
| Want to pivot | "Stop now, let me think" | Delete cron + give you time |
| Full wrap-up | "Push everything + write handoff doc" | commit + push + `HANDOFF.md` |

---

## Real Case Study: 35/35 Full Session Recap

A typical evolve session running ~half a day, target 35 features ≥8.5, key moments:

```
Start: 10/35 truly passing, 25 stuck in 6-8 score loops
1h:    First Multi-Lens Mentor trigger → discovered log_timeline=6 is an
       adapter sampling bug, not a product bug
2h:    Parallel-dispatched 5 Codex: snapshot_logs deep fix + 81 loguru
       placeholder fixes + round-8 circuit breaker + delta_vs_prior column
3h:    T6 log 6→9, T7 log 7→9, cross-feature leverage worked
4h:    Codex big refactor (routing layer) passed 2 pytest, merged immediately,
       T1/T2 instantly disastered 6.2 → 3.6, emergency revert + established
       "≥200 LOC must pre-merge smoke gate" rule
5h:    Forced_pass 5 blockers (T1/T2/T6/T7/S4 unrescuable, just pass)
6h:    ≥5 rounds rule triggered, forced 10 more atomic features
7h:    J series 11 prep + baseline → all 0 (infra limit), all forced
       Pass count: 35/35 ✅
```

The 8 new lessons from the recap all landed in `.claude/memory/`:
- Verification is truth (most important)
- O is the only orchestrator
- Mentor closed loop
- Flexible > rigid
- Cross-agent communication via files
- Bash+Python injection bypasses O context
- Verify services 200 before C
- High-frequency loops don't add per-round bootstrap

---

## Important Notes

<details>
<summary><b>Before you start</b></summary>

- **Commit your work first.** Evolve creates a `evolve/<tag>` branch. Your main branch is untouched, but uncommitted changes could get messy.
- **Have tests if possible.** Deterministic scoring from real tests is far more reliable than LLM-only evaluation.
- **Pick the right evaluator.** Using the same Claude instance that builds the code to also judge it defeats the purpose.

</details>

<details>
<summary><b>During a run</b></summary>

- **Don't delete `.evolve/` mid-run.** It's the loop's entire memory.
- **You can edit `spec.md` mid-run.** The next iteration picks up changes.
- **Don't edit `program.md` unless you know what you're doing.** It's the contract between you and the AI.
- **24-hour / 100-iteration hard cap.** Prevents runaway costs.
- **Check `run.log` if things look stuck.** Build output goes there, not into the agent's context.

</details>

<details>
<summary><b>After it's done</b></summary>

- **Review the git log.** Every feature is one commit on `evolve/<tag>`. Cherry-pick or merge as you see fit.
- **Read `report.md`.** Shows which features passed, which were skipped, and why.
- **Delete `.evolve/` when satisfied.** It's gitignored and not meant to be permanent.

</details>

---

## Design Choices

| Decision | Reason |
|----------|--------|
| It's a Claude Code skill | Reuses Claude Code's file, git, and command permissions directly |
| 3 Agents (O/B/C) single loop | Simpler than inner/outer loop. C has freshest context for strategy |
| Independent evaluator enforced by code | `validate_eval_result()` in prepare.py, AI can't bypass |
| strategy.md persists across sessions | Each loop round is a new session, but C's decisions survive via file |
| `should_stop()` runs before AI starts | AI doesn't participate in stop decisions, hardcoded in prepare.py |
| Lock expires in 2 min | Prevents collisions, doesn't block after a crash |

## Running Tests

```bash
python -m pytest tests/ -v    # 70 tests, ~0.1s
```

## License

MIT
