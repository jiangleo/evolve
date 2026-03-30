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

## Key Concepts

**Three Agents, Clear Roles** -- O (Orchestrator) talks to you and dispatches; B (Builder) only writes code; C (Critic) evaluates + makes strategic decisions. The code writer and the scorer are never the same agent. Independent evaluator (Codex/Claude CLI) is enforced by code.

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
