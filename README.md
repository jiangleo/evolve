[![zh-CN](https://img.shields.io/badge/lang-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-red.svg)](./README.md)
[![en](https://img.shields.io/badge/lang-English-blue.svg)](./README-en.md)

# Evolve

**定义目标，AI 自动构建、评估、迭代，直到达标。**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/tests-70%20passed-brightgreen.svg)]()
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)]()

一个 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) Skill。你定义想要什么和怎么算好，AI 自己写代码、自己打分、不及格自己改，直到过线。灵感来自 [Anthropic 的 harness design](https://www.anthropic.com/engineering/harness-design-long-running-apps) 和 [Karpathy 的 autoresearch](https://github.com/karpathy/autoresearch)。

```
你: "做一个带认证和文件上传的 REST API"
  -> Evolve 自动跑 14 轮
  -> 每个功能由独立 LLM 评估器打分
  -> 3 个原子 commit 在功能分支上，随时可以 merge
```

---

## 什么时候用

你有明确的质量标准，但手动一轮轮改太慢。让 AI 替你跑这个循环。

| 场景 | Evolve 做什么 |
|------|-------------|
| 从零构建 Web 应用 | 逐个实现功能，每个跑测试，不过就自动修 |
| 调优 AI 聊天机器人 | 模拟真实用户跟你的 bot 聊天，给对话质量打分，改 prompt 直到分数过线 |
| 做教学材料 | 写内容 → 按教学标准评估 → 改到所有维度达标 |
| 改进现有代码 | 逐个功能重构，每次改完评估，质量下降就回滚 |

**不适合：** 一次性任务、快速修 bug、或者你自己判断比跑评估循环更快的场景。

## 前置条件

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)（CLI 或 IDE 扩展）
- Python 3.8+
- Git

> `/evolve` 和 `/loop` 是你在 Claude Code 聊天框里输入的斜杠命令，不是 shell 命令。如果你不熟悉 Claude Code skills，看 [skills 文档](https://docs.anthropic.com/en/docs/claude-code/skills)。

---

## 快速开始

### 1. 安装

```bash
mkdir -p .claude/skills
git clone https://github.com/jiangleo/evolve .claude/skills/evolve
```

### 2. 初始化（交互式，约 5 分钟）

在 Claude Code 里输入 `/evolve`，它会引导你完成配置：

```
Step 1  扫描项目              （自动）
Step 2  头脑风暴              （3-5 个问题，帮你理清目标）
Step 3  生成 program.md       （含功能列表 + 评估标准，你确认）
Step 4  校验 + 创建分支        （自动）
```

### 3. 启动自动循环（无人值守）

```
/loop 1m /evolve
```

> `/loop` 是另一个 Claude Code skill，按间隔重复运行斜杠命令。没有 `/loop`？每次手动输入 `/evolve` 也行——会从文件自动恢复状态。

AI 会逐个功能处理：构建 → 提交 → 评估 → 不过就修 → 过了就下一个。

你可以走开。回来看 `.evolve/report.md` 就知道进度。

### 4. 查看进度

再次输入 `/evolve` 查看当前状态：

```
# Evolve Progress — 第 12 轮
  OK  用户认证       — 第 3 轮达标 (7.8)
  OK  数据导入       — 第 5 轮达标 (8.2)
  >>  API 限流       — 尝试 2 次，上轮: "缺少滑动窗口实现"
  ..  数据导出       — 未开始
```

---

## 完整示例

<details>
<summary><b>端到端演示：从零构建 REST API</b></summary>

**你输入：**
```
/evolve
```

**Evolve 扫描项目后问你：**
```
检测到 Node.js + Express 项目，有 vitest。
你想构建什么？
```

**你说：**
```
一个 REST API，要有用户认证（JWT）、聊天接口、文件上传。
```

**Evolve 生成评估标准让你确认：**
```
评估维度：
  1. 测试通过率 — deterministic (vitest)，门槛 7.0
  2. 代码质量 — llm-judged，门槛 7.0
  3. API 正确性 — llm-judged，门槛 7.0
要调整吗？(Y/n)
```

**你确认，然后启动循环：**
```
/loop 1m /evolve
```

**30 分钟后你回来看：**
```
# Evolve Progress — 第 14 轮
  OK  JWT 认证      — 第 4 轮达标 (8.1)
  OK  聊天接口      — 第 8 轮达标 (7.6)
  >>  文件上传      — 尝试 2 次，上轮："缺少文件大小校验"
```

**1 小时后：**
```
# Evolve Progress — 第 22 轮
  OK  JWT 认证      — 第 4 轮达标 (8.1)
  OK  聊天接口      — 第 8 轮达标 (7.6)
  OK  文件上传      — 第 16 轮达标 (7.8)
  所有功能达标。完成。
```

**你看结果：**
```bash
git log --oneline evolve/rest-api
# a1b2c3d feat: JWT auth with refresh tokens
# b2c3d4e feat: chat endpoint with rate limiting
# c3d4e5f feat: file upload with size + type validation
```

三个原子 commit 在功能分支上，随时可以 merge。

</details>

---

## 核心概念

**三个 Agent，各司其职** — O (Orchestrator) 和你对话、调度；B (Builder) 只写代码；C (Critic) 评估 + 做战略决策。写代码的和打分的不是同一个 Agent，独立评估器（Codex/Claude CLI）由代码强制调用。

**一切都是文件** — 状态全在 `.evolve/` 里，没有数据库，没有服务。删掉这个目录就回到原点。

| 文件 | 作用 | 谁写的 |
|------|------|--------|
| `program.md` | 你的目标 + 功能列表 + 评估标准 | 你（Init 时） |
| `adapter.py` | 怎么启动/测试你的项目 | Init 自动生成 |
| `strategy.md` | C 的战略决策：方向、轨迹、下一步 | C（每轮覆写） |
| `results.tsv` | 完整迭代记录 | B + C（只追加） |
| `run.log` | 所有 Agent 输出 | O + B + C（只追加） |

**Adapter** — Init 时根据你的项目自动生成 `adapter.py`，告诉 Evolve 怎么跑你的项目。仓库里附了三个参考：

| Adapter | 适用 | 打分方式 |
|---------|------|---------|
| `web_app.py` | Web 应用（FastAPI、Flask、Node） | 测试通过率 + LLM 评审 |
| `teaching.py` | 教学内容 | 全 LLM 打分 |
| `chat_agent.py` | 对话 AI agent（[OpenClaw](https://github.com/nicepkg/openclaw)） | 模拟对话 + LLM 打分 |

---

## 注意事项

<details>
<summary><b>开始前</b></summary>

- **先 commit 你的代码。** Evolve 会创建 `evolve/<tag>` 分支。你的主分支不受影响，但未提交的改动可能会乱。
- **有测试最好。** 有真实测试的确定性打分靠谱得多。
- **选对评估器。** 用同一个写代码的 Claude 来打分，等于自己给自己批作业。

</details>

<details>
<summary><b>运行中</b></summary>

- **别删 `.evolve/`。** 它是循环的全部记忆。
- **可以改 `spec.md`。** 加功能、删功能、调顺序都行，下一轮自动读取。
- **别随便改 `program.md`。** 它是你和 AI 的合约。
- **24 小时 / 100 轮硬上限。** 自动停止，防止跑飞。
- **卡住了看 `run.log`。** 构建输出都在那里。

</details>

<details>
<summary><b>完成后</b></summary>

- **看 git log。** 每个功能一个 commit，在 `evolve/<tag>` 分支上。
- **看 `report.md`。** 哪些通过了、哪些跳过了、为什么。
- **满意了就删 `.evolve/`。** 默认已 gitignore，不是设计来长期保留的。

</details>

---

## 设计选择

| 决策 | 原因 |
|------|------|
| 做成 Claude Code skill | 直接复用 Claude Code 的文件、git、命令权限，不用自己造轮子 |
| 3 Agent (O/B/C) 单循环 | 比内外循环简单，C 拥有最新上下文做战略决策 |
| 独立评估器由代码强制 | `validate_eval_result()` 在 prepare.py 里，AI 绕不过去 |
| strategy.md 跨 session | 每轮新 session，但 C 的战略决策通过文件持久化 |
| `should_stop()` 在 AI 启动前运行 | AI 不参与停止决策，硬编码在 prepare.py |
| 锁 2 分钟过期 | 防撞车，崩了也不卡下一轮 |

## 运行测试

```bash
python -m pytest tests/ -v    # 70 个测试，约 0.1 秒
```

## 许可证

MIT
