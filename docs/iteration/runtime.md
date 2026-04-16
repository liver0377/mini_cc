# Harness 运行期迭代记录

本文档描述 Harness 在运行期间会持久化哪些迭代记录，以及这些记录如何支持长时运行、自恢复和审计。

## 产物位置

每个 run 都会在以下目录下生成独立记录：

```text
.mini_cc/runs/<run_id>/
```

当前会持久化以下文件：

- `state.json`：最新 `RunState`
- `events.jsonl`：run 级事件时间线
- `summary.md`：最新 step 摘要
- `journal.md`：面向人的逐步运行日志（每步追加）
- `Documentation.md`：Run 终态后的结构化总结文档（共享记忆 + 审计日志 + 质量判断）
- `iteration_snapshots.jsonl`：每个 step 的事实快照
- `iteration_reviews.jsonl`：每个 step 的结构化复盘
- `artifacts/`：step 产出的文本工件
- `checkpoints/`：按 step 保存的状态检查点

## 记录语义

### 1. Snapshot

`iteration_snapshots.jsonl` 只记录事实，不做判断：

- step 类型
- 是否成功
- 摘要
- 错误
- 是否有进展
- 测试通过/失败数
- 命令和 artifact 路径

### 2. Review

`iteration_reviews.jsonl` 记录系统对本轮的结论：

- `outcome`: `improved` / `stalled` / `regressed` / `blocked`
- `root_cause`
- `next_constraints`
- `recommended_step_kind`
- 结构化评分

### 3. Journal

`journal.md` 用于人工排查，按 step 追加：

- outcome
- success
- summary
- root cause
- 下一轮约束
- 自动生成的后续 step
- 相关命令

### 4. Documentation

`Documentation.md` 在 Run 到达终态时一次性生成，同时服务三个角色：

- **共享记忆**：后续 Run 的 system prompt 自动注入最近 Documentation.md 的"经验教训"段落
- **审计日志**：包含完整的 Step 时间线、决策原因、Agent 活动、资源消耗
- **质量判断**：包含迭代评分趋势、最终质量评估、未解决问题清单

详见 [harness/design.md#十四run-documentation-设计](../harness/design.md)。

## 自动迭代行为

当前 Harness 已实现以下规则：

- `EDIT_CODE` 成功后，如没有待执行验证步骤，则自动插入 `RUN_TESTS`
- `RUN_TESTS` 失败后，会自动插入 `INSPECT_FAILURES`
- 复盘结论会生成 `next_constraints`，供后续 step 和人工排查参考
- 所有复盘结果都会被持久化，便于 `resume()` 后继续沿用上下文
- TUI `RunScreen` 会展示 review 和 journal tail，并可直接恢复选中的 run
- 主 agent prompt 会注入最近 run 的 review/journal；harness step 会优先注入当前 run 的记录
- 子 agent prompt 也会从项目根目录读取最近 run 记录，避免与主 run 脱节
- Run 终态后自动生成 `Documentation.md`，其中"经验教训"段落注入后续 Run 的 system prompt

## 使用建议

- 长时运行优先通过 `RunHarness.resume(run_id)` 续跑，而不是重新起新 run
- 将项目默认测试命令写入 `RunState.metadata["test_command"]`，可提升自动验证质量
- 排查卡顿时优先查看 `journal.md`，需要程序化分析时读取 `iteration_reviews.jsonl`
- 判断 Run 质量时查看 `Documentation.md`，无需阅读 JSONL 文件
- 跨 Run 的经验传递通过 `Documentation.md` 的"经验教训"段落自动完成
