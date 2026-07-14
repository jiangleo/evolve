# 设计：Tier 1 Token 消耗优化

日期：2026-07-10
状态：已批准
英文版：[2026-07-10-token-optimization-tier1-design.md](./2026-07-10-token-optimization-tier1-design.md)
基于：[2026-07-10-cascade-population-worktree-design.zh.md](./2026-07-10-cascade-population-worktree-design.zh.md)（同一分支）

## 动机

业内 agent 循环经济学数据：agent 比对话烧 50 倍以上 token，重复发送的上下文
占账单约 62%，朴素循环成本 O(N²) 复合增长。成熟杠杆有五个：prompt
caching、上下文压缩、模型分级路由、记忆外置、按需读取。evolve 的文件化架构
已经天然占据"记忆外置"（每轮 fresh context、状态在 `.evolve/`），确定性级联
也已消灭了最浪费的评审调用。本 spec 应用其余的低风险杠杆。

**硬约束：零功能变化。** 不改评分语义、不改 pass/fail 行为、不改调度决策。
每项独立可回退，有旋钮的都可用环境变量覆盖。

## 第 1 项 —— Previous Round Evidence 截断（压缩）

**位置：** `prepare.py` 的 `prepare_dispatch()`，为 C 添加的
`## Previous Round Evidence` 块。

**现状：** 上一轮 `eval_*.md` 全文内联——judge 文件通常 15–30KB；中间是过程
性记录，得分在开头、结论和 rationale 在结尾。

**改法：**

```python
EVIDENCE_CAP = int(os.environ.get("EVOLVE_EVIDENCE_CAP", "6000"))  # 字符
```

文件超过 `EVIDENCE_CAP`（且 cap > 0）时：保留开头 1,000 字符 + 结尾
`EVIDENCE_CAP - 1000` 字符，中间用显式标记 `[... truncated N chars ...]`
连接。`EVOLVE_EVIDENCE_CAP=0` 完全关闭截断。

**为什么安全：** pairwise 判定只喂轨迹分析（从不进 pass 门），且
`analyze_trajectory` 的矛盾→`noisy` 规则是兜底——即使截断偶尔让判定失真，
也不会触发错误决策。

## 第 0 项 —— 前置 bugfix：build_manifest 泄漏 build_lock

**位置：** `prepare.py` 的 `build_manifest()`（约第 771 行）。

**现状：** "build lock 状态"这一行是通过**真的调用**
`acquire_build_lock()`（会实际拿锁）产生的，且从不释放。这是历史遗留泄
漏，在旧的 120s 过期规则下能自愈；但本分支的 I3 修复把
`BUILD_LOCK_STALE_SECONDS` 提到 1800 后，每次生成 manifest 都会把合并
毒死最长 30 分钟。

**改法：** 探测但不持有——`acquire_build_lock` 成功则立即
`release_build_lock(evolve_dir, token)` 并报告 "free"；失败则报告锁定
原因。回归测试：`build_manifest()` 之后，新的 `acquire_build_lock()`
必须成功。

## 第 2 项 —— Manifest 摘要缓存（消除重复计算）

**位置：** `prepare.py` 的 `build_manifest()`。

**现状：** 每次调用都跑 `_haiku_summarize()`——真实 LLM API 调用——哪怕自
上一轮以来什么都没变。1 分钟 cadence + 20 分钟构建在途的场景下，约 19 次
调用是白费的。

**改法——缓存摘要，绝不缓存整个 manifest。** manifest 的 `Status` /
`Feature States` 段包含易变状态（锁持有者、`in_progress` 标记、基于时间
的 should_stop），这些不依赖文件变化——缓存整个 manifest 会呈现过期状态，
违反零功能变化约束。所以 manifest 每次都新鲜组装（便宜、确定性），只缓存
昂贵的摘要调用：

- 指纹 = `sha256(json of {round, phase, feature, raw_files})`，其中
  `raw_files` 正是传给 `_haiku_summarize` 的那个 dict
- 缓存文件 `.evolve/manifest_summary.json`：
  `{"fingerprint": ..., "summary": ...}`
- 指纹命中 → 复用缓存摘要，零 LLM 调用；未命中 → 重新摘要并覆写缓存

**为什么安全：** 指纹覆盖摘要叙述的全部内容输入。易变的锁/时间状态仍可
经 status_text 到达摘要器而不改变指纹——可接受，因为权威的 Status 段每次
都新鲜重算且就在摘要正上方。最坏失败模式是缓存文件损坏 → 多一次冗余摘要
调用。

## 第 3 项 —— Manifest 摘要降级小模型（路由）

**位置：** `prepare.py` 常量 + `_haiku_summarize()`。

**现状：** H 升级到 Sonnet 4.6（`HELPER_MODEL`）时，manifest 摘要调用被
顺带升级。但它只是"把状态压成 3–5 行"、输出 ≤300 token 的活——小模型的活。

**改法：**

```python
MANIFEST_MODEL = os.environ.get(
    "EVOLVE_MANIFEST_MODEL", "claude-haiku-4-5-20251001")
```

`_haiku_summarize()` 改用 `MANIFEST_MODEL`。H 本体（context scoping、
dispatch 组装）保持 `HELPER_MODEL` 不变——只有这一个调用被路由降级。

## 第 4 项 —— Judge 输出结构化（压缩，文档契约）

**位置：** `agents/critic.md`（+ H 嵌入 `dispatch_C.md` 的 Evaluator
Prompt 指引）。

**现状：** 对 judge 输出格式没有约束，经常写成小作文。这个输出计费两次：
一次作为 judge 输出，一次作为下一轮的 Previous Round Evidence 输入。

**改法：** 增加强制输出契约：

- 每个维度恰好一行：`<维度名>: <分数> — <理由，≤30 字>`
- 之后一个 pairwise 块：每维一行 `<维度名>: better|same|worse — <一句话依据>`
- 最后至多 3 行总评
- 禁止转录对话内容、禁止粘贴日志原文——引用 evidence 文件路径即可

**为什么安全：** 详细依据仍在 evidence 目录里（gate 报告、日志采样），
judge 只是不再复述。与第 1 项复利：多数 eval 文件将不再触发截断上限。

## 第 5 项 —— Mentor 输入封顶（压缩，文档契约）

**位置：** `agents/mentor.md`。

**现状：** 每小时 3 个 Opus mentor 各读无上限历史——results.tsv 全表、完整
evidence、完整 commit log。session 后半程一次就是数万 token × 3 × 每小时。

**改法：** 增加强制输入预算段：

- results.tsv：只读最近 30 行（`tail -30`）；更早的用一行概括
  （"此前 N 轮，M pass"）
- evidence / 日志文件：每个 ≤2,000 字符，优先结尾
- git 历史：`git log --oneline -20`，不看逐个 diff
- 每份 META 报告 ≤60 行

**为什么安全：** Mentor 闭环本来就通过 META 文件滚动携带上轮建议及其实测
后果——跨窗口的关键结论不依赖重读原始历史。

## 第 6 项 —— 缓存友好的 dispatch 排布（prompt caching）

**位置：** `prepare.py` 的 `prepare_dispatch()` 段落组装。

**现状：** 组装顺序是：header → `## Note from O`（易变，每轮都不同）→
文件内容 → evidence。易变段在最前意味着任何 provider 侧前缀缓存从第 1
字节就失效。

**改法：** 确定性的"稳定优先"排序：

1. header（`# Dispatch: B|C`）
2. `file_list` 中的已知稳定文件，保持相对顺序——按解析后的文件名判定
   （去掉 `:行号范围` 或 `#章节` 后缀之后的部分）：
   `program.md`、`eval.yml`、`spec.md`、`adapter.py`
3. 其余 `file_list` 条目（strategy.md、tail、mentor advice……），保持
   相对顺序
4. `## Note from O`（易变）
5. `## Previous Round Evidence`（易变，仅 C）

**为什么安全：** dispatch 文件被 B/C 整体消费，段落顺序不承载语义（现有
文档从未承诺顺序）。codex exec 能否吃到 provider 前缀缓存不受我们控制，
但这个排布零成本，且 O 自己的 Claude session 确实受益于稳定前缀布局。

## 测试

单元测试（tmp 目录模式，无网络——摘要器用 monkeypatch）：

- 第 1 项：超限文件被截断且标记存在、头尾尺寸正确；未超限文件不动；
  cap=0 关闭截断。
- 第 0 项：`build_manifest()` 之后，新的 `acquire_build_lock()` 成功
  （无锁泄漏）。
- 第 2 项：输入不变时第二次 `build_manifest` 不调用摘要器（monkeypatch
  哨兵，被调即抛异常）并复用缓存摘要；任何原始输入变化使缓存失效；缓存
  命中时结构化 Status 段仍然新鲜（例如锁状态变化照常反映）。
- 第 3 项：`MANIFEST_MODEL` 默认值 + 环境变量覆盖；`_haiku_summarize`
  以它作为 `model` 传参。
- 第 6 项：组装出的 dispatch 中 program.md 内容在 strategy.md 内容之前、
  `## Note from O` 在所有文件段之后、evidence 在最后。
- 第 4、5 项是 agent 契约文档：靠评审把关，无单元测试。

## 范围外（Tier 2，等实测数据后决策）

- dispatch 按路径引用（不再内联 program.md；codex 按需自读）
- loop.md 重构为紧凑的每轮 runtime card
- Judge 调用合并 / 多维度合一调用
- 任何评分语义、阈值或升级阶梯的改动
