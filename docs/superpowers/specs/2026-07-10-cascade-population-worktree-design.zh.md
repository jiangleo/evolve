# 设计：确定性校验级联、种群分支、Worktree 隔离

日期：2026-07-10
状态：已批准
英文版：[2026-07-10-cascade-population-worktree-design.md](./2026-07-10-cascade-population-worktree-design.md)

## 动机

对照当前业内最佳 harness 实践（Anthropic harness design、AlphaEvolve 式种群
搜索、worktree 隔离的并行 builder），evolve 存在三个差距：

1. **过度依赖 LLM 评审。** 即使构建已经明显坏掉（服务 500、`.next` cache
   腐坏），每轮 eval 仍要付出 5 维 LLM judge 的成本，产生全 0 分轮次，伪装成
   产品回归。绝对 1-10 打分还会逐轮漂移，导致 `analyze_trajectory()` 对评审
   噪声而非真实趋势做出反应。
2. **单谱系死磕。** 卡住的 feature 只在一条轨迹上反复重试，直到 `forced_pass`
   放行。在一次真实的 35/35 session 中，35 个 pass 里约 16 个是 forced ——
   "达标"悄悄变成了"弃权"。
3. **Builder 串行化。** `build_lock` 同一时间只允许一个 B，限制了多 feature
   运行的吞吐；并行工作会在同一个工作树里互相冲突。

## 指导原则

保证必须活在 AI 之前/之外运行的 Python 代码里（沿用现有的
`validate_eval_result` / `should_stop` 模式），绝不能只写在 agent markdown
里。新代码放进新模块 —— skill 根目录下的 `cascade.py`、`worktree.py`、
`population.py` —— 由 `prepare.py` 重新导出公开函数，保持文档中
`from prepare import ...` 的接口稳定。prepare.py（1,340 行）不得显著增长。

## 第一部分 —— 确定性级联 + 成对比较轨迹

### 级联（`cascade.py`）

- `eval.yml` 新增可选的顶层 `cascade:` 段：

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

- `load_eval_config()` 负责解析；没有该段 → 空级联（老项目行为不变）。
- 新增 `run_cascade(evolve_dir, feature, stages) -> dict`，按顺序执行各
  stage，**fail-fast**：第一个失败的 stage 立即中止本轮。失败 stage 的输出
  尾部写入 `.evolve/{feature}/cascade_fail.md` 并进入本轮 summary。
- **Stage 0 隐式且始终存在：** 由 `adapter.setup()` 派生的服务存活检查
  （adapter 可声明 `health_check()` 返回 ok/fail；默认为"setup() 没有
  crash"）。这把"派 C 前必须验证服务 200"从 README 警告变成代码强制。
- results.tsv 新增状态 `cascade_fail`（加入 `VALID_STATUSES`）：
  scores 为 `-`，total 为 `0`，summary 引用失败 stage。`cascade_fail` 轮次
  对轨迹分析无效（类似 chat adapter 的 `gate_fail`，本设计是它的泛化）。
- **强制执行：** `validate_eval_result()` 额外要求 eval 结果携带
  `cascade: passed`（或显式的空级联标记），否则任何 LLM 评审分数都不被
  接受。C 无法跳过级联。

### 成对比较轨迹

- 当存在上一轮 eval 时，`prepare_dispatch()` 在 `dispatch_C.md` 中加入
  `## Previous Round Evidence` 段（指向上一轮 `eval_*.md` / evidence 目录的
  路径）。
- 评审必须按维度输出可解析的 `pairwise: better|same|worse`，由 C 记录。
- `results.tsv` 新增**可选第 8 列** `pairwise`（如
  `log:better/ui:same/db:worse`）。读取方（`read_progress`、
  `analyze_trajectory`、`generate_report`）同时接受 7 列和 8 列的行；
  `append_result` 对 eval 行写 8 列，其余写 `-`。
- 及格判定**不变**：绝对分数对比各维度 threshold。
- `analyze_trajectory()` 在有成对判定时优先使用它，而非原始分数差。矛盾
  规则：若分数差与成对多数意见方向相反（分数涨了但多数维度 `worse`，或
  反之），trend 记为 `noisy`，本轮不贡献任何轨迹信号 —— 评审漂移无法再
  触发错误的 Pivot/Rollback。

## 第二部分 —— Worktree 隔离（`worktree.py`）

- 每个 feature 的 B 在 `.evolve/worktrees/{feature}` 工作，对应分支
  `evolve/<tag>--{feature}`（兄弟 ref：git 不允许在已有分支名下嵌套分支），由
  `create_feature_worktree(evolve_dir, feature) -> path` 创建。
- **build_lock 语义变更：** 不再是"同一时间只有一个 B"。它现在只串行化
  真正的临界区 —— 合入 `evolve/<tag>`。多个 B 可跨 feature 并行（并发上限
  仍为 5，写在 loop.md 指引中）。
- 迭代期间 C 在 worktree **内部**评估（adapter 函数本就接受
  `project_dir`）。
- **过线即合并 + 集成门：** `merge_feature(evolve_dir, feature) -> dict`：
  1. 拿 build_lock，将 feature 分支合入 `evolve/<tag>`；
  2. 在合并后的代码树上重跑确定性级联；
  3. 门通过 → 保留合并，删除 worktree + 分支，feature 记 `completed`；
  4. 门失败（冲突或级联回归）→ revert 合并，写
     `.evolve/{feature}/merge_conflict.md`（什么坏了、冲突文件），feature
     退回 `needs_build`。
- **资源冲突：** `adapters/base.py` 新增可选的
  `allocate_slot(n) -> dict`（第 n 个并行实例的环境变量覆盖）；
  `web_app.py` 演示按 slot 偏移 PORT。未实现该函数的 adapter 视为无冲突
  （文档/教学类 adapter）。
- **泄漏清理：** `acquire_lock()` 尽力清理陈旧 worktree：completed
  feature 的 worktree+分支**只有在分支已合入 base**（`git merge-base
  --is-ancestor`）时才删除 —— 已 pass 但未合并的分支必须在崩溃后幸存，
  等 O 来合并。逐项隔离失败（一个坏 worktree 不会中断其余清理）。

## 第三部分 —— 种群分支（`population.py`）+ 受门控的 forced_pass

### 升级阶梯（取代扁平的"≥5 轮 → forced_pass"）

```
consecutive_fails ≥ 3   → Mentor 建议（不变，现有行为）
consecutive_fails ≥ 6   → 分支：派生 N=3 个候选
所有候选都失败           → forced_pass 变为可用（仍需用户明确批准；由 O 询问）
```

与现有"mentor 建议 #3 → BLOCKER"规则（critic.md）的交互：分支插在
BLOCKER **之前**。建议 #3 检查不再直接标记 BLOCKER，而是使 feature 具备
分支资格。只有当分支已失败**且**用户拒绝 forced_pass 时才到达 BLOCKER ——
它仍是终态跳过状态。

### 机制

- `spawn_candidates(evolve_dir, feature, n=3) -> list[dict]` 从 feature 当前
  分支派生 N 个 worktree `.evolve/worktrees/{feature}-cand{i}`，对应分支
  `evolve/<tag>--{feature}-cand{i}`。每个候选的 `strategy.md` 以一个
  **彼此不同的方案**作为种子，来源是 Mentor 的假设和 C 未尝试过的 Pivot
  选项；种子由 O 写入。
- O 并行派出 N 条 B→C 链，每条对应一个候选 worktree（复用第二部分的
  机制；候选计入 5 并发上限）。
- 候选轮次以 feature id `F01@cand2` 记入 results.tsv，历史可审计，且
  `read_progress` 将其归组到父 feature 下。
- `select_candidate(evolve_dir, feature) -> dict` 选出胜者：
  1. 必须通过确定性级联；
  2. **最低维度分**最高者胜；
  3. 平局：total 更高者，再平则看与在位谱系的 pairwise 对比。
  胜者走正常集成门合入；败者的 worktree 与分支删除。
- 预算：`HARD_LIMITS["max_branching_rounds_per_feature"] = 1` 与
  `HARD_LIMITS["candidates_per_branching"] = 3`（可经 program.md 覆盖）。
- `scan_all_features()` 新增状态 `branching`（候选运行中）。

### forced_pass 门控

- `can_force_pass(evolve_dir, feature) -> (bool, reason)`：仅当一轮分支
  已完成且无胜者时返回 True。
- `mark_forced_pass(evolve_dir, feature, user_approved: bool)` 是唯一合法
  入口；它检查门控并追加一行新状态 `forced`（加入 `VALID_STATUSES`）。
- `read_progress()` / `generate_report()` 将 `forced` 单独计数：报告显示
  `passed: M true + K forced / T` —— 被弃权的 feature 永远不会被当作
  真 pass 展示。

## 文档更新

- `loop.md`：调度流程（并行 B、worktree 生命周期、branching 阶段、过线即
  合并）、并发规则、新文件的权限矩阵行。
- `agents/critic.md`：级联优先的单轮流程、pairwise 输出格式、
  cascade_fail 处理（对齐现有 gate_fail 协议）。
- `agents/orchestrator.md`：升级阶梯、候选种子职责、forced_pass 门控
  （"仅当 can_force_pass 为 True 时才问用户"）。
- `agents/builder.md`：工作发生在 feature worktree 内；绝不直接改
  `evolve/<tag>`。
- `README.md` / `README-en.md`：核心原则、带 true/forced 区分的进度展示、
  更新测试 badge。

## 测试

在 tmp 目录用一次性 git 仓库做单元测试（与现有快速测试套件一致）：

- 级联：stage 顺序、fail-fast 短路、隐式健康检查 stage、缺少级联标记时
  `validate_eval_result` 拒绝；
- pairwise：7/8 列 TSV 读写往返、轨迹分析优先采用 pairwise、矛盾 →
  `noisy`；
- worktree：创建/删除、过线即合并的 happy path、集成门冲突时 revert、
  陈旧 worktree 清理；
- 种群：候选派生/种子、`F01@cand*` 在 read_progress 中的归组、
  select_candidate 排序规则、预算强制；
- forced_pass：分支前门关闭、全败后门打开、`forced` 在报告中单独计数。

## 范围外

- 每 feature 多轮分支（默认预算保持 1）。
- 跨 feature 候选共享或 MAP-Elites 式档案库。
- 更改评审 CLI 优先级或 judge 模型选择。
- 迁移已存在的 `.evolve/` 状态目录（仅对新运行生效；旧 results.tsv 通过
  7 列兼容仍可读取）。
