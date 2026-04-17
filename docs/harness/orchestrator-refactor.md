# Harness 多 Agent 编排重构方案

## 一、背景

当前 Harness 已经把部分 prompt 类 Step 委派给子 Agent 执行，但整体编排仍然存在几个结构性问题：

- 主流程仍然是串行 `ready_steps()[0]` 调度，不是真正的调度器
- `StepRunner` 委派子 Agent 与 `AgentTool` 创建子 Agent 走的是两套路径
- 子 Agent 观测链路不统一，`TextDelta`、工具调用、完成事件的可见性不对称
- `429`、超时、工具失败没有统一失败分型，容易被累计成错误 block
- `bootstrap_project`、`edit_code` 仍然是大颗粒 Step，恢复和重试成本高

本方案的目标是把系统重构为：

- 主 Agent 只负责调度
- 子 Agent 只负责执行
- 所有执行单元统一走一个 dispatcher
- 所有失败都按类别处理
- 所有事件都进入统一 trace

---

## 二、目标与非目标

### 目标

- 将 Harness 从“串行 StepRunner”演进为“可调度的 Orchestrator”
- 统一 Step 委派和 AgentTool 委派的资源治理、事件模型和预算模型
- 把大 Step 拆成更小的 WorkItem，支持小粒度重试和恢复
- 建立可解释的失败分类、退避和等待机制
- 建立统一的执行 trace，能够明确区分 LLM 等待、工具执行、子 Agent 阻塞

### 非目标

- 不引入分布式调度
- 不支持递归多层子 Agent
- 不在本阶段重写 QueryEngine 的基本流式语义
- 不在本阶段引入真正的远程沙箱或多工作区隔离

---

## 三、重构后的分层

建议将当前 Harness / Agent 编排重构为四层：

### 1. Orchestrator

职责：

- 维护 Run 生命周期
- 选择下一批可执行 WorkItem
- 分配预算、优先级、并发槽位
- 处理失败、退避、等待和 block
- 不直接调用模型

对应现有模块：

- `src/mini_cc/harness/supervisor.py`
- `src/mini_cc/harness/policy.py`
- `src/mini_cc/harness/judge.py`

### 2. Dispatcher

职责：

- 统一接收执行请求
- 决定由子 Agent 还是本地执行器承接
- 统一注入 run context、预算、trace context
- 统一处理超时、取消、结果回传

建议新增模块：

- `src/mini_cc/harness/dispatcher.py`

### 3. Worker

职责：

- 承担单一职责的执行工作
- 不关心全局调度
- 只对自己的 WorkItem 负责

建议的 Worker 角色：

- `planner`
- `analyzer`
- `implementer`
- `verifier`
- `reporter`

### 4. Event Bus / Trace Store

职责：

- 汇聚 worker 生命周期事件
- 生成统一 trace tree
- 给 TUI / checkpoint / docs 提供统一的可观测数据

对应现有模块：

- `src/mini_cc/agent/bus.py`
- `src/mini_cc/harness/checkpoint.py`
- `src/mini_cc/harness/doc_generator.py`

需要进一步统一，而不是继续由不同调用路径各自拼 metadata。

---

## 四、核心数据模型

### 1. WorkItem

`Step` 继续保留为 Run 级编排结构，但真正调度和执行的最小单元应下沉为 `WorkItem`。

建议新增模型：

```python
class WorkItem(BaseModel):
    id: str
    run_id: str
    step_id: str
    kind: str
    role: WorkerRole
    goal: str
    inputs: dict[str, str]
    artifacts: dict[str, str]
    dependencies: list[str]
    priority: int
    timeout_seconds: int
    retry_policy: RetryPolicy
    write_scope: list[str]
    status: WorkItemStatus
    metadata: dict[str, str]
```

设计要点：

- `kind` 表示任务内容，例如 `bootstrap.detect_scaffold`
- `role` 表示执行角色，例如 `analyzer`
- `step_id` 只负责把多个 WorkItem 归属于同一个 Step
- 一个 Step 可以拆成多个 WorkItem

### 2. ExecutionSpec

用于描述 WorkItem 应如何执行。

```python
class ExecutionSpec(BaseModel):
    executor_type: Literal["sub_agent", "local_command"]
    role: WorkerRole
    readonly: bool
    timeout_seconds: int
    max_tool_calls: int | None
    cwd: str
    env: dict[str, str]
    trace_context: dict[str, str]
```

设计要点：

- `StepRunner` 和 `AgentTool` 不再自己拼 agent 参数
- 所有执行请求都先转换成 `ExecutionSpec`

### 3. FailureClass

建议新增统一失败分型：

```python
class FailureClass(StrEnum):
    TRANSIENT_PROVIDER = "transient_provider"
    TRANSIENT_ENV = "transient_env"
    TOOL_FAILURE = "tool_failure"
    LOGIC_FAILURE = "logic_failure"
    TIME_BUDGET_EXCEEDED = "time_budget_exceeded"
    CANCELLED = "cancelled"
    HUMAN_BLOCKED = "human_blocked"
```

设计要点：

- `429` 归类为 `TRANSIENT_PROVIDER`
- sandbox / 文件锁 / 临时环境异常归类为 `TRANSIENT_ENV`
- 测试失败、审计失败通常归类为 `LOGIC_FAILURE`
- 超时不再简单等同于失败重试计数 +1

---

## 五、调度模型

### 1. 调度原则

Orchestrator 应以 WorkItem 为单位调度，而不是简单执行第一个 ready Step。

推荐优先级顺序：

- `verifier`
- `implementer`
- `analyzer`
- `planner`
- `reporter`

同时引入三个修正因子：

- `aging_bonus`：等待越久优先级越高，避免饥饿
- `retry_penalty`：连续失败的任务优先级降低
- `cooldown_penalty`：处于退避窗口内的任务不调度

### 2. 建议公式

```text
effective_priority = base_priority + aging_bonus - retry_penalty - cooldown_penalty
```

### 3. 并发模型

建议引入两层并发限制：

- `global_max_active_workers`
- `per_role_limit`

推荐默认值：

- `planner = 1`
- `analyzer = 2`
- `implementer = 2`
- `verifier = 1`
- `reporter = 1`

### 4. 调度行为

Orchestrator 每轮执行：

1. 拉取 ready work items
2. 过滤处于 cooldown / waiting 状态的任务
3. 按 `effective_priority` 排序
4. 在并发槽位内派发给 dispatcher
5. 监听完成事件并更新 RunState

---

## 六、Step 拆分方案

### 1. Bootstrap 拆分

当前问题：

- 目标过大
- 工具调用过多
- 超时后无法精确恢复

建议拆分为：

- `bootstrap.inspect_repo`
- `bootstrap.detect_scaffold`
- `bootstrap.generate_skeleton`
- `bootstrap.write_skeleton`
- `bootstrap.verify_bootstrap`

角色建议：

- 前两项由 `analyzer`
- 中间两项由 `implementer`
- 最后一项由 `verifier`

### 2. Analyze 拆分

建议拆分为：

- `analyze.repo_map`
- `analyze.key_files`
- `analyze.constraints`
- `analyze.risk_summary`

### 3. Plan 拆分

建议拆分为：

- `plan.work_breakdown`
- `plan.acceptance_checks`
- `plan.execution_order`

### 4. Edit 拆分

建议拆分为：

- `edit.select_target_slice`
- `edit.apply_patch_slice`
- `edit.self_check`
- `edit.emit_change_summary`

### 5. Verify 拆分

建议拆分为：

- `verify.tests`
- `verify.audit`
- `verify.failure_inspection`

### 6. 颗粒度约束

每个 WorkItem 应满足：

- 目标单一
- 正常情况下 1 到 3 次工具调用内可收束
- 单次超时后可单独重试
- 输出可作为下游 WorkItem 的稳定输入

---

## 七、统一委派入口

### 1. 当前问题

当前存在两套子 Agent 创建路径：

- `StepRunner._run_delegated_agent_step()`
- `AgentTool.call()`

这会导致：

- 预算统计不一致
- 并发控制不一致
- trace 注入不一致
- run context 传播路径分叉

### 2. 目标方案

引入统一入口：

```python
dispatch_work_item(work_item: WorkItem, spec: ExecutionSpec) -> DispatchHandle
```

所有子 Agent 创建都必须经过此入口。

### 3. Dispatcher 职责

- 根据 `ExecutionSpec` 创建 readonly / write 子 Agent
- 注入 `run_id`、`step_id`、`work_item_id`、`trace_id`
- 注册生命周期事件
- 统一扣减 agent budget
- 统一执行超时、取消和清理

### 4. 模块迁移建议

- `src/mini_cc/harness/step_runner.py`
  - 从“直接创建 agent”改为“构造 WorkItem + 调 dispatcher”
- `src/mini_cc/tools/agent_tool.py`
  - 从“直接调 manager.create_agent()`”改为“转成 dispatch request”
- `src/mini_cc/agent/manager.py`
  - 降级为底层 agent 生命周期管理器，不再承担调度语义

---

## 八、失败分型与重试策略

### 1. 状态机建议

建议引入更明确的 WorkItem 状态：

- `queued`
- `running`
- `cooldown`
- `waiting_human`
- `failed_retryable`
- `failed_terminal`
- `completed`

### 2. 分型策略

| 场景 | FailureClass | 处理策略 |
|------|--------------|----------|
| Provider 429 / 临时 API 错误 | `TRANSIENT_PROVIDER` | 指数退避，进入 `cooldown` |
| 本地命令瞬时失败 / 临时环境异常 | `TRANSIENT_ENV` | 限次重试 |
| 工具调用返回失败 | `TOOL_FAILURE` | 记录 artifact，必要时转 `failure_inspection` |
| 测试失败 / 审计失败 / 逻辑不正确 | `LOGIC_FAILURE` | 进入修复链路，不直接 block |
| 超时 | `TIME_BUDGET_EXCEEDED` | 若已产出 artifact，拆分后重试；否则限次重试 |

### 3. 退避规则

建议默认退避窗口：

- 第 1 次 `429`：30 秒
- 第 2 次 `429`：120 秒
- 第 3 次 `429`：300 秒

超过阈值后转为：

- `waiting_human`

而不是直接：

- `blocked`

### 4. Block 触发条件

`blocked` 只用于：

- 明确缺少人工输入
- 权限不足
- 环境不可修复
- 多次逻辑失败后无法自动收敛

---

## 九、统一可观测性

### 1. 目标

需要能回答以下问题：

- 慢的是 LLM 还是工具
- 慢的是主调度还是子 Agent
- 哪个 WorkItem 重试最多
- 哪个角色最容易触发 `429`

### 2. Span Trace 模型

建议引入 span tree：

```text
run
└── step
    └── work_item
        └── worker
            ├── llm_turn
            ├── tool_call
            ├── nested_agent
            └── local_command
```

每个 span 至少记录：

- `span_id`
- `parent_span_id`
- `run_id`
- `step_id`
- `work_item_id`
- `kind`
- `role`
- `start_at`
- `end_at`
- `duration_ms`
- `status`
- `failure_class`
- `input_summary`
- `output_summary`

### 3. 事件统一

建议统一的 worker 事件至少包括：

- `worker_started`
- `worker_text_delta`
- `worker_tool_call`
- `worker_tool_result`
- `worker_completed`
- `worker_failed`
- `worker_cancelled`

`StepRunner` 不应再自行拼装一套特殊 metadata。

### 4. 持久化位置

建议保留并扩展：

- `events.jsonl`
- `iteration_snapshots.jsonl`

新增：

- `trace_spans.jsonl`

---

## 十、权限与角色约束

建议显式定义角色权限，而不是继续依赖 `step kind -> readonly/build` 的隐式映射。

| 角色 | 文件读取 | 文件写入 | Bash | AgentTool |
|------|----------|----------|------|-----------|
| `planner` | 是 | 否 | 否 | 否 |
| `analyzer` | 是 | 否 | 只读型 | 否 |
| `implementer` | 是 | 是 | 是 | 否 |
| `verifier` | 是 | 否 | 是 | 否 |
| `reporter` | 是 | 否 | 否 | 否 |

约束原则：

- 主 Orchestrator 不直接拿工具权限
- Worker 不允许再创建新的 Worker
- 所有创建只能通过 Dispatcher

---

## 十一、具体实现清单

### Phase 1：统一委派入口

目标：

- 收敛所有子 Agent 创建路径

实现项：

- 新增 `src/mini_cc/harness/dispatcher.py`
- 新增 `WorkItem`、`ExecutionSpec`、`FailureClass` 模型
- `StepRunner` 改为构造 `WorkItem` 并调用 dispatcher
- `AgentTool` 改为构造 dispatch request，而非直接调用 `AgentManager`
- `AgentManager` 只保留 agent 生命周期与注册逻辑

验收标准：

- 所有子 Agent 创建都能记录 `run_id + step_id + work_item_id`
- 所有子 Agent 都走同一套预算扣减逻辑
- 不再存在两套并行的 agent 创建口径

### Phase 2：Step 下沉为 WorkItem

目标：

- 把大 Step 拆成小颗粒 WorkItem

实现项：

- 为 `bootstrap_project` 引入 WorkItem 拆分
- 为 `edit_code` 引入 WorkItem 拆分
- `RunState` 新增 work item 状态追踪
- `SupervisorLoop` 从“选 Step”改为“选 ready WorkItem”

验收标准：

- `bootstrap_project` 超时后可只重试失败的子项
- `edit_code` 不再是单个大回合
- journal 和 snapshot 能展示 work item 级状态

### Phase 3：失败分型与退避

目标：

- 让失败处理从“计数器”升级为“分类状态机”

实现项：

- `PolicyEngine` 接入 `FailureClass`
- `RunJudge` 输出结构化失败原因
- 引入 `cooldown` / `waiting_human` 状态
- Provider 限流走退避，不直接触发 block

验收标准：

- 连续 `429` 不再直接 block
- timeout 会区分“无进展超时”和“有产出超时”
- 失败事件中可见 `failure_class`

### Phase 4：统一事件总线与 trace

目标：

- 建立完整执行链路

实现项：

- 统一 `worker_*` 事件模型
- 子 Agent `TextDelta` 全量透传
- 新增 `trace_spans.jsonl`
- TUI Run 详情页展示 work item 和 span 维度耗时

验收标准：

- 任一慢步骤都能拆解到 LLM / tool / worker 级别
- step 委派和 agent tool 的观测格式一致
- 旧的 ad-hoc `diag_*` metadata 可以逐步收敛

### Phase 5：角色化调度

目标：

- 让 orchestrator 从串行执行器升级为真正调度器

实现项：

- 引入 per-role 并发限制
- 引入优先级、aging、retry penalty
- verifier 优先级高于 implementer
- 支持 work item 级取消与抢占

验收标准：

- `ready_steps()[0]` 式调度不再存在
- verifier 类任务可抢在后续实现前运行
- 长任务不会长期独占调度资源

---

## 十二、建议的模块改动顺序

建议按以下顺序落地，避免一次性重写：

1. `models.py`
   新增 `WorkItem`、`ExecutionSpec`、`FailureClass`、相关状态枚举
2. `dispatcher.py`
   建立统一委派入口
3. `step_runner.py`
   改为只负责 Step 到 WorkItem 的映射
4. `agent_tool.py`
   接入 dispatcher
5. `supervisor.py`
   从 Step 调度过渡到 WorkItem 调度
6. `policy.py` / `judge.py`
   接入失败分型和退避
7. `checkpoint.py` / `doc_generator.py` / TUI
   接入 work item 和 trace 展示

---

## 十三、测试清单

### 单元测试

- dispatcher 能统一创建 readonly / write worker
- `AgentTool` 与 `StepRunner` 走同一委派入口
- `FailureClass` 映射正确
- cooldown 状态不会被调度
- work item 优先级排序符合预期

### 集成测试

- bootstrap 拆分后，单个 work item 超时可以恢复
- edit_code 拆分后，可先实现再验证
- 连续 `429` 进入 cooldown 而不是 block
- trace 中能看到子 Agent `TextDelta`

### 回归测试

- 现有单 Agent chat 能继续工作
- 没有 `AgentManager` 的测试场景仍可降级运行
- run 文档与 TUI 不因新增状态而崩溃

---

## 十四、第一阶段的最小落地范围

如果只做第一轮重构，建议严格限制在以下范围：

- 新增 `dispatcher.py`
- 建立统一的 `WorkItem` / `ExecutionSpec`
- 让 `StepRunner` 和 `AgentTool` 共用一条 agent 创建链路
- 先不拆全部 Step
- 先不大改 TUI

原因：

- 这是后续预算、trace、失败分型统一的前提
- 如果不先收口 agent 创建路径，后续所有策略都会继续分叉

这也是整个重构的第一刀。
