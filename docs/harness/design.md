# Harness 设计

## 一、目标

Mini Claude Code 的 Harness 是位于 QueryEngine 之上的**运行时控制层**。它把"单轮对话式 Agent 循环"扩展为"围绕目标持续运行的结构化执行系统"，负责将目标、计划、预算、重试、检查点、验证、恢复和停止条件组织成一个稳定的长周期执行闭环。

### 核心目标

- 围绕一个高层目标持续运行 30 到 60 分钟
- 将任务拆分为有限个可评估的 Step，逐步推进
- 在中断、错误或卡住时具备恢复和重规划能力
- 将运行过程持久化，便于恢复、审计和调试
- 统一感知并管控 LLM 自主派生的子 Agent，防止资源失控

### 非目标

- 不实现完全自主的任务分解器——Step 类型和编排由 SupervisorLoop 规则驱动
- 不实现复杂的多层级 Agent 调度——子 Agent 不可递归
- 不引入分布式执行或远程沙箱
- 不替换现有 TUI，只增量补充 Run 视图能力

---

## 二、为什么需要 Harness

系统在 Harness 之下已具备：

| 能力 | 模块 | 作用 |
|------|------|------|
| 单轮推理 | QueryEngine | 流式 LLM 调用、工具执行、上下文压缩 |
| 子 Agent | AgentManager / SubAgent | 只读探索或写入修改，异步或同步执行 |
| 任务记录 | TaskService | 本地文件持久化的任务追踪 |
| 并发控制 | StreamingToolExecutor | 安全工具并发、不安全工具串行 |
| 文件快照 | SnapshotService | 写 Agent 的文件级备份/回滚 |
| 长期记忆 | Memory / Compression | 降低长对话的上下文压力 |

这些模块足以支持"一次对话中的多轮工具调用"，但缺少一个**控制平面**：

- 没有 Run 级生命周期（创建、运行、完成、恢复）
- 没有 deadline、retry、budget 等全局策略
- 没有 Step 边界和进展判定
- 没有 checkpoint / resume 机制
- 没有 stuck detection 和统一的失败分类
- **没有对 LLM 自主派生子 Agent 的感知和约束**——LLM 在 Query-backed Step 内部可以通过 AgentTool 自主创建子 Agent，但 Harness 对此一无所知

因此需要在 QueryEngine 之上增加 Harness 层。

---

## 三、设计原则

### 1. QueryEngine 继续做"单轮执行器"

Harness 不替代 QueryEngine。QueryEngine 仍然负责接收 messages、调用 LLM、处理工具调用、产出流式事件。Harness 只负责决定"下一步做什么、花多少预算、失败后怎么办"。

### 2. 以 Step 为边界

每次只让模型处理一个明确的 Step。一个 Step 必须具备明确的目标、输入、输出、预算和完成条件。这是长期运行稳定性的核心。

### 3. 状态可恢复

一小时运行过程中的任何阶段都可能被中断（用户退出、进程崩溃、API 错误）。因此 Harness 把 Run 视为持久化对象，而非仅存在于内存中的协程。

### 4. 先规则化，再逐步智能化

优先使用规则和有限状态机控制运行，不把"是否继续、是否重试、是否卡住"全部交给 LLM。后续可逐步引入 LLM-based judge，但不作为前提。

### 5. 统一感知两条 Agent 编排路径

系统存在两条 Agent 创建路径——Harness 结构化派生（SPAWN_READONLY_AGENT Step）和 LLM 自主派生（AgentTool）。Harness 必须能感知两条路径创建的所有 Agent，统一管控其数量、预算和归属。

---

## 四、两条 Agent 编排路径

这是理解 Harness 设计的关键前提：**Agent 的创建有两条独立路径，Harness 必须将它们统一管控**。

### 路径 A：Harness 结构化派生

SupervisorLoop 在执行 `SPAWN_READONLY_AGENT` 类型的 Step 时，通过 StepRunner 直接调用 AgentManager 创建子 Agent。这些 Agent 的创建是 Harness 计划的一部分，Step 的 goal 定义了 Agent 的任务。

### 路径 B：LLM 自主派生

在执行 `MAKE_PLAN`、`EDIT_CODE`、`SUMMARIZE_PROGRESS`、`FINALIZE` 等 Query-backed Step 时，StepRunner 把控制权交给 QueryEngine。LLM 在推理过程中可以自主调用 AgentTool 创建子 Agent——可能是单个 readonly Agent、单个 write Agent，也可能通过 `dispatch_plan_json` 批量创建多个 readonly Agent。Harness 对这些 Agent 的存在、数量和行为**没有直接控制**。

### 两条路径的嵌套关系

```
SupervisorLoop.run()
  │
  ├─ Step: MAKE_PLAN ──→ StepRunner ──→ QueryEngine.submit_message()
  │                                          │
  │                                          └─ LLM 自主调用 AgentTool ──→ AgentManager.create_agent()  [路径 B]
  │
  ├─ Step: SPAWN_READONLY_AGENT ──→ StepRunner ──→ AgentManager.create_agent()  [路径 A]
  │
  └─ Step: EDIT_CODE ──→ StepRunner ──→ QueryEngine.submit_message()
                                          │
                                          └─ LLM 自主调用 AgentTool ──→ AgentManager.create_agent()  [路径 B]
```

两条路径共享同一个 `AgentManager` 实例，因此 Scope 冲突检查对两条路径都有效。但 Harness 的 PolicyEngine 和 RunJudge 只看 Step 级别的结果，无法感知路径 B 创建的 Agent。

### 统一感知机制

为解决此问题，引入 `AgentEventBus`——一个进程内的事件总线，所有 Agent 生命周期事件（创建 / 完成 / 取消）统一发布到此 bus。SupervisorLoop 在每个 Step 执行后消费积压事件，更新 RunState 的 Agent 追踪数据。

详见第八节"Agent 感知与预算管控"。

---

## 五、总体架构

```
用户目标
   │
   ▼
RunHarness.run(goal)
   │
   ▼
SupervisorLoop
   │
   ├── 读取 / 恢复 RunState
   ├── 消费 AgentEventBus → 更新 spawned_agents
   ├── 选择下一个 ready Step
   ├── 注入 AgentBudget 到 EngineContext
   ├── 调用 StepRunner 执行
   │     │
   │     ├── Query-backed Step → QueryEngine → (LLM 可能调用 AgentTool → AgentBudget 扣减)
   │     ├── Bash Step → Bash 工具直接执行
   │     └── SPAWN_READONLY_AGENT → AgentManager
   │
   ├── IterationOptimizer 采集 snapshot → review → 生成后续 Step
   ├── RunJudge 评估健康度（progressing / stalled / blocked / regressing）
   ├── PolicyEngine 判定策略（continue / retry / replan / block / fail / complete）
   └── CheckpointStore 持久化状态 + 事件 + journal
   │
   ▼
继续下一轮 / 停止 / 恢复
```

### 模块清单

```text
src/mini_cc/harness/
├── __init__.py          # 公共导出
├── runner.py            # RunHarness，顶层入口
├── models.py            # RunState, Step, RunBudget, AgentTrace, AgentBudget 等
├── supervisor.py        # SupervisorLoop，主循环
├── step_runner.py       # StepRunner，Step 派发执行
├── policy.py            # PolicyEngine，策略决策
├── judge.py             # RunJudge，健康度评估
├── iteration.py         # IterationOptimizer，迭代评分/审查/约束
├── checkpoint.py        # CheckpointStore，持久化
└── events.py            # HarnessEvent 定义

src/mini_cc/agent/
├── bus.py               # AgentEventBus，Agent 生命周期事件总线
├── manager.py           # AgentManager，子 Agent 创建/追踪/清理
├── sub_agent.py         # SubAgent，单个子 Agent 运行时
└── snapshot.py          # SnapshotService，文件快照备份
```

### 与现有模块的关系

| 模块 | Harness 中的角色 |
|------|------------------|
| QueryEngine | 单 Step 内的 Agent 执行器 |
| AgentManager | 子 Agent 调度器，同时服务两条路径 |
| AgentEventBus | 连接 AgentManager 与 SupervisorLoop 的感知桥梁 |
| TaskService | 子 Agent 任务记录 |
| StreamingToolExecutor | 工具并发/串行执行 |
| SnapshotService | 写 Agent 的文件级备份/回滚 |
| Compression | Step 内上下文管理 |
| Memory | 长运行时的辅助记忆 |
| TUI | 展示 Run 状态和 timeline |

---

## 六、RunState 设计

RunState 是 Harness 的核心状态对象，必须可序列化到磁盘。

### 完整字段

```text
RunState
├── run_id: str                          # Run 唯一标识
├── goal: str                            # 用户目标
├── status: RunStatus                    # 当前 Run 状态
├── phase: str                           # 当前阶段描述
├── created_at: str                      # ISO 时间戳
├── started_at: str | None
├── deadline_at: str | None              # 运行截止时间
├── updated_at: str
├── budget: RunBudget                    # 资源预算
├── retry_policy: RetryPolicy            # 重试策略
├── steps: list[Step]                    # 所有 Step
├── current_step_id: str | None          # 当前执行中的 Step
├── completed_step_ids: list[str]        # 已完成的 Step
├── failed_step_ids: list[str]           # 已失败的 Step
├── artifacts: dict[str, str]            # Run 级 artifact 路径
├── latest_summary: str                  # 最近一次摘要
├── latest_query_state: QueryState       # 最近一次 LLM 对话状态
├── failure_count: int                   # 连续失败计数
├── consecutive_no_progress_count: int   # 连续无进展计数
├── test_run_count: int                  # 已执行测试次数
├── bash_command_count: int              # 已执行 Bash 命令次数
├── spawned_agents: list[AgentTrace]     # 所有被创建的子 Agent 追踪记录
├── agent_budget: AgentBudget | None     # Agent 创建预算（注入到 EngineContext）
├── replan_count: int                    # 累计 REPLAN 次数
└── metadata: dict[str, str]
```

### RunStatus 状态机

```text
CREATED ──→ RUNNING ──→ VERIFYING ──→ RUNNING (循环)
                         │
                         ├──→ COMPLETED
                         ├──→ FAILED
                         ├──→ BLOCKED
                         ├──→ CANCELLED
                         └──→ TIMED_OUT
```

| 状态 | 含义 |
|------|------|
| CREATED | Run 已创建，尚未开始 |
| RUNNING | 正在执行 Step |
| VERIFYING | 当前 Step 为 RUN_TESTS，处于验证阶段 |
| BLOCKED | 连续失败，无法继续 |
| COMPLETED | 所有 Step 完成，目标达成 |
| FAILED | 不可恢复的失败 |
| CANCELLED | 用户手动取消 |
| TIMED_OUT | 超过总时间预算 |

### AgentTrace：子 Agent 追踪记录

记录 Run 内每个被创建的子 Agent（无论来自哪条路径）：

```text
AgentTrace
├── agent_id: str              # Agent 唯一标识
├── source_step_id: str | None # 创建该 Agent 的 Step（路径 B 可能为 None）
├── readonly: bool             # 是否只读
├── scope_paths: list[str]     # 写入范围
├── created_at: str            # 创建时间
├── completed_at: str | None   # 完成时间
└── success: bool | None       # 执行结果
```

`active_agent_count` 属性统计 `completed_at is None` 的记录数量，供 PolicyEngine 使用。

### AgentBudget：Agent 创建预算

```text
AgentBudget
├── max_readonly: int          # 本轮最大只读 Agent 数（默认 5）
├── max_write: int             # 本轮最大写 Agent 数（默认 1）
├── remaining_readonly: int    # 剩余只读配额
└── remaining_write: int       # 剩余写配额
```

AgentBudget 在每个 Step 执行前由 SupervisorLoop 根据当前 RunState 计算并注入到 EngineContext。AgentTool 在创建 Agent 前检查并扣减配额，耗尽时返回错误信息给 LLM。

### Step

每个 Step 是"有限、可评估、可重试"的原子工作单元：

```text
Step
├── id: str                    # 如 "step-0001"
├── kind: StepKind             # Step 类型
├── title: str                 # 简短标题
├── goal: str                  # 详细目标（包含约束注入后的完整 prompt）
├── inputs: dict               # 输入参数
├── expected_output: str       # 预期输出描述
├── status: StepStatus         # 当前状态
├── retry_count: int           # 已重试次数
├── budget_seconds: int | None # 单步超时
├── depends_on: list[str]      # 前置 Step ID
├── artifacts: dict[str, str]  # 产出 artifact
├── evaluation: str            # 评估结果
├── summary: str               # 执行摘要
└── error: str | None          # 错误信息
```

### StepKind

| 类型 | 执行方式 | 说明 |
|------|----------|------|
| ANALYZE_REPO | Query-backed | 仓库只读分析 |
| MAKE_PLAN | Query-backed | 生成或修正后续 Step |
| EDIT_CODE | Query-backed | 允许写工具的代码修改 |
| RUN_TESTS | Bash-backed | 直接调用本地测试命令 |
| INSPECT_FAILURES | Bash-backed | 读取失败日志、测试输出 |
| SPAWN_READONLY_AGENT | Agent-backed | 通过 AgentManager 启动只读子 Agent |
| SUMMARIZE_PROGRESS | Query-backed | 汇总最近进展 |
| CHECKPOINT | No-op | 强制保存状态 |
| FINALIZE | Query-backed | 生成最终摘要 |

### StepStatus 生命周期

```text
PENDING → IN_PROGRESS → SUCCEEDED
                      → FAILED_RETRYABLE → PENDING (重试)
                      → FAILED_TERMINAL
                      → SKIPPED
```

### RunBudget 与 RetryPolicy

```text
RunBudget
├── max_runtime_seconds: 3600    # 总运行时限 1 小时
├── max_step_seconds: 300        # 单步时限 5 分钟
├── max_test_runs: 20            # 最大测试执行次数
├── max_bash_commands: 50        # 最大 Bash 命令次数
└── max_active_agents: 2         # 最大并发活跃 Agent

RetryPolicy
├── max_step_retries: 2                # 单步最大重试次数
├── max_consecutive_failures: 3        # 最大连续失败次数
└── max_consecutive_no_progress: 3     # 最大连续无进展次数
```

---

## 七、SupervisorLoop 设计

SupervisorLoop 是 Run 级控制循环，负责把 RunState 持续推进到终态。

### 主循环流程

```text
load or create RunState
│
while run not terminal:
│
├── 1. 检查运行限制
│     └── PolicyEngine.check_run_limits()
│         ├── deadline 超限 → TIME_OUT
│         ├── test_run_count 超限 → FAIL
│         ├── bash_command_count 超限 → FAIL
│         ├── active_agent_count 超限 → BLOCK
│         └── 通过 → 继续
│
├── 2. 选择下一个 ready Step
│     └── ready_steps() 中 depends_on 全部满足的第一个
│     └── 无 ready Step → COMPLETED
│
├── 3. 消费 Agent 生命周期事件
│     └── AgentEventBus.drain()
│     └── 更新 RunState.spawned_agents（新增 / 标记完成）
│
├── 4. 注入 AgentBudget
│     └── 根据 RunState 当前活跃 Agent 数计算剩余配额
│     └── 写入 EngineContext.agent_budget
│
├── 5. 执行 Step
│     └── StepRunner.run_step(step, run_state)
│     └── 设置 AgentManager.set_current_step(step.id)
│
├── 6. 迭代优化
│     └── IterationOptimizer.capture() → IterationSnapshot
│     └── IterationOptimizer.review() → IterationReview
│     └── 可能生成后续 Step（如 EDIT_CODE 后自动插入 RUN_TESTS）
│
├── 7. 健康评估
│     └── RunJudge.assess() → RunHealth
│
├── 8. 策略决策
│     └── PolicyEngine.evaluate_step() → PolicyDecision
│     └── REPLAN 决策时递增 replan_count
│
├── 9. 应用决策和约束
│     └── apply_step_decision()（更新 Step 和 Run 状态）
│     └── apply_constraints_to_steps()（约束注入后续 Step）
│
├── 10. 持久化
│      └── CheckpointStore 保存 state / events / snapshot / review / journal
│
└── continue
│
return final RunState
```

### Step 选择策略

按依赖拓扑排序取第一个 ready Step：

1. 优先执行 `pending` 且 `depends_on` 全部完成的 Step
2. 若无待执行 Step 且尚未完成，标记 Run 为 COMPLETED
3. 迭代优化器可能自动插入后续 Step（如 RUN_TESTS、INSPECT_FAILURES）

### 终止条件

任一条件满足即终止：

- 所有 Step 完成且 FINALIZE 成功 → COMPLETED
- 超过总时长预算 → TIMED_OUT
- 达到不可恢复失败上限 → BLOCKED
- 用户取消 → CANCELLED
- REPLAN 深度超过阈值（默认 3 次）且无实质进展 → FAILED

---

## 八、Agent 感知与预算管控

这是系统最关键的设计之一：Harness 必须感知并约束两条 Agent 编排路径创建的所有子 Agent。

### 问题：两条路径的感知盲区

| 维度 | 路径 A（Harness 派生） | 路径 B（LLM 自主派生） |
|------|------------------------|------------------------|
| 创建触发 | SupervisorLoop 执行 SPAWN_READONLY_AGENT Step | LLM 在 Query-backed Step 内调用 AgentTool |
| Harness 感知 | 完全可见——StepResult 包含 agent_id | **不可见**——Agent 创建发生在 QueryEngine 内部 |
| 预算控制 | 由 RunBudget.max_active_agents 约束 | **无约束**——LLM 可无限创建 Agent |
| 结果归属 | 归属于当前 Step | **归属模糊**——混入 QueryEngine 的对话流 |

### 解决方案：AgentEventBus + AgentBudget

#### AgentEventBus：统一感知层

一个进程内的事件总线，解耦 AgentManager 与 SupervisorLoop。

```text
AgentManager.create_agent()
  └── publish("created", agent_id, source_step_id, readonly, scope_paths)

SubAgent._finish()
  └── publish("completed" | "cancelled", agent_id, success)

SupervisorLoop（每个 Step 执行后）
  └── bus.drain()
  └── 更新 RunState.spawned_agents
```

AgentEventBus 是独立于现有 `completion_queue` 和 `agent_event_queue` 的新增通道。那两个 Queue 被 QueryEngine 内部消费，如果 SupervisorLoop 也 drain 它们会导致事件被"偷走"。AgentEventBus 专门服务于 Harness 层。

#### AgentBudget：预算钱包

AgentBudget 是一个可变配额对象，每个 Step 执行前由 SupervisorLoop 计算并注入 EngineContext。

**注入时机**：StepRunner 在执行 `_run_query_step()` 前，根据 `RunState.active_agent_count` 和 `RunBudget.max_active_agents` 计算剩余配额，写入 `EngineContext.agent_budget`。

**扣减时机**：AgentTool 在 `async_execute()` 中，创建 Agent 前检查并原子扣减 `remaining`。

**耗尽行为**：返回 `ToolResult(success=False)` 给 LLM，附带明确的预算耗尽提示，LLM 会看到这个错误并调整策略。

```text
SupervisorLoop                          StepRunner                     AgentTool
     │                                      │                              │
     │  agent_budget = compute()            │                              │
     │  engine_ctx.agent_budget = budget     │                              │
     │ ──────────────────────────────────→   │                              │
     │                                      │  LLM 调用 AgentTool          │
     │                                      │ ──────────────────────────→   │
     │                                      │                              │
     │                                      │           budget.remaining > 0?
     │                                      │           ├── Yes → 扣减，创建 Agent
     │                                      │           └── No → return error
     │                                      │                              │
     │                                      │  ←──── ToolResult ──────────  │
     │                                      │                              │
     │  读回 budget.remaining 到             │                              │
     │  StepResult.metadata                 │                              │
     │ ←──────────────────────────────────   │                              │
```

#### 预算计算规则

```text
AgentBudget 每步重新计算：
  remaining_readonly = max(0, max_readonly - active_readonly_agent_count)
  remaining_write = max(0, 1 - active_write_agent_count)
```

这意味着：
- 如果已有 2 个活跃 readonly Agent，新 Step 的 remaining_readonly = 0，LLM 无法再创建
- 如果已有 1 个活跃 write Agent，新 Step 的 remaining_write = 0，LLM 无法再创建新的 write Agent
- 写 Agent 采用**全局严格串行**策略；scope 检查仍保留，但仅作为额外保护，而不是并发写的许可条件
- 批量派工（dispatch_plan）一次性检查总数量

---

## 九、StepRunner 设计

StepRunner 负责执行单个 Step，将外部世界的结果转成统一的 StepResult。

### 核心原则

- 不直接负责全局生命周期
- 不直接修改其他 Step
- 不判断 Run 是否结束
- 只处理"这个 Step 怎么执行，结果是什么"
- 执行前后设置/清理 AgentManager 的 Step 上下文

### Step 执行方式

| StepKind | 执行方式 | 说明 |
|----------|----------|------|
| ANALYZE_REPO | QueryEngine (plan mode) | 仓库只读分析，注入 Plan 模式 system prompt |
| MAKE_PLAN | QueryEngine (plan mode) | 生成或修正后续 Step 建议 |
| EDIT_CODE | QueryEngine (build mode) | 允许写工具的代码修改，注入 Build 模式 system prompt |
| SUMMARIZE_PROGRESS | QueryEngine | 汇总最近进展 |
| FINALIZE | QueryEngine | 生成最终摘要和结果说明 |
| RUN_TESTS | Bash 工具 | 直接调用本地测试命令 |
| INSPECT_FAILURES | Bash 工具 | 读取失败日志 |
| SPAWN_READONLY_AGENT | AgentManager | 创建只读子 Agent 并后台启动 |
| CHECKPOINT | 无操作 | 强制保存状态 |

### Query-backed Step 的执行细节

Query-backed Step 的执行流程如下：

1. 从 Step.goal 构建 prompt
2. 设置 EngineContext 的 mode（build / plan）
3. 准备 QueryState：使用当前 system prompt 构建初始状态，或深拷贝已有 state 并刷新 system message
4. **注入 AgentBudget 到 EngineContext**
5. 设置 AgentManager.set_current_step(step.id)——让 AgentEventBus 知道后续创建的 Agent 归属哪个 Step
6. 调用 QueryEngine.submit_message()，逐事件 yield
7. 事件同时转发给 TUI（通过 query_event_sink 回调）
8. 收集文本内容和工具输出，构建 StepResult
9. 清理 AgentManager.clear_current_step()
10. 读回 AgentBudget 剩余配额到 StepResult.metadata

### StepResult

```text
StepResult
├── success: bool
├── summary: str                       # 执行摘要
├── artifacts: dict[str, str]           # 产出工件
├── next_steps: list[Step]              # 建议的后续 Step
├── retryable: bool                     # 是否可重试
├── error: str | None                   # 错误信息
├── progress_made: bool                 # 是否有实质进展
├── query_state: QueryState | None      # LLM 对话状态（供后续 Step 继承）
└── metadata: dict[str, str]            # 包含 agents_remaining_ro, agents_remaining_w
```

---

## 十、PolicyEngine 设计

PolicyEngine 负责把运行规则集中化，在两个关键时机做决策。

### 时机一：Step 执行前（check_run_limits）

在 SupervisorLoop 主循环顶部，执行任何 Step 之前检查：

| 检查项 | 条件 | 决策 |
|--------|------|------|
| 总时限 | 当前时间 > deadline_at | TIME_OUT |
| 测试预算 | test_run_count >= max_test_runs | FAIL |
| 命令预算 | bash_command_count >= max_bash_commands | FAIL |
| Agent 数量 | active_agent_count > max_active_agents * 2 | BLOCK |
| 写 Agent 串行性 | active_write_agent_count > 1 | BLOCK |

任一触发即返回 PolicyDecision，Run 直接进入终态。

### 时机二：Step 执行后（evaluate_step）

结合 StepResult、RunHealth 和迭代审查结果决策：

```text
Step 执行后的决策树：

FINALIZE 成功？
  └── Yes → COMPLETE

Step 成功？
  ├── Yes + STALLED + 非 MAKE_PLAN？
  │     └── REPLAN（插入 MAKE_PLAN Step）
  └── Yes → CONTINUE

Step 失败？
  ├── 重试次数 < max_step_retries 且 retryable？
  │     └── RETRY
  ├── RUN_TESTS / INSPECT_FAILURES 失败 且 非 BLOCKED？
  │     └── REPLAN
  ├── STALLED + 非 MAKE_PLAN？
  │     └── REPLAN（插入 MAKE_PLAN Step）
  ├── BLOCKED 或 连续失败 >= max_consecutive_failures？
  │     └── BLOCK
  └── 其他 → FAIL
```

### REPLAN 深度限制

每次 REPLAN 决策时递增 `RunState.replan_count`。当 `replan_count >= 3` 时，不再允许 REPLAN，直接 FAIL。

这是为了防止系统陷入 `MAKE_PLAN → EDIT → TEST → FAIL → REPLAN → MAKE_PLAN...` 的无限循环。唯一的止损阀之前只有 `max_runtime_seconds`，现在有了更明确的语义限制。

### PolicyDecision

```text
PolicyDecision
├── action: PolicyAction          # CONTINUE / RETRY / REPLAN / BLOCK / FAIL / COMPLETE / TIME_OUT
├── reason: str                    # 决策原因
├── insert_steps: list[Step]       # 需要自动插入的 Step
└── terminal_status: RunStatus     # 终态（仅终态决策有值）
```

---

## 十一、RunJudge 设计

RunJudge 回答一个关键问题：**系统是否在推进目标？**

### 输出

| 健康度 | 含义 |
|--------|------|
| PROGRESSING | 有明确进展信号 |
| STALLED | 连续多步无实质进展 |
| BLOCKED | 连续失败达到上限 |
| REGRESSING | 状态在恶化（如测试从通过变为失败） |

### 判定规则

| 条件 | 结论 |
|------|------|
| progress_made = True | PROGRESSING |
| 失败 且 failure_count >= max_consecutive_failures | BLOCKED |
| consecutive_no_progress_count >= max_consecutive_no_progress | STALLED |
| 失败 且无进展 | REGRESSING |
| 默认 | STALLED |

RunJudge 当前是纯规则实现，逻辑精简。未来可扩展为 LLM 辅助判断，但作为兜底的规则判定必须保留。

---

## 十二、迭代优化系统

迭代优化系统位于 SupervisorLoop 内部、StepRunner 和 PolicyEngine 之间，负责"每一步都比上一步做得更好"。

详见 [iteration/design.md](../iteration/design.md) 和 [iteration/runtime.md](../iteration/runtime.md)。

### 核心闭环

```text
Step 执行完成
   │
   ▼
采集事实数据（IterationSnapshot）
   │
   ▼
与上一步 Snapshot 对比 → 生成复盘结论（IterationReview）
   │
   ├── outcome: IMPROVED / STALLED / REGRESSED / BLOCKED
   ├── score: 多信号打分
   ├── root_cause: 根因分析
   ├── next_constraints: 下一轮约束
   └── recommended_step_kind: 建议的下一步类型
   │
   ▼
生成后续 Step + 约束注入
   │
   ▼
交给 PolicyEngine 做最终判定
```

### 自动 Step 生成规则

| 触发条件 | 自动插入的 Step |
|----------|----------------|
| EDIT_CODE 成功 且 无待执行 RUN_TESTS | RUN_TESTS |
| RUN_TESTS 失败 | INSPECT_FAILURES |

### 评分信号

评分由六个独立信号加权求和：

| 信号 | 说明 | 得分范围 |
|------|------|----------|
| success_signal | Step 是否成功 | +3 / 0 |
| progress_signal | 是否有实质进展 | +2 / 0 |
| verification_signal | 测试通过数 - 失败数 - 错误数，成功测试轮额外 +2 | -N ~ +N |
| artifact_signal | 是否产生了工件 | +1 / 0 |
| comparison_signal | 与上一步对比的改善/恶化 | -1 ~ +2 |
| penalty | 是否有错误 | 0 / -2 |

### 结果分类

| 条件 | 分类 |
|------|------|
| 相同错误重复出现 | BLOCKED |
| 失败 且无进展 | REGRESSED |
| 成功 且无进展 | STALLED |
| 首步 | IMPROVED 或 STALLED |
| score > 0 | IMPROVED |
| score < 0 | REGRESSED |
| score = 0 | STALLED |

### 约束注入

review 产出的 `next_constraints` 会被注入后续 Step 的 goal 中，形成"复盘 → 约束 → 调整"的闭环。例如：

- "将失败测试数降到 3 以下"
- "保持使用命令 X 作为基线"
- "先处理错误 Y，再考虑其他修改"

---

## 十三、Checkpoint 与持久化

### 目录结构

```text
.mini_cc/
└── runs/
    └── <run_id>/
        ├── state.json                     # 最新 RunState
        ├── events.jsonl                   # HarnessEvent 时间线
        ├── summary.md                     # 最新 Step 摘要
        ├── journal.md                     # 面向人的逐步运行日志（每步追加）
        ├── Documentation.md               # Run 终态后的结构化总结文档
        ├── iteration_snapshots.jsonl      # 每个 Step 的事实快照
        ├── iteration_reviews.jsonl        # 每个 Step 的结构化复盘
        ├── artifacts/                     # Step 产出工件
        │   ├── step-0003_step-0003.txt
        │   └── ...
        └── checkpoints/                   # 按 Step 保存的状态检查点
            ├── step-0003.json
            └── step-0007.json
```

### 保存时机

| 时机 | 保存内容 |
|------|----------|
| Run 创建后 | RunState |
| 每个 Step 开始前 | RunState |
| 每个 Step 完成后 | RunState + HarnessEvent + IterationSnapshot + IterationReview + journal |
| 触发 Policy 决策后 | RunState |
| Run 到达终态后 | RunState + 最终 HarnessEvent + **Documentation.md** |

### 恢复策略

1. 读取 `state.json`
2. 校验 Run 是否已终态；`COMPLETED / FAILED / BLOCKED / CANCELLED / TIMED_OUT` 都视为终态，不允许 resume
3. 扫描并恢复所有异常中断的 Step 状态，而不只依赖 `current_step_id`
4. 将所有未完成子 Agent 标记为失效
5. 在队列头部插入一个恢复专用 `Resume Replan` 的 `MAKE_PLAN` Step
6. 从恢复后的 Step 队列继续运行

恢复时不恢复子 Agent 的运行中状态；若有未完成子 Agent，会统一标记为失效：

- `completed_at` 写为恢复时刻
- `success=False`
- `termination_reason="invalidated_on_resume"`
- `invalidated_on_resume=True`

同时，所有 `IN_PROGRESS` 的 Step 都会被回退为 `PENDING`，并在 `error` 中写入恢复说明。恢复完成后发出结构化 `run_resumed` 事件，记录失效 Agent 数、恢复 Step 数以及恢复专用重规划决策。

### journal.md 与 Documentation.md 的分工

| 维度 | journal.md | Documentation.md |
|------|-----------|------------------|
| 生成时机 | 每步追加 | Run 终态时一次性生成 |
| 内容粒度 | 每个 Step 的简要日志（outcome、summary、constraints） | 完整的结构化总结 |
| 容错性 | 中途崩溃也保留已有内容 | 只在正常终态后生成 |
| 读者 | 运行期调试、人工排查 | 开发者质量判断 + 系统共享记忆 + 审计 |
| 注入 system prompt | 作为当前 run 的现场日志注入 | 仅"经验教训"段落注入后续 Run |

---

## 十四、Run Documentation 设计

每个 Run 到达终态（COMPLETED / FAILED / BLOCKED / CANCELLED / TIMED_OUT）时，系统自动生成一份结构化总结文档 `Documentation.md`。该文档同时服务于三个角色：**系统共享记忆**、**审计日志**和**开发者质量判断标准**。

### 设计动机

当前系统已有 `journal.md`（每步追加的简要日志）和 `iteration_reviews.jsonl`（机器可读的复盘数据），但缺少一个面向人且面向系统的**完整 Run 总结**。具体缺失：

- 开发者无法快速判断"这次 Run 做了什么、做得怎么样"
- 后续 Run 无法从上一次 Run 的经验中学习
- 缺少一份可读的 Run 级审计记录

Documentation.md 填补这一空白。

### 文档结构

```text
# Run <run_id> Documentation

## 基本信息
  目标 / 状态 / 起止时间 / 耗时 / 终止原因

## Step 执行时间线
  每个 Step 一行：id, kind, status, outcome, summary, 耗时估计
  标注关键转折点（REPLAN / RETRY 的触发原因）

## 迭代评分趋势
  每个 Step 的 score.total 表格
  最高分 / 最低分 / 最终走向

## 子 Agent 活动摘要
  总创建数（readonly / write）
  每个 Agent 的 scope、来源 Step、成功/失败、终止原因
  活跃 Agent 峰值
  Agent 创建/成功/失败聚合指标

## 资源消耗
  测试执行次数 / Bash 命令次数 / REPLAN 次数
  是否触发预算限制

## 质量评估
  最终健康度
  目标达成度评估
  未解决问题清单

## 关键决策记录
  每个 REPLAN / RETRY / BLOCK / RESUME_REPLAN 决策的 reason
  自动插入的 Step 及其原因

## 经验教训
  本轮发现的项目知识
  失败教训
  有效策略
```

### 各段落详细设计

#### 1. 基本信息

从 `RunState` 直接提取：

```text
## 基本信息

| 项目 | 值 |
|------|------|
| Run ID | a1b2c3d4e5f6 |
| 目标 | 修复 test_query_engine 中的断言失败 |
| 状态 | completed |
| 阶段 | completed |
| 创建时间 | 2026-04-16T10:00:00Z |
| 结束时间 | 2026-04-16T10:23:45Z |
| 运行耗时 | 23 分 45 秒 |
| 终止原因 | finalize step succeeded |
| 总 Step 数 | 8 |
| 成功 / 失败 | 6 / 2 |
```

#### 2. Step 执行时间线

从 `RunState.steps` 生成，每个 Step 一行，标注状态和迭代结果：

```text
## Step 执行时间线

| # | ID | 类型 | 状态 | 迭代结果 | 摘要 |
|---|------|------|------|----------|------|
| 1 | step-0001 | make_plan | succeeded | improved | 生成了 4 步修复方案 |
| 2 | step-0002 | edit_code | succeeded | improved | 修改 query_engine/engine.py |
| 3 | step-0003 | run_tests | succeeded | improved | 3 tests passed, 0 failed |
| 4 | step-0004 | finalize | succeeded | — | 目标达成，所有测试通过 |

**关键转折：**

- Step 2 → Step 3: `EDIT_CODE` 成功后自动插入 `RUN_TESTS`
- 无 REPLAN 或 RETRY 触发
```

对于更复杂的 Run（有 REPLAN/RETRY），关键转折部分会标注：

```text
**关键转折：**

- Step 3 → REPLAN: `RUN_TESTS` 失败（2 tests failed），自动插入 `INSPECT_FAILURES`
- Step 4 → Step 5: 检测到根因"未读取 base.py 即修改"，插入约束后重试
- Step 5 → RETRY: `EDIT_CODE` 重试 #1，约束："先读取 base.py，只修改 2 个文件"
- Step 7 → REPLAN #2: 连续无进展，触发重新规划
```

#### 3. 迭代评分趋势

从 `CheckpointStore` 加载所有 `iteration_reviews`，提取每个 Step 的 `score.total`：

```text
## 迭代评分趋势

| Step | 类型 | Score | Outcome | 根因 |
|------|------|-------|---------|------|
| step-0001 | make_plan | 6 | improved | — |
| step-0002 | edit_code | 5 | improved | — |
| step-0003 | run_tests | 8 | improved | 测试全部通过 |
| step-0005 | edit_code | 1 | stalled | 修改范围过大 |
| step-0006 | run_tests | -2 | regressed | 引入新失败 |

**趋势：** 最高分 8（step-0003），最低分 -2（step-0006），最终走向：改善
```

这使开发者可以一眼看出 Run 的"质量曲线"——是一直改善、先升后降、还是持续震荡。

#### 4. 子 Agent 活动摘要

从 `RunState.spawned_agents` 和 `RunState.metadata` 中的聚合指标生成。如果 spawned_agents 为空，显示"本 Run 未使用子 Agent"：

```text
## 子 Agent 活动摘要

| 指标 | 值 |
|------|------|
| 总创建数 | 5 |
| Readonly / Write | 4 / 1 |
| 成功 / 失败 | 4 / 1 |
| 活跃 Agent 峰值 | 3 |

| Agent | 类型 | 来源 Step | Scope | 结果 | 终止原因 |
|-------|------|-----------|-------|------|----------|
| a1b2 | readonly | step-0001 | src/query_engine/ | 成功 | — |
| c3d4 | readonly | step-0001 | src/tools/ | 成功 | — |
| e5f6 | readonly | step-0001 | tests/ | 成功 | — |
| g7h8 | readonly | step-0001 | src/context/ | 失败 | timeout |
| i9j0 | write | step-0005 | src/query_engine/ | 成功 | — |
```

#### 5. 资源消耗

从 `RunState` 的计数器直接计算：

```text
## 资源消耗

| 资源 | 使用量 | 上限 | 剩余 |
|------|--------|------|------|
| 运行时间 | 23m45s | 60m | 61% |
| 测试执行 | 3 次 | 20 | 85% |
| Bash 命令 | 5 次 | 50 | 90% |
| REPLAN 次数 | 1 次 | 3 | 66% |
| 活跃 Agent 峰值 | 4 | 2*2 | — |

*未触发任何预算限制。*
```

#### 6. 质量评估

综合所有数据给出最终判断：

```text
## 质量评估

| 维度 | 评价 |
|------|------|
| 目标达成度 | 完全达成 |
| 最终健康度 | progressing |
| 代码修改质量 | 聚焦修改 1 个文件，测试全部通过 |
| 迭代效率 | 8 步完成，无重试 |
| Agent 使用效率 | 4/5 只读 Agent 产出有效信息 |

### 未解决问题

（无）
```

对于未达成的 Run：

```text
## 质量评估

| 维度 | 评价 |
|------|------|
| 目标达成度 | 未达成 |
| 最终健康度 | blocked |
| 阻塞原因 | 连续 3 次 RUN_TESTS 失败，错误相同 |
| 代码修改质量 | 修改范围过大（6 个文件），未聚焦根因 |
| 迭代效率 | 14 步，3 次 REPLAN，2 次 RETRY |
| Agent 使用效率 | 2/3 只读 Agent 产出有效信息 |

### 未解决问题

- test_pipeline 系列测试仍然失败（3 个）
- 根因：pipeline.py 中的类型签名与下游不兼容
- 建议：先用 readonly agent 梳理 pipeline.py 的调用链，再精确修改类型签名
```

#### 7. 关键决策记录

从 `CheckpointStore` 加载 `events.jsonl`，读取 `step_completed` / `run_failed` / `run_timed_out` / `run_resumed` 的结构化 `data` 字段：

```text
## 关键决策记录

| Step | 决策 | 原因 | Active Agents | 自动插入 |
|------|------|------|---------------|----------|
| step-0003 | continue | step succeeded | 0 | run_tests |
| step-0005 | retry | step failed but retry budget remains | 0 | — |
| step-0007 | replan | verification failed; gather diagnostics and replan | 1 | inspect_failures,make_plan |
| — | resume_replan | resume recovered interrupted state and inserted replanning step | 0 | resume_replan |
```

#### 8. 经验教训

这是 Documentation.md 中**最关键的段落**，也是注入后续 Run system prompt 的核心内容。

来源：从所有 iteration_reviews 的 `root_cause` + `next_constraints` + Step 执行轨迹中提炼。

```text
## 经验教训

### 项目知识

- 本项目测试使用 pytest，测试命令为 `uv run pytest tests/`
- 代码风格：ruff format + mypy strict，修改后必须同时通过两者
- src/query_engine/engine.py 是核心模块，修改时需注意上下文压缩逻辑的边界条件

### 失败教训

- 不要同时修改 3 个以上文件，连锁影响难以控制
- 不要在未读取 base.py 的情况下修改继承自它的子类
- bash 超时默认 120 秒，全量测试可能超时，需用 -k 缩小范围

### 有效策略

- 先用 scan_dir 获取目录结构，再按模块派发 readonly agent 并行分析，效率最高
- 约束"只修改 1-2 个文件"能有效收敛建模范围
- 失败后先 INSPECT_FAILURES 再 EDIT_CODE，比直接重试成功率高
```

### 文档生成器设计

新增模块 `src/mini_cc/harness/doc_generator.py`，包含 `RunDocGenerator` 类。

#### 输入

| 数据源 | 来源 | 用途 |
|--------|------|------|
| RunState | 函数参数 | 基本信息、Step 列表、计数器、spawned_agents |
| IterationSnapshot 列表 | CheckpointStore.load_iteration_snapshots() | 评分趋势、事实数据 |
| IterationReview 列表 | CheckpointStore.load_iteration_reviews() | 迭代结果、根因、约束 |
| HarnessEvent 列表 | CheckpointStore.load_events() | 关键决策记录 |

#### 输出

一段完整的 Markdown 文本，由 `CheckpointStore.save_documentation()` 写入 `.mini_cc/runs/<run_id>/Documentation.md`。

#### 生成流程

```text
RunDocGenerator.generate(run_state, store)
  │
  ├── _render_basic_info(run_state)
  │     └── 从 RunState 提取目标、状态、时间、终止原因
  │
  ├── _render_step_timeline(run_state, reviews)
  │     └── 从 RunState.steps 生成时间线表格
  │     └── 从 events 过滤 REPLAN/RETRY 事件标注转折点
  │
  ├── _render_score_trend(reviews)
  │     └── 从 review.score.total 生成评分表格
  │
  ├── _render_agent_summary(run_state)
  │     └── 从 RunState.spawned_agents 生成 Agent 活动表
  │     └── 若 spawned_agents 为空，显示"未使用子 Agent"
  │
  ├── _render_resource_usage(run_state)
  │     └── 从 RunState 计数器计算使用量/上限/剩余
  │
  ├── _render_quality_assessment(run_state, reviews)
  │     └── 综合终态、最终健康度、评分趋势生成质量评价
  │     └── 从最后一个 review 的 next_constraints 提取未解决问题
  │
  ├── _render_decisions(events)
  │     └── 过滤 step_completed / run_failed / run_timed_out / run_resumed
  │     └── 提取 policy 决策与恢复决策数据
  │
  └── _render_lessons_learned(reviews, snapshots)
        └── 从所有 review 的 root_cause 提炼失败教训
        └── 从成功的 Step 提炼有效策略
        └── 从约束和执行轨迹提炼项目知识
```

#### 调用时机

在 `SupervisorLoop.run()` 的主循环退出后、return 之前：

```text
while not run_state.is_terminal:
    ...  # 主循环

# Run 已到终态，生成 Documentation.md
doc = self._doc_generator.generate(run_state, self._store)
self._store.save_documentation(run_state.run_id, doc)

return run_state
```

无论终态是 COMPLETED / FAILED / BLOCKED / CANCELLED / TIMED_OUT，都会生成。中途崩溃不生成（此时 journal.md 作为安全网存在）。

### 与 AgentEventBus 的关系

`_render_agent_summary()` 依赖 `RunState.spawned_agents` 与 `RunState.metadata` 中的聚合指标。当前实现中，默认 `create_engine() -> AgentManager -> EngineContext -> RunHarness.create_default()` 链路已经共享同一个 `AgentEventBus`，因此 Harness 能统一感知：

- Harness 结构化派生的 Agent
- Query-backed Step 中由 LLM 通过 `AgentTool` 自主创建的 Agent

SupervisorLoop 会在每轮开始前和 Step 执行后 drain bus，持续刷新：

- `agents_created_readonly`
- `agents_created_write`
- `agents_succeeded`
- `agents_failed`
- `agent_peak_active`

文档生成器直接读取这些聚合指标，不再依赖临时推断。

### 注入后续 Run 的 system prompt

修改 `_build_harness_context()`，在读取当前 Run 现场信息的基础上，附加最近一次终态 Run 的 `Documentation.md` 的"经验教训"段落。

注入格式：

```text
<run_context>
Run ID: a1b2c3d4
Run status: running
...
Recent reviews and journal:
- Step step-0004: tests still failing in pipeline.py
- Constraint: only modify src/pipeline.py and related tests

Lessons from previous completed run:
- 测试命令: uv run pytest tests/
- 不要同时修改 3 个以上文件
- 先用 scan_dir 获取目录结构再派发 readonly agent
</run_context>
```

注入优先级：

1. 如果当前有 active run_id，始终保留当前 Run 的现场上下文（状态、最近 review、journal tail）
2. 如果当前 Run 已有 Documentation.md 的"经验教训"段落，额外附加 `Lessons from current run`
3. 如果当前 Run 还没有 lessons，则保留当前 Run 的 `review + journal tail`，并再附加最近一次终态 Run 的 Documentation.md lessons
4. 如果没有可用的 Documentation.md lessons，则只使用当前 Run 的现场上下文

"经验教训"段落通过 `## 经验教训` heading 在文档中定位，只提取该段落的内容（不注入完整的 Documentation.md，避免 token 浪费）。

### 文档示例：完整的 Documentation.md

以下是一个成功 Run 的完整文档示例：

```text
# Run a1b2c3d4 Documentation

## 基本信息

| 项目 | 值 |
|------|------|
| Run ID | a1b2c3d4e5f6 |
| 目标 | 修复 test_query_engine 中的断言失败 |
| 状态 | completed |
| 创建时间 | 2026-04-16T10:00:00Z |
| 结束时间 | 2026-04-16T10:23:45Z |
| 运行耗时 | 23 分 45 秒 |
| 终止原因 | finalize step succeeded |
| 总 Step 数 | 8 |
| 成功 / 失败 | 6 / 2 |

## Step 执行时间线

| # | ID | 类型 | 状态 | 迭代结果 | 摘要 |
|---|------|------|------|----------|------|
| 1 | step-0001 | analyze_repo | succeeded | improved | 识别到 query_engine 模块为主要修改区域 |
| 2 | step-0002 | make_plan | succeeded | improved | 生成 4 步修复方案，聚焦 engine.py |
| 3 | step-0003 | edit_code | succeeded | improved | 修改 query_engine/engine.py 的 drain 逻辑 |
| 4 | step-0004 | run_tests | succeeded | improved | 3 tests passed, 0 failed |
| 5 | step-0005 | edit_code | succeeded | stalled | 修改了无关文件，无测试改善 |
| 6 | step-0006 | run_tests | succeeded | improved | 3 tests passed, 0 failed（与上轮相同） |
| 7 | step-0007 | run_tests | succeeded | improved | 最终验证通过 |
| 8 | step-0008 | finalize | succeeded | — | 目标达成 |

**关键转折：**

- Step 3 → Step 4: `EDIT_CODE` 成功后自动插入 `RUN_TESTS`
- Step 5: 约束注入"只修改 engine.py"，收敛建模范围

## 迭代评分趋势

| Step | 类型 | Score | Outcome | 根因 |
|------|------|-------|---------|------|
| step-0001 | analyze_repo | 6 | improved | — |
| step-0002 | make_plan | 5 | improved | — |
| step-0003 | edit_code | 8 | improved | 聚焦修改 |
| step-0004 | run_tests | 8 | improved | 全部通过 |
| step-0005 | edit_code | 1 | stalled | 修改范围过大 |
| step-0006 | run_tests | 6 | improved | 约束生效 |

**趋势：** 最高分 8，最低分 1，最终走向：改善

## 子 Agent 活动摘要

| 指标 | 值 |
|------|------|
| 总创建数 | 3 |
| Readonly / Write | 3 / 0 |
| 成功 / 失败 | 3 / 0 |
| 预算消耗 | readonly 3/5 |

| Agent | 类型 | 来源 Step | Scope | 结果 | 终止原因 |
|-------|------|-----------|-------|------|----------|
| a1b2 | readonly | step-0001 | src/query_engine/ | 成功 | — |
| c3d4 | readonly | step-0001 | src/tools/ | 成功 | — |
| e5f6 | readonly | step-0001 | tests/ | 成功 | — |

## 资源消耗

| 资源 | 使用量 | 上限 | 剩余 |
|------|--------|------|------|
| 运行时间 | 23m45s | 60m | 61% |
| 测试执行 | 3 次 | 20 | 85% |
| Bash 命令 | 4 次 | 50 | 92% |
| REPLAN 次数 | 0 次 | 3 | 100% |

*未触发任何预算限制。*

## 质量评估

| 维度 | 评价 |
|------|------|
| 目标达成度 | 完全达成 |
| 最终健康度 | progressing |
| 代码修改质量 | 最终聚焦到 1 个文件，测试全部通过 |
| 迭代效率 | 8 步完成，无重试无 REPLAN |
| Agent 使用效率 | 3/3 只读 Agent 产出有效信息 |

### 未解决问题

（无）

## 关键决策记录

| Step | 决策 | 原因 | Active Agents | 自动插入 |
|------|------|------|---------------|----------|
| step-0003 | CONTINUE | step succeeded | 0 | RUN_TESTS |
| step-0004 | CONTINUE | step succeeded | 0 | — |
| step-0005 | CONTINUE | step succeeded（stalled 但有 progress） | 1 | RUN_TESTS |
| step-0008 | COMPLETE | finalize step succeeded | 0 | — |

## 经验教训

### 项目知识

- 测试框架为 pytest，命令 `uv run pytest tests/`
- 代码质量工具：ruff format + mypy strict
- query_engine/engine.py 是核心模块，_query_loop 中的 drain 逻辑需注意边界

### 失败教训

- 不要同时修改 3 个以上文件，连锁影响难以控制
- 编辑前必须先读取目标文件，理解上下文

### 有效策略

- 先用 scan_dir 获取目录结构，再按模块派发 readonly agent 并行分析
- 约束"只修改 1-2 个文件"能有效收敛建模范围
- `EDIT_CODE` 后立即 `RUN_TESTS` 验证，避免积累错误
```

---

## 十五、并发冲突防护

### 四层防护体系

| 层级 | 机制 | 防护对象 |
|------|------|----------|
| 严格串行写 | Harness 预算层在预算注入阶段强制 `max_write=1`，并保证同一时刻最多 1 个活跃 write Agent | write-write 冲突 |
| Scope 隔离 | AgentManager 检查新 write Agent 的 scope 与所有活跃 write Agent 无路径前缀重叠 | 额外保护 |
| 读写工具分离 | Readonly Agent 只用 create_readonly_registry()，无 file_edit / file_write | read-write 冲突（工具级） |
| 文件快照 | Write Agent 修改文件前自动 SnapshotService.snapshot()，可 restore_all() 回滚 | 误修改恢复 |
| 工具级串行 | StreamingToolExecutor 将 unsafe 工具串行执行 | 单 Agent 内部竞态 |

### Scope 检查算法

两个 scope 路径的公共前缀匹配：如果任一方是 `"."`（根路径），则视为重叠；否则比较 Path.parts 的公共前缀是否相同。这确保了 `src/foo/` 和 `src/bar/` 不冲突，但 `src/` 和 `src/foo/` 冲突。当前实现中，scope 检查是**严格串行写策略之上的补充约束**。

### 已知局限

- **无 git worktree 隔离**：所有 Agent 共享同一工作目录。Readonly Agent 可能看到 Write Agent 正在修改的中间状态。
- **Staleness 检测是事后的**：`base_version_stamp` 在 Agent 创建和完成时各取一次，只在 `AgentCompletionEvent.is_stale` 中标记，不会自动重试。
- **Readonly Agent 之间无一致性保证**：多个并行 Readonly Agent 可能对同一文件看到不同版本（如果其间有 Write Agent 活跃）。

---

## 十六、与现有代码的集成方案

### QueryEngine

保留不动，作为单 Step 的 Agent 执行器。Harness 不直接修改 QueryEngine 内部状态机。AgentEventBus 和 AgentBudget 是在 QueryEngine 之外的层注入的。

### AgentManager

同时服务两条 Agent 编排路径。新增：
- `lifecycle_bus` 参数：创建/清理 Agent 时发布生命周期事件
- `set_current_step()` / `clear_current_step()`：标记当前 Step 上下文
- Scope 冲突检查对两条路径统一生效

默认集成链路为：

```text
create_engine()
  └── 创建 AgentEventBus
  └── AgentManager(lifecycle_bus=bus)
  └── EngineContext(lifecycle_bus=bus)

RunHarness.create_default()
  └── 复用 engine_ctx.lifecycle_bus
```

这样 TUI / 默认聊天链路中的 `AgentTool` 派生 Agent 也能被 Harness 追踪到。

### AgentTool

新增 `get_budget` 回调参数。创建 Agent 前检查并扣减 AgentBudget。耗尽时返回错误给 LLM。

### TaskService

继续负责子 Agent 的任务记录，与 CheckpointStore 职责分离——"任务账本"与"运行时状态机"不混为一物。

### TUI

ChatScreen 是 TUI 的中心编排屏幕，通过 Harness 事件回调实时更新 Run 状态和 Step 进度。每次新 Run 创建时会刷新 EngineContext，确保不同 Run 之间完全隔离。

---

## 十七、典型运行示例

### 示例一：修复 failing test

```text
Run created
  │
  ├─ Step 1: ANALYZE_REPO (QueryEngine, plan mode)
  │    └── LLM 读取仓库结构，分析测试失败原因
  │
  ├─ Step 2: MAKE_PLAN (QueryEngine, plan mode)
  │    └── LLM 生成修复方案和后续 Step 建议
  │
  ├─ Step 3: EDIT_CODE (QueryEngine, build mode)
  │    └── LLM 修改相关代码
  │    └── IterationOptimizer 自动插入 RUN_TESTS
  │
  ├─ Step 4: RUN_TESTS (Bash)
  │    ├── 通过 → 继续
  │    └── 失败 → IterationOptimizer 自动插入 INSPECT_FAILURES
  │
  ├─ Step 5: INSPECT_FAILURES (Bash) [如需要]
  │
  ├─ Step 6: EDIT_CODE (重试) [如需要]
  │    └── 约束注入: "只修改目标模块, 先读取失败测试相关文件"
  │
  ├─ Step 7: RUN_TESTS [验证修复]
  │
  └─ Step 8: FINALIZE
       └── 生成最终摘要
```

### 示例二：LLM 自主派生 Agent 时的预算管控

```text
Step: ANALYZE_REPO
  │
  ├── EngineContext.agent_budget = {max_readonly: 5, max_write: 1, remaining_readonly: 5, remaining_write: 1}
  │
  ├── LLM 调用 AgentTool(dispatch_plan_json=...) → 创建 3 个 readonly Agent
  │    └── budget.remaining_readonly: 5 → 2
  │
  ├── LLM 再调用 AgentTool(prompt=..., readonly=true) → 创建 1 个 readonly Agent
  │    └── budget.remaining_readonly: 2 → 1
  │
  ├── LLM 再调用 AgentTool(prompt=..., readonly=true) → 创建 1 个 readonly Agent
  │    └── budget.remaining_readonly: 1 → 0
  │
  ├── LLM 再调用 AgentTool(prompt=..., readonly=true) → 被拦截
  │    └── ToolResult(success=False, "只读 Agent 预算已耗尽...")
  │
  └── StepResult.metadata = {agents_remaining_ro: "0", agents_remaining_w: "1"}
```

若某一时刻已有活跃 write Agent，则下一步注入预算时会强制：

```text
EngineContext.agent_budget = {max_write: 1, remaining_write: 0}
```

即使外部配置曾把 `max_write` 设为大于 1，Harness 仍会在执行前钳制为严格串行写。

### 示例四：resume 恢复与结构化事件

```text
Run interrupted
  │
  ├── state.json 中存在 2 个 IN_PROGRESS steps
  ├── spawned_agents 中存在 1 个未完成 readonly Agent
  │
  ├── RunHarness.resume()
  │    ├── 将未完成 Agent 标记为 invalidated_on_resume
  │    ├── 将 2 个 IN_PROGRESS steps 回退为 PENDING
  │    ├── 在队列头插入 Resume Replan(MAKE_PLAN)
  │    └── 记录 run_resumed 事件：
  │         decision=resume_replan
  │         invalidated_agents=1
  │         recovered_steps=2
  │
  └── SupervisorLoop 从 Resume Replan 开始继续运行
```

### 示例三：REPLAN 深度限制

```text
Step: MAKE_PLAN → Step: EDIT_CODE → Step: RUN_TESTS (失败)
  │
  ├── PolicyEngine: REPLAN (replan_count: 0 → 1)
  ├── 插入 MAKE_PLAN → 新的 EDIT_CODE → RUN_TESTS (再次失败)
  │
  ├── PolicyEngine: REPLAN (replan_count: 1 → 2)
  ├── 插入 MAKE_PLAN → 新的 EDIT_CODE → RUN_TESTS (第三次失败)
  │
  ├── PolicyEngine: REPLAN (replan_count: 2 → 3)
  ├── 插入 MAKE_PLAN → 新的 EDIT_CODE → RUN_TESTS (第四次失败)
  │
  └── PolicyEngine: replan_count >= 3 → FAIL（而非继续 REPLAN）
```

---

## 十八、完整数据流总览

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                                 TUI / REPL                                   │
│                         ChatScreen._run_goal(text)                           │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              RunHarness                                      │
│                         run() / resume() / cancel()                          │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                            SupervisorLoop                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌──────────┐  ┌───────────┐ │
│  │PolicyEng.│  │ RunJudge │  │IterationOpt. │  │EventBus  │  │Checkpoint │ │
│  │          │  │          │  │              │  │ drain()  │  │  Store    │ │
│  └──────────┘  └──────────┘  └──────────────┘  └──────────┘  └───────────┘ │
│                                                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐ │
│  │ Run 终态后: RunDocGenerator.generate() → Documentation.md              │ │
│  └─────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              StepRunner                                       │
│  ┌─────────────────┐  ┌──────────────┐  ┌─────────────────────────────────┐ │
│  │ Query-backed    │  │ Bash-backed  │  │ Agent-backed                    │ │
│  │ Step → Engine   │  │ Step → Bash  │  │ Step → AgentManager.create()    │ │
│  └────────┬────────┘  └──────────────┘  └─────────────────────────────────┘ │
└───────────┼──────────────────────────────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                           EngineContext                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────────────────┐ │
│  │ QueryEngine  │  │ AgentManager │  │ AgentBudget (per-step wallet)      │ │
│  │              │  │              │  │                                    │ │
│  │ LLM Stream   │  │ create()     │  │ AgentTool 检查/扣减                │ │
│  │ Tool Execute │  │ cleanup()    │  │ 耗尽 → ToolResult(error)           │ │
│  │ Auto Compact │  │              │  │                                    │ │
│  └──────┬───────┘  └──────┬───────┘  └────────────────────────────────────┘ │
│         │                  │                                                │
│         │         publish lifecycle events                                  │
│         │                  │                                                │
└─────────┼──────────────────┼────────────────────────────────────────────────┘
          │                  │
          ▼                  ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────────────┐
│ OpenAI Provider  │  │ AgentEventBus    │  │ Documentation.md                 │
│ (LLM Streaming)  │  │ (SupervisorLoop  │  │ (终态生成 → 注入后续 Run         │
│                  │  │  consumption)    │  │  system prompt 的经验教训段落)    │
└──────────────────┘  └──────────────────┘  └──────────────────────────────────┘
```

---

## 十九、成功标准

若系统达到以下标准，则认为 Harness 设计成立：

- 可创建 Run 并持续执行多个 Step
- 可围绕一个 repo 任务持续运行 30 到 60 分钟
- 运行中支持编辑、测试、复盘、重试
- 进程中断后可从磁盘恢复
- 遇到重复失败时不会无限死循环（REPLAN 深度限制）
- Harness 能感知所有子 Agent（无论创建路径），Agent 数量不失控（AgentBudget）
- 每个 Run 终态后自动生成 Documentation.md，包含完整的时间线、评分趋势、质量评估和经验教训
- Documentation.md 的经验教训段落自动注入后续 Run 的 system prompt，形成跨 Run 的记忆传递
- 开发者可通过 Documentation.md 快速判断 Run 质量，无需阅读 machine-readable 的 JSONL 文件
- 用户能看到 Run 当前在做什么、为何继续或停止、子 Agent 的活动概况
