# Harness 设计（MVP）

## 一、目标

本文档定义 Mini Claude Code 的最小可落地 Harness 设计，用于把当前“单轮 Query Engine”扩展为“可持续运行约 1 小时的 code agent”。

Harness 不是新的模型能力，而是位于 Query Engine 之上的**运行时控制层**。它负责把目标、计划、预算、重试、检查点、验证、恢复和停止条件组织成一个稳定的长周期执行系统。

**MVP 目标：**

- 围绕一个高层目标持续运行 30 到 60 分钟
- 将任务拆分为有限个可评估的 step，逐步推进
- 在中断、错误或卡住时具备恢复和重规划能力
- 将运行过程持久化，便于恢复、审计和调试
- 与现有 QueryEngine / AgentManager / TaskService 兼容，而不是推倒重来

**非目标：**

- 不在 MVP 中实现完全自主的任务分解器
- 不在 MVP 中实现复杂的多层级 Agent 调度
- 不在 MVP 中引入分布式执行或远程沙箱
- 不在 MVP 中替换当前 TUI，只增量补充 run 视图能力

---

## 二、为什么需要 Harness

当前仓库已经具备以下能力：

- QueryEngine：完成单轮流式推理、工具执行、上下文压缩、子 Agent 结果回流
- ToolExecutor：管理工具并发/串行执行
- AgentManager：启动只读或写子 Agent
- TaskService：以本地文件形式维护任务记录
- Compression / Memory：降低长对话带来的上下文压力

这些模块足以支持“一次对话中的多轮工具调用”，但还不足以支持“一次目标驱动的长时间运行”。缺失的不是工具，而是**控制平面**：

- 没有 run 级生命周期
- 没有 deadline、retry、budget 等全局策略
- 没有 step 边界和进展判定
- 没有 checkpoint / resume 机制
- 没有 stuck detection 和统一的失败分类

因此需要在 QueryEngine 之上增加 Harness 层。

---

## 三、设计原则

### 1. Query Engine 继续做“单轮执行器”

Harness 不替代 QueryEngine。  
QueryEngine 仍然负责：

- 接收一组 messages
- 调用 LLM
- 处理工具调用
- 产出流式事件

Harness 只负责决定：

- 当前 run 的目标是什么
- 下一步该做什么
- 每一步允许花多少时间和预算
- 某一步失败后是重试、换路、还是停止

### 2. 以 Step 为边界，而不是让模型自由跑一小时

每次只让模型处理一个明确 step。  
一个 step 应该具备：

- 明确目标
- 明确输入
- 明确输出
- 明确预算
- 明确完成条件

这是长期运行稳定性的核心。

### 3. 状态可恢复

一小时运行过程中的任何阶段都可能被以下情况打断：

- 用户退出
- TUI 关闭
- 进程崩溃
- API 错误
- 工具异常

因此 Harness 必须把 run 视为持久化对象，而不是仅存在于内存中的协程。

### 4. 先规则化，再逐步智能化

MVP 优先使用规则和有限状态机来控制运行，不把“是否继续、是否重试、是否卡住”全部交给 LLM 判断。  
后续可以逐步引入 LLM-based judge，但不作为第一阶段前提。

---

## 四、总体架构

```
用户目标
   │
   ▼
RunHarness.run(goal)
   │
   ▼
SupervisorLoop
   │
   ├── 读取 RunState
   ├── 选择下一个 Step
   ├── 调用 StepRunner 执行
   ├── 调用 PolicyEngine 判定预算 / 重试 / 超时
   ├── 调用 Judge 判定 progressing / stalled / blocked
   └── 调用 CheckpointStore 持久化状态
   │
   ▼
StepRunner
   │
   ├── 对于 agent step：调用 QueryEngine
   ├── 对于 test step：调用 bash / pytest / mypy / ruff
   ├── 对于 inspect step：读取日志 / 测试结果 / diff
   └── 返回 StepResult
   │
   ▼
RunState 更新
   │
   ▼
继续下一轮 / 停止 / 等待人工
```

### 与现有模块的关系

| 模块 | 现状 | Harness 中的角色 |
|------|------|------------------|
| QueryEngine | 已存在 | 单 step 内的 agent 执行器 |
| AgentManager | 已存在 | 只读/写子 Agent 调度器 |
| TaskService | 已存在 | 子 Agent / 任务记录，可选集成 |
| Compression | 已存在 | step 内上下文管理 |
| Memory | 已存在 | 长运行时的辅助记忆来源 |
| TUI | 已存在 | 展示 run 状态和 timeline |

---

## 五、模块划分（MVP）

建议新增目录：

```text
src/mini_cc/harness/
├── __init__.py
├── runner.py
├── models.py
├── supervisor.py
├── step_runner.py
├── policy.py
├── judge.py
├── checkpoint.py
└── events.py
```

### 1. runner.py

对外入口，负责：

- 创建新的 run
- 加载已有 run 并恢复
- 启动 SupervisorLoop
- 暴露 `run()` / `resume()` / `cancel()` 接口

### 2. models.py

定义核心持久化模型：

- `RunState`
- `RunStatus`
- `Step`
- `StepKind`
- `StepStatus`
- `RunBudget`
- `RetryPolicy`
- `StepResult`
- `RunSummary`

### 3. supervisor.py

Harness 的主循环，负责：

- 检查是否超时 / 超预算
- 选择待执行 step
- 决定是否重规划
- 判断是否需要 checkpoint
- 更新 run 状态

### 4. step_runner.py

执行一个原子 step。  
它不维护全局状态，只接收 step 和上下文，然后返回结果。

### 5. policy.py

统一管理运行规则：

- 总时间预算
- 单 step 超时
- 同类错误最大重试次数
- 工具调用预算
- 子 Agent 数量上限

### 6. judge.py

负责判断 run 当前健康度：

- `progressing`
- `stalled`
- `blocked`
- `regressing`

MVP 可先用规则实现，不依赖额外 LLM。

### 7. checkpoint.py

负责：

- 保存 `RunState`
- 记录事件流
- 保存 artifacts
- 从磁盘恢复 run

### 8. events.py

定义 run 级事件，而不是复用 UI 事件：

- `RunStarted`
- `StepStarted`
- `StepCompleted`
- `PolicyTriggered`
- `CheckpointSaved`
- `RunCompleted`
- `RunFailed`

---

## 六、RunState 设计

RunState 是 Harness 的核心状态对象，必须可序列化到磁盘。

```text
RunState
├── run_id: str
├── goal: str
├── status: RunStatus
├── phase: str
├── created_at: str
├── started_at: str | None
├── deadline_at: str | None
├── updated_at: str
├── budget: RunBudget
├── retry_policy: RetryPolicy
├── steps: list[Step]
├── current_step_id: str | None
├── completed_step_ids: list[str]
├── failed_step_ids: list[str]
├── artifacts: dict[str, str]
├── latest_summary: str
├── latest_query_state_path: str | None
├── failure_count: int
├── consecutive_no_progress_count: int
└── metadata: dict[str, Any]
```

### RunStatus

建议的状态：

- `created`
- `planning`
- `running`
- `verifying`
- `blocked`
- `waiting_human`
- `completed`
- `failed`
- `cancelled`
- `timed_out`

### Step

每个 Step 必须是“有限、可评估、可重试”的原子工作单元。

建议字段：

```text
Step
├── id: str
├── kind: StepKind
├── title: str
├── goal: str
├── inputs: dict[str, Any]
├── expected_output: str
├── status: StepStatus
├── retry_count: int
├── budget_seconds: int
├── depends_on: list[str]
├── artifacts: dict[str, str]
├── evaluation: str
└── error: str | None
```

### StepKind

MVP 建议只支持以下几类：

- `analyze_repo`
- `make_plan`
- `edit_code`
- `run_tests`
- `inspect_failures`
- `spawn_readonly_agent`
- `summarize_progress`
- `checkpoint`
- `finalize`

不要一开始把 step 类型做得太多。

---

## 七、SupervisorLoop 设计

SupervisorLoop 是 run 级控制循环，负责把 RunState 持续推进到终态。

### 伪流程

```
load or create RunState

while run not terminal:
    1. 检查 deadline / budget / cancel flag
    2. 选择下一个 step
    3. 标记 step in_progress
    4. 执行 step
    5. 写入 step result
    6. 调用 judge 判定是否有进展
    7. 调用 policy 决定 retry / replan / stop
    8. 保存 checkpoint

return final state
```

### Step 选择策略（MVP）

MVP 不需要复杂调度器，可用简单规则：

1. 优先执行 `pending` 且依赖满足的 step
2. 若无待执行 step 且尚未完成，插入 `summarize_progress`
3. 若最近测试失败，则插入 `inspect_failures`
4. 若连续无进展次数过高，则插入 `make_plan`
5. 若达到终态条件，则插入 `finalize`

### 终止条件

任一条件满足即可终止：

- 明确完成目标
- 超过总时长预算
- 达到不可恢复失败上限
- 用户取消
- 进入 `waiting_human` 且用户未继续

---

## 八、StepRunner 设计

StepRunner 负责执行一个 step，并将外部世界的结果转成统一的 `StepResult`。

### 核心原则

- 不直接负责全局生命周期
- 不直接修改其他 step
- 不判断 run 是否结束
- 只处理“这个 step 怎么执行，结果是什么”

### Step 执行方式

| StepKind | 执行方式 |
|----------|----------|
| analyze_repo | 调用 QueryEngine，限制为只读分析 |
| make_plan | 调用 QueryEngine，生成或修正后续 step 建议 |
| edit_code | 调用 QueryEngine，允许写工具 |
| run_tests | 直接调用本地测试命令 |
| inspect_failures | 读取失败日志、测试输出、diff，再做总结 |
| spawn_readonly_agent | 通过 AgentManager 启动只读子 Agent |
| summarize_progress | 汇总最近 step、测试结果、diff |
| checkpoint | 强制保存状态 |
| finalize | 生成最终摘要和结果说明 |

### StepResult

建议字段：

- `success: bool`
- `summary: str`
- `artifacts: dict[str, str]`
- `next_step_hints: list[str]`
- `retryable: bool`
- `error: str | None`
- `progress_made: bool`

MVP 不要求所有 step 都返回结构化复杂结果，但必须统一到一个模型中。

---

## 九、PolicyEngine 设计

PolicyEngine 负责把“运行规则”集中化，而不是散落在各层 `if/else` 中。

### MVP 规则

#### 1. 时间预算

- 每个 run 默认上限：3600 秒
- 每个 step 默认上限：120 到 300 秒
- 超时后将 step 标记为失败，并进入 retry 或 replan

#### 2. 重试预算

- 同一 step 最大重试次数：2
- 同一类型错误最大连续出现次数：3
- 连续无进展次数达到阈值后，强制进入 `make_plan`

#### 3. 工具预算

- 单 run 的测试命令执行次数上限
- 单 run 的 bash 执行次数上限
- 单 run 的子 Agent 并发数上限

#### 4. 写入预算

- 单 run 最多修改文件数
- 单文件最大自动修改轮数

这些阈值初期不必非常精确，但必须存在。

---

## 十、Judge 设计

Judge 用于回答一个关键问题：**系统是否在推进目标？**

### Judge 输出

- `progressing`
- `stalled`
- `blocked`
- `regressing`

### MVP 规则信号

可以从以下信号综合判断：

- 最近一次测试是否比之前通过更多
- 最近一次代码修改是否产生有效 diff
- 最近若干 step 是否只是重复读取 / 重试而无新结果
- 当前错误是否与前几轮完全相同
- 是否出现“无新 artifact”的连续循环

### 规则示例

| 条件 | 结论 |
|------|------|
| 测试通过数增加 | progressing |
| 新增有效代码变更且无立即回滚 | progressing |
| 连续 3 个 step 无新 artifact | stalled |
| 同一错误连续出现 3 次 | blocked |
| 测试从通过变为失败更多 | regressing |

MVP 先基于规则实现；后续可以增加 LLM-based judge 作为辅助，而不是替代规则。

---

## 十一、Checkpoint 与持久化设计

### 目录结构建议

```text
.mini_cc/
└── runs/
    └── <run_id>/
        ├── state.json
        ├── events.jsonl
        ├── summary.md
        ├── artifacts/
        │   ├── latest_tests.txt
        │   ├── latest_diff.txt
        │   └── ...
        └── checkpoints/
            ├── step-0003.json
            └── step-0007.json
```

### 保存时机

MVP 建议至少在以下时机保存：

- run 创建后
- 每个 step 开始前
- 每个 step 完成后
- 触发 policy 决策后
- run 结束时

### 保存内容

- 当前 `RunState`
- 最近事件追加到 `events.jsonl`
- 关键 artifact 路径
- 最新摘要

### 恢复策略

恢复时：

1. 读取 `state.json`
2. 校验 run 是否已终态
3. 恢复 steps 和 budget 使用情况
4. 重新构建最小执行上下文
5. 从最近未完成 step 继续

MVP 不要求完整恢复子 Agent 的运行中状态；若恢复时有未完成子 Agent，可标记为失效并重新调度。

---

## 十二、与现有代码的集成方案

### 1. QueryEngine

保留不动，作为单 step 的 agent 执行器。  
Harness 不直接修改 QueryEngine 内部状态机。

### 2. AgentManager

仅在需要并行只读探索时由 StepRunner 调用。  
MVP 中建议限制：

- 最多并发 2 个只读子 Agent
- 不把主路径阻塞任务交给子 Agent

### 3. TaskService

MVP 中不强制让 Harness 依赖 TaskService 作为 run 主存储。  
建议：

- `TaskService` 继续负责子 Agent / task 记录
- `CheckpointStore` 单独负责 run 持久化

这样可以避免把“任务账本”和“运行时状态机”混成一个对象。

### 4. TUI

MVP 先不引入复杂新页面，只需要逐步补充：

- 当前 `Run ID`
- 当前 `Phase`
- 当前 `Step`
- 已运行时长 / 截止时间
- 最近一次 policy 决策

后续再加入独立的 run timeline 面板。

---

## 十三、MVP 落地范围

### 第一阶段必须实现

- `RunState` / `Step` / `RunBudget` 模型
- `RunHarness.run()` / `resume()`
- `SupervisorLoop`
- `StepRunner`
- `PolicyEngine`
- `CheckpointStore`
- 基础 run 事件流

### 第一阶段可以复用现有能力

- QueryEngine
- Compression
- Memory
- ToolExecutor
- AgentManager
- SnapshotService

### 第一阶段暂不实现

- 多层嵌套 sub-agent harness
- 自动任务树依赖图优化
- 复杂的 run UI 编排
- 跨会话运行状态合并

---

## 十四、建议的最小实现顺序

### Step 1：定义模型和磁盘存储

先实现：

- `RunState`
- `Step`
- `RunBudget`
- `CheckpointStore.save/load`

没有持久化，就没有真正的 harness。

### Step 2：实现 SupervisorLoop

先用最简单的队列执行：

- `pending -> in_progress -> succeeded/failed`
- 支持 deadline、retry、terminal state

### Step 3：实现 StepRunner

优先支持 3 类 step：

- `make_plan`
- `edit_code`
- `run_tests`

这样已经能形成最小闭环。

### Step 4：实现 Judge 与 Policy

加入：

- 连续无进展检测
- 同类错误重试限制
- 超时终止

### Step 5：接入 TUI

把 run 状态展示出来，而不是只显示单轮聊天内容。

---

## 十五、一个典型运行示例

目标：修复某个 failing test，并确认相关测试通过。

```
Run created
  └── Step 1: analyze_repo
  └── Step 2: make_plan
  └── Step 3: edit_code
  └── Step 4: run_tests
         ├── 若通过：finalize
         └── 若失败：inspect_failures -> edit_code -> run_tests
```

如果出现如下情况：

- 连续 3 次测试失败且错误相同

则 PolicyEngine 可触发：

- 插入 `make_plan`
- 或转为 `blocked`
- 或请求人工介入

这就是 Harness 相比“纯 agent loop”的关键差异：  
它知道何时继续，何时换路，何时停。

---

## 十六、成功标准

若 MVP 达到以下标准，即可认为 Harness 初步成立：

- 可创建一个 run 并持续执行多个 step
- 可围绕一个 repo 任务持续运行 30 到 60 分钟
- 运行中支持编辑、测试、复盘、重试
- 进程中断后可从磁盘恢复
- 遇到重复失败时不会无限死循环
- 用户能看到 run 当前在做什么，以及为何继续或停止

---

## 十七、总结

Harness 的本质不是“让模型跑更久”，而是把长期运行拆解为：

- 有边界的 step
- 有预算的执行
- 有证据的进展判断
- 有检查点的恢复机制
- 有规则的停止条件

对 Mini Claude Code 而言，最合理的路径不是重写 QueryEngine，而是在其之上增加一个清晰、持久化、可控的运行时控制层。

MVP 只要先把 `RunState + SupervisorLoop + StepRunner + Policy + Checkpoint` 建起来，就已经具备向“一小时持续运行 code agent”演进的基础。
