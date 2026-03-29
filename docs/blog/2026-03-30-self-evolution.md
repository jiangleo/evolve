# Evolve V2: 一个 AI 系统用 18 轮自进化完成了自己的重写

## 背景

Evolve 是一个 Claude Code skill，让 AI 自主构建、评估、迭代，直到目标达成。V1 有个核心问题：AI 只在细节上修修补补，没有宏观思维。每轮 `/loop 1m /evolve` 是一个新 session，上一轮的战略决策全部丢失。

V2 的设计目标：加入跨 session 战略记忆（strategy.md）、轨迹分析（analyze_trajectory）、独立评估器强制执行、代码级停止条件。设计文档写了 550 行，经过 10 个学术校验 agent 验证。

然后我们做了一件事：**让 V1 的 evolve 自己把自己改成 V2。**

## 怎么做到的

### Danger Mode

Evolve 有一条安全规则：`Do not modify files under .claude/skills/evolve/`。这是为了防止 AI 改动自己的评估基础设施。

但如果目标就是改自己呢？

1. 去掉这条规则（Danger Mode）
2. 在 `.evolve/program.md` 里写上完整的 V2 设计文档作为目标
3. `/loop 1m /evolve` 启动
4. 完成后加回规则

相当于暂时解除免疫系统，做完手术再装回去。

### 6 个 Feature，按安全顺序排列

```
Feature 1: prepare.py    ← 最安全，加新函数不影响现有逻辑
Feature 2: tests         ← 验证 Feature 1
Feature 3: agent 定义     ← 新文件，不影响现有代码
Feature 4: loop.md       ← 控制面，危险
Feature 5: SKILL.md      ← 控制面，危险
Feature 6: README        ← 文档，最后改
```

关键：控制面（loop.md、SKILL.md）放最后。如果放前面，改坏了 loop 逻辑，后续所有轮次都无法运行。

## 18 轮运行记录

```
Round  Time   Phase  Feature           Score   Status  发生了什么
─────  ─────  ─────  ────────────────  ──────  ──────  ────────────────────────────────
 1     01:12  build  prepare.py        -       keep    加了 6 个新函数 + 2 个常量
 2     01:14  eval   prepare.py        8.9     pass    codex 评 9/10，一次过
 3     01:19  build  tests             -       keep    46 个新测试全过
 4     01:20  eval   tests             8.0     FAIL    codex 评 6/10：缺 should_stop 的完整覆盖
 5     01:23  build  tests             -       keep    补了 5 个测试，51 个全过
 6     01:25  eval   tests             8.5     pass    codex 评 7/10，刚好过线
 7     01:28  build  agent definitions -       keep    创建 O/B/C 三个 agent 定义文件
 8     01:30  eval   agent definitions 9.5     pass    codex 评 9/10，一次过
 9     01:33  build  loop.md           -       keep    重写状态机、should_stop 门控、B/C 派发
10     01:35  eval   loop.md           8.0     FAIL    codex 评 6/10：init/plan 残留 + Human 列
11     01:38  build  loop.md           -       keep    清理 init/plan 残留，权限矩阵改纯 O/B/C
12     01:42  build  loop.md           -       keep    继续清理 plan/keep 引用，加路由表
13     01:44  eval   loop.md           9.0     pass    codex 评 8/10，第三轮才过
14     01:47  build  SKILL.md          -       keep    重写为 4 步 Init + brainstorming
15     01:49  eval   SKILL.md          9.0     pass    codex 评 8/10，一次过
16     01:52  build  README            -       keep    两份 README 更新为 V2 架构
17     01:54  eval   README            10.0    pass    codex 评 10/10，完美
18     01:55  done   -                 -       -       6/6 features passed
```

> 时间为 2026-03-30 北京时间，从 git commit 时间戳提取。

### 关键数据

- **总耗时**：40 分钟（01:12 → 01:52，从第一个 commit 到最后一个 commit）
- **平均每轮**：~2.5 分钟（含 build/eval + codex 调用 + /loop 1m 等待）
- **总轮次**：18（12 轮 build + 6 轮 eval pass + 2 轮 eval fail = 18 轮有效执行）
- **一次过的 feature**：4/6（prepare.py, agent definitions, SKILL.md, README）
- **需要修的 feature**：2/6（tests 修了 1 轮，loop.md 修了 2 轮）
- **修复原因**：全部是 codex 指出的设计合规问题，不是代码 bug
- **最终 commit 数**：10（9 个 feature commit + 1 个安全规则恢复）
- **代码变更**：+869 行 / -319 行，涉及 9 个文件

## 有趣的发现

### 1. 独立评估器确实有用

两次 FAIL 都是 codex 发现的，不是测试发现的：

- Round 4：codex 指出 `should_stop` 缺少 `max_rounds_per_feature` 和 `max_flat_after_pivot` 测试。测试本身 100% pass，但测试覆盖不够。如果没有 codex，这些缺失的测试就会被跳过。

- Round 10：codex 指出 loop.md 里残留了 V1 的 `init/plan/keep` 状态。纯文档问题，没有代码测试能抓到。

这验证了 V2 设计的核心假设：**构建者不能评估自己。** 写代码的 AI 认为自己写完了，但另一个模型（codex）能看到它遗漏的东西。

### 2. 控制面确实更难改

- prepare.py（纯函数）：1 轮 build + 1 轮 eval = 2 轮搞定
- tests（纯测试）：2 轮 build + 2 轮 eval = 4 轮搞定
- agent 定义（新文件）：1 轮 build + 1 轮 eval = 2 轮搞定
- **loop.md（控制面）：3 轮 build + 3 轮 eval = 6 轮搞定**
- SKILL.md（控制面）：1 轮 build + 1 轮 eval = 2 轮搞定
- README（文档）：1 轮 build + 1 轮 eval = 2 轮搞定

loop.md 耗时最长，因为它描述的是系统自身的运行逻辑。改自己的规则比改业务代码难——这跟人类重构框架比写业务代码难是同一个道理。

### 3. "安全顺序" 策略有效

把 loop.md 和 SKILL.md 放在最后是正确的。如果先改 loop.md，改坏了状态机，后续所有轮次都会读到错误的路由逻辑。但因为先改了 prepare.py 和 tests，即使 loop.md 在 Round 10 改出了问题，测试依然能跑，状态机依然能路由到修复流程。

### 4. 鸡生蛋问题的解法

"用 V1 改出 V2" 的前提是 V1 的 loop 机制在整个过程中保持可用。这要求：

1. **向后兼容**：新加的函数不破坏旧的调用方式
2. **增量修改**：每轮只改一小块，不做大爆炸式重写
3. **每轮 commit**：改坏了能 rollback
4. **最坏情况兜底**：git 能恢复一切

这其实就是操作系统在线升级的思路：先升级用户态程序，最后才换内核。

## 没做到的

1. **strategy.md 没有真正使用**：因为 V1 的 loop 没有 C agent 和策略机制，这 18 轮都是 "build → eval → fix" 的简单循环，没有 C 的战略决策。strategy.md 只是被写出来了，没有被消费。V2 的真正价值需要在下一个项目上验证。

2. **codex 评分有波动**：同一个 feature 的同一个版本，codex 在 Round 10 给了 6/10（批评 Human 列），Round 12 给了 4/10（又说应该加回 Human 列）。独立评估器有自己的随机性，这是 V2 设计文档里已经预见到的问题（NAACL 2025: 89% 误差方差）。adapter.py 的确定性测试是真正的地板。

3. **`load_eval_config` 还是读 eval.yml**：设计文档说应该从 program.md 解析评估标准，但 prepare.py 的 `load_eval_config()` 还是只读 eval.yml 格式。codex 在 Round 15 指出了这个残留，但分数仍在阈值以上就没修。技术债。

## 结论

18 轮，40 分钟（01:12 → 01:52），一个 AI 系统用自己的 V1 版本把自己重写成了 V2。

这不是 AGI。这是一个设计良好的 harness + 一个足够聪明的模型 + 一个愿意按 "Danger Mode" 的人类。

真正有价值的不是 "AI 能自己改自己" 这个噱头，而是这个过程验证了 V2 的几个核心设计假设：

- **独立评估器能抓到构建者的盲点**（2 次 FAIL 都是 codex 发现的）
- **安全顺序排列 feature 能保护控制面**（loop.md 改坏了不影响其他 feature）
- **每轮 commit 让 rollback 成本趋近于零**（虽然这次没用到）
- **代码级停止条件让系统不会跑飞**（should_stop 在 AI 启动前运行）

V2 的设计是对的。现在需要在一个真实项目上跑起来，看 strategy.md 和 C 的 6 选项策略菜单能不能真正解决 "AI 只修细节不看全局" 的问题。
