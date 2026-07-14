# 全 Claude 部署配置（All-Claude Profile）

evolve 默认 B/C 走 codex exec（gpt-5.4-high）。本文档是全 Claude 家族的替代
配置——适用于没有 codex/Cursor、或希望统一在 Anthropic 生态内运行的场景。

## 模型分配

| 位置 | 模型 | Effort | 理由 |
|---|---|---|---|
| O (Orchestrator) | `claude-opus-4-8`（会话模型） | xhigh | 长时程调度判断；不用 Fable 5——分钟级单请求与循环 cadence 相冲 |
| H (Helper) | `claude-sonnet-5` | low/medium | context 定位/dispatch 组装，近 Opus 质量、Sonnet 价格 |
| manifest 摘要 | `claude-haiku-4-5-20251001`（默认） | — | 300 token 压缩任务 |
| B (Builder) | `claude-fable-5` | high 起步 | 最强编码与长时程执行；单请求可运行数分钟（B 是后台子进程，不阻塞 O） |
| C (Critic/judge) | `claude-opus-4-8` | high | 证据驱动评审；跨档位 + 新鲜上下文弥补同家族独立性损失 |
| M (Mentor ×3) | `claude-opus-4-8`（预算宽松可 `claude-fable-5`） | high/xhigh | 低频高难度反思 |

## 启用方式（一次配置，全局生效）

在 `~/.claude/settings.json` 的 `env` 中（或 shell profile）设置：

```json
{
  "env": {
    "EVOLVE_EVALUATOR": "claude",
    "EVOLVE_HELPER_MODEL": "claude-sonnet-5",
    "EVOLVE_MANIFEST_MODEL": "claude-haiku-4-5-20251001"
  }
}
```

`EVOLVE_EVALUATOR=claude` 强制评估器走 claude CLI——即使机器上装着更高
优先级但已损坏的 codex/agent 也不会误选。B 的 dispatch 命令见下节。

## B/C dispatch 命令替换

loop.md 的 codex exec 调用替换为 claude CLI（evolve 的评估器链路本就支持 claude）：

```bash
# B（在 feature worktree 内）
claude -p --model claude-fable-5 "$(cat .evolve/{feature}/dispatch_B.md)"

# C 的独立评审
claude -p --model claude-opus-4-8 "<evaluator prompt per critic.md>"
```

## 取舍与注意事项

1. **同家族自评风险**：默认设计是 B/C 跨模型家族。全 Claude 后靠三件事补：
   跨档位评审（Opus 评 Fable 的产物）、确定性级联（build/test 不看模型脸色）、
   新鲜上下文 + Judge 输出契约。
2. **档位倒挂**：B（Fable 5）高于 C（Opus 4.8）。可接受——C 评的是产物和
   证据，不与 B 比推理；不建议 C 同上 Fable 5（自评风险 + 每轮评审成本翻倍）。
3. **种群分支无升级档**：B 已是最强模型，候选差异化靠方案种子 + effort max。
4. **Fable 5 特性**：
   - 成本 $10/$50 /MTok（约为 Sonnet 5 的 3 倍）——Tier 1 token 优化在此
     配置下回报放大
   - 可能返回 `stop_reason: refusal`（安全分类器）：O 将该 B 轮按 crash
     处理，重派时降级 `claude-opus-4-8`
   - dispatch_B 保持"目标 + 约束"风格，避免步骤清单式指令（过度处方化会
     降低 Fable 5 输出质量）
   - 组织需 ≥30 天数据保留（ZDR 组织的 Fable 5 请求一律 400）
