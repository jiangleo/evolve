---
name: evolve
description: Define a goal. AI builds, evaluates, and iterates until it's met. Use with /loop 1m /evolve for continuous autonomous operation.
triggers:
  - /evolve
---

# Evolve V2: Define Goal → Auto Build → Auto Evaluate → Iterate Until Done

## Overview

Five agents, one loop: O (Orchestrator) → H (Helper, Sonnet) → B (Builder, Codex) → C (Critic, Codex) → M (Multi-Lens Mentor, Opus) → ... → Done

- **Init**: This file. O guides user through 4 steps.
- **Loop**: `loop.md`. H/B/C/M run automatically via `/loop 1m /evolve`.

Hard dependencies: Python 3.8+, Git. Everything else is declared by the project adapter.

Agent definitions:
- `agents/orchestrator.md` — O 调度（含**核心原则**：凡以校验为准 / 实验法 / O 唯一调度 etc）
- `agents/helper.md` — H prep context
- `agents/builder.md` — B 写代码（Codex 5.4 high）
- `agents/critic.md` — C 评估（Codex + 5 dim LLM judge）
- `agents/mentor.md` — M 三幕反思（Past/Present/Future Opus）+ 闭环

---

## Trigger Routing

```
/evolve triggered
    │
Check if .evolve/ exists?
    ├── not exists → First-time setup (Step 1)
    └── exists → Check state:
         ├── no adapter.py → Step 3
         ├── no program.md → Step 3 (generate program.md)
         ├── no results.tsv or empty → Step 4 (validation)
         └── results.tsv has data → Enter loop (loop.md)
```

---

## Init Flow (4 steps)

### Step 1: Project Scan (automatic)

Scan language, framework, test framework, directory structure. Output brief summary:

> "Detected Node.js + Express project, has vitest, entry at app/main.js"

### Step 2: Brainstorming (interactive, core step)

O talks to user following /brainstorming principles:
- One question at a time
- Multiple choice preferred
- Goal: help user clarify three things — what they want, what "good" means, what's off limits

```
Q1: "What do you want to build? One sentence."

Q2: "Core features? I suggest based on your project:
     A. JWT authentication
     B. Chat endpoint
     C. File upload
     D. Other: ___"

Q3: "What matters for quality?
     A. Tests pass (deterministic)
     B. Code quality (AI review)
     C. API design (AI review)
     D. Other: ___"

Q4: "Score threshold per dimension? Default 3.5/5."

Q5: "Constraints?
     A. No new dependencies
     B. Don't touch xxx directory
     C. No limits
     D. Other: ___"
```

3-5 questions, ~2 minutes.

#### Skill Discovery

Claude Code built-in skills are always available, directly written into program.md. User's custom skills need to be discovered and confirmed.

1. **Pre-fill**: Write Claude Code built-in skills into program.md (always available, no config needed)
2. **Scan**: List user's custom installed skills in current environment
3. **Recommend**: Based on project type, suggest which custom skills agents might need
4. **Confirm**: Ask user which to include

```
"Claude Code 自带的 skills 我已经写进 program.md 了（/brainstorming 等）。

  你还安装了这些自定义 skills：
  ✅ /qa, /browse, /tdd, /simplify

  根据项目类型，建议纳入：
  A. /qa → C 评估时用，系统化测试
  B. /browse → C 验收时用，检查页面效果
  C. 全部纳入
  D. 不需要额外 skills

  选好后写进 program.md，B 和 C 在循环中按需调用。"
```

#### Reference Documents

Ask user if there are existing documents that agents should consult during the loop:

```
"有没有相关文档需要 AI 在过程中参考？比如：
  - 设计文档 / PRD / 产品原型
  - API 规范 / 接口文档
  - 技术方案 / 架构设计
  - 竞品分析 / 用户研究
  - 其他参考资料

  提供文件路径或 URL，我写进 program.md，B 和 C 需要时会自己去查。
  没有的话直接跳过。"
```

Record paths/URLs in program.md `## Reference Documents`. Agents read these on-demand (not preloaded — just know where to look).

### Step 3: Generate program.md (automatic + user review)

Auto-generate from Step 1 scan + Step 2 conversation:

```markdown
# Program

## Product Requirements
<from Step 2>

## Feature List
- [ ] Feature A
- [ ] Feature B
- [ ] Feature C

## Evaluation Criteria
dimensions:
  - name: <dimension name>
    type: deterministic | llm-judged
    cmd: <command>  # optional, for deterministic
    threshold: <float>
    description: >  # optional, multi-line
      What this dimension measures and how to evaluate it.
    scoring_rubric:  # optional but recommended, anchor points for consistent LLM scoring
      1: "definition of 1"
      2: "definition of 2"
      3: "definition of 3"
      4: "definition of 4 (threshold)"
      5: "definition of 5"
    checks:  # optional, for deterministic: explicit check items
      - check item 1
      - check item 2

## Technical Constraints
- Stack: <from Step 1 scan>
- Dependency limits: <from user>
- No-go zones: <from user>

## Reference Documents
<!-- Paths or URLs that agents can consult during the loop. Read on-demand, not preloaded. -->
- docs/design/api-spec.md — API 接口规范
- docs/prd.md — 产品需求文档
- https://example.com/design-system — 设计系统参考

## Available Skills

### Built-in (Claude Code default, always available)
- /brainstorming → O uses during init for structured Q&A
- /loop → schedule recurring prompt

### Project Skills (user confirmed during init)
<!-- O scans user's custom skills + user confirms. Agents call these as needed. -->
- /qa → systematic testing, C calls during eval
- /browse → live page inspection, C calls to verify UI

## Agent Rules
- Do not modify program.md
- Do not modify files under .claude/skills/evolve/
- Git commit after each agent run
- Build output appended to .evolve/run.log
```

**Sub-PRD Pattern** (for design-heavy products):

When features have a design phase before build, use the sub-PRD convention:
- Feature names use suffixes: `F1-design`, `F1-build`, `F2-design`, `F2-build`, ...
- `F*-design` produces a sub-PRD in `.evolve/sub-prd/{feature_base}.md`
- `F*-build` reads the sub-PRD as its specification
- Adapter routes checks by suffix (design → doc quality, build → implementation)
- Parallel groups: design features can run in parallel, build features may have dependencies

```markdown
## Feature List
### Group A (parallel)
- [ ] F1-design — 功能 A 产品文档
- [ ] F2-design — 功能 B 产品文档

### Group B (after Group A)
- [ ] F1-build — 功能 A 实现
- [ ] F2-build — 功能 B 实现（依赖 F1-build 的数据格式）
```

Show to user: "Here's your program.md. Want to adjust?"

Also generate `.evolve/adapter.py`:
1. Read `adapters/base.py` for interface definition
2. Read `adapters/web_app.py` or `adapters/teaching.py` as reference
3. Auto-generate project-specific adapter

### Step 4: Validation + Branch Creation (automatic)

#### Validation

| Check | Rule | Failure |
|-------|------|---------|
| Product requirements | At least 1 non-empty | "Product requirements are empty." |
| Eval dimensions | At least 1 | "No eval dimensions." |
| Template placeholders | No `{{` or `[fill...]` | "program.md line N is still a placeholder" |
| adapter.py | Importable | "adapter.py load failed: {error}" |
| Python 3.8+ | Available | "Python 3.8+ required" |
| Git | Available | "Git required" |
| Uncommitted changes | Warn | "!! Recommend committing first" |

#### Setup

```python
import sys
sys.path.insert(0, '.claude/skills/evolve')
from prepare import load_adapter, load_eval_config
```

- `git checkout -b evolve/<tag>`
- Generate `.evolve/adapter.py` (from reference adapters + project scan)
- Create `.evolve/results.tsv` (header only)
- Create `.evolve/strategy.md` (empty template)
- Create `.evolve/run.log` (empty)
- Add `.evolve/` to `.gitignore`

#### Completion Summary (MUST follow this format)

After validation passes, O MUST present a detailed summary before asking user to start. Not a brief overview — full detail so user can make an informed decision.

**Required sections:**

1. **Feature list with order** — list every feature by name and execution sequence, not just count
2. **Evaluation dimensions** — each dimension's name, type (deterministic/llm-judged), and threshold
3. **Output target** — full file path(s) that will be created/modified
4. **How the loop works** — B writes code → C calls independent evaluator (Codex) to score → fail means fix and retry → pass means next feature. Each round ~2-3 min.
5. **Clickable file links** — provide paths to program.md and spec.md so user can open and review

Example:

```
✅ 验证通过，准备就绪。

## 执行计划

### Feature 列表（按顺序执行）
1. JWT 用户认证 — 注册/登录/刷新 token
2. 聊天接口 — WebSocket 实时消息
3. 文件上传 — 大小/类型校验 + S3 存储

### 评估维度
| 维度 | 类型 | 门槛 |
|------|------|------|
| 测试通过率 | deterministic (vitest) | 3.5 |
| 代码质量 | llm-judged (Codex) | 3.5 |
| API 设计 | llm-judged (Codex) | 3.5 |

### 产出文件
→ src/routes/*.ts, src/middleware/auth.ts, ...

### 循环机制
每轮 ~2-3 分钟：B 写代码并 commit → C 调 Codex 独立评估 → 不及格自动修 → 及格进下一个。
硬停止条件：总运行 24 小时 / 总轮数 100 / 单 feature 30 轮 / 连续崩溃 5 次。

### 查看详情
- program.md: .evolve/program.md
- spec.md: .evolve/spec.md

### 查看详情
- program.md: .evolve/program.md
- spec.md: .evolve/spec.md

需要调整吗？(Y/n)
```

**Confirmation 1** = program.md content + execution plan. User reviews WHAT will be done.

User confirms → O shows auto-start reminder:

```
⚠️ 确认后将自动启动循环（每分钟一轮，最长 24 小时）。
过程中你可以走开，AI 自动构建、评估、迭代。Ctrl+C 随时可停。

确认启动？(Y/n)
```

**Confirmation 2** = start running. User confirms the ACTION.

User confirms → O immediately invokes `Skill("loop", args="1m /evolve")`.

---

## prepare.py Function Reference

```bash
python -c "import sys; sys.path.insert(0, '.claude/skills/evolve'); from prepare import <func>; ..."
```

| Function | Signature | Description |
|----------|-----------|-------------|
| `load_eval_config` | `(path) -> list[dict]` | Parse eval.yml, return dimension list |
| `load_adapter` | `(path) -> module` | Load adapter from file path |
| `append_result` | `(tsv, row) -> None` | Append one row to results.tsv |
| `read_progress` | `(tsv) -> dict` | Read progress and state machine state |
| `generate_report` | `(tsv) -> str` | Generate structured progress report |
| `analyze_trajectory` | `(tsv, feature, window=3) -> dict` | Trend analysis (rising/flat/falling) |
| `should_stop` | `(tsv, feature) -> (bool, str)` | Code-enforced stop conditions |
| `validate_eval_result` | `(result) -> None` | Enforce independent evaluator |
| `get_evaluator` | `() -> str\|None` | Find available evaluator CLI |
| `acquire_lock` | `(dir) -> dict` | Acquire concurrency lock |
| `update_lock` | `(dir, phase, feature) -> None` | Update heartbeat |
| `release_lock` | `(dir) -> None` | Release lock |
