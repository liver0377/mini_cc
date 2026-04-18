# 自主运行线束 (harness)

自主运行线束（Harness）是系统的自动化执行框架，能够将复杂的编程任务分解为多个步骤，自主调度执行，并通过裁判系统和策略引擎进行质量控制和错误恢复。

## 模块结构

```
harness/
├── models.py          # 数据模型（RunState, Step, WorkItem 等）
├── events.py          # Harness 事件
├── runner.py          # RunHarness — 顶层运行编排器
├── scheduler.py       # Scheduler — 步骤/工作项调度器
├── step_runner.py     # StepRunner — 步骤执行器
├── judge.py           # RunJudge — 运行健康度裁判
├── supervisor.py      # SupervisorLoop — 监督主循环
├── policy.py          # PolicyEngine — 策略决策引擎
├── checkpoint.py      # CheckpointStore — 状态持久化
├── diagnostics.py     # QueryDiagnostics — 查询诊断
├── doc_generator.py   # RunDocGenerator — 文档生成器
├── bootstrap.py       # 引导步骤计划生成
├── iteration.py       # IterationOptimizer — 迭代优化器
├── dispatch_roles.py  # 步骤角色映射
└── audit/             # 任务审计系统
    ├── core.py        # 审计核心（插件化）
    └── plugins/       # 内置审计插件
        └── mini_jq/   #   mini_jq 审计插件
```

## 整体架构图

```
┌────────────────────────────────────────────────────────────────┐
│                         RunHarness                              │
│                     （顶层运行编排器）                             │
│                                                                │
│  · 创建运行（create_run）                                       │
│  · 恢复运行（resume_run）                                       │
│  · 取消运行（cancel_run）                                       │
│  · 失效化飞行中智能体                                           │
└──────────────────────────┬─────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────────┐
│                      SupervisorLoop                             │
│                       （监督主循环）                              │
│                                                                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │Scheduler │  │StepRunner│  │RunJudge  │  │PolicyEngine  │  │
│  │ 调度步骤  │  │ 执行步骤  │  │ 健康评估  │  │ 决策下一步    │  │
│  └──────────┘  └──────────┘  └──────────┘  └──────────────┘  │
│                                                                │
│  ┌──────────────────┐  ┌──────────────────┐  ┌────────────┐  │
│  │IterationOptimizer│  │RunDocGenerator   │  │Checkpoint  │  │
│  │ 迭代优化          │  │ 文档生成          │  │Store       │  │
│  └──────────────────┘  └──────────────────┘  └────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

## 核心流程

### 运行生命周期

```
┌──────────────────────────────────────────────────────────────────┐
│                       Run 完整生命周期                             │
│                                                                  │
│  ① create_run()                                                  │
│     │                                                            │
│     ▼                                                            │
│  ② bootstrap — prepare_run_request()                             │
│     │  生成初始步骤计划：                                         │
│     │  bootstrap → analyze → edit → finalize                     │
│     │                                                            │
│     ▼                                                            │
│  ③ SupervisorLoop 主循环                                         │
│     │                                                            │
│     ├── Scheduler.schedule()    ──► 选择下一个步骤               │
│     │                                                            │
│     ├── StepRunner.execute()    ──► 执行步骤                     │
│     │       │                                                    │
│     │       ├── 构建 prompt → QueryEngine → 收集结果             │
│     │       └── 或执行 bash / 委派智能体                         │
│     │                                                            │
│     ├── RunJudge.assess()       ──► 评估健康度                   │
│     │                                                            │
│     ├── PolicyEngine.decide()   ──► 决策                         │
│     │       │                                                    │
│     │       ├── CONTINUE ──► 继续下一轮                          │
│     │       ├── RETRY    ──► 重试当前步骤                        │
│     │       ├── COOLDOWN ──► 等待后继续                          │
│     │       ├── REPLAN   ──► 重新生成步骤计划                    │
│     │       ├── BLOCK    ──► 阻塞，等待人工干预                  │
│     │       ├── FAIL     ──► 标记运行失败                        │
│     │       └── TIME_OUT ──► 超时终止                            │
│     │                                                            │
│     ├── IterationOptimizer      ──► 捕获快照、评分               │
│     │                                                            │
│     ├── RunDocGenerator         ──► 生成文档                     │
│     │                                                            │
│     └── 循环直到完成或终止                                        │
│                                                                  │
│  ④ 运行结束                                                      │
│     ├── SUCCESS: 所有步骤完成                                     │
│     ├── FAILED: 策略引擎判定失败                                  │
│     ├── CANCELLED: 用户取消                                      │
│     └── TIMED_OUT: 超出预算                                      │
└──────────────────────────────────────────────────────────────────┘
```

### 步骤执行流程

```
StepRunner.execute(step)
   │
   ├── 根据 StepKind 选择执行方式
   │
   │   ┌───────────────────────────────────────────┐
   │   │ StepKind                                   │
   │   │                                           │
   │   │ BOOTSTRAP  ──► 初始化分析                  │
   │   │ ANALYZE    ──► 代码分析 / 问题诊断         │
   │   │ EDIT_CODE  ──► 代码编辑                    │
   │   │ RUN_TESTS  ──► 测试执行                    │
   │   │ VERIFY     ──► 验证结果                    │
   │   │ FINALIZE   ──► 收尾 / 清理                 │
   │   └───────────────────────────────────────────┘
   │
   ├── 为步骤分配角色（dispatch_roles）
   │   ├── implementer  → 实现代码
   │   ├── analyzer     → 分析诊断
   │   ├── planner      → 规划设计
   │   ├── verifier     → 验证测试
   │   └── reporter     → 报告总结
   │
   ├── 构建 prompt（包含上下文、经验教训、历史日志）
   │
   ├── 调用 QueryEngine 执行
   │
   └── 收集结果（输出、工具调用、诊断信息）
```

## 核心组件详解

### RunHarness — 运行编排器

| 方法 | 说明 |
|------|------|
| `create_run()` | 创建新运行，生成引导步骤 |
| `resume_run()` | 恢复中断的运行 |
| `cancel_run()` | 取消运行 |
| `list_runs()` | 列出所有运行 |
| `get_run()` | 获取运行详情 |

### Scheduler — 调度器

```
Scheduler
├── 单步调度
│   ├── 优先级排序（依赖满足的步骤优先）
│   └── 选择下一个可执行步骤
│
├── 工作项调度
│   ├── 将步骤分解为工作项
│   └── 按优先级分配
│
└── 只读批量调度
    ├── 识别可并行的只读工作项
    └── 批量分配给后台智能体
```

### StepRunner — 步骤执行器

```
StepRunner.execute(step)
├── 准备执行上下文
│   ├── 加载运行历史
│   ├── 加载经验教训
│   └── 加载审查记录
│
├── 执行方式
│   ├── Query 模式：构建 prompt → QueryEngine
│   ├── Bash 模式：直接执行命令
│   ├── Agent 模式：委派给子智能体
│   └── Background 模式：后台只读智能体
│
├── 结果收集
│   ├── 输出文本
│   ├── 工具调用记录
│   └── 诊断信息（timing、token 用量）
│
└── 生成 StepResult
```

### RunJudge — 裁判

评估运行的整体健康度：

```
RunJudge.assess(run_state)
├── PROGRESSING    # 正常推进中
├── STALLED        # 停滞不前（多次重试无效）
├── BLOCKED        # 被阻塞（外部依赖无法满足）
├── REGRESSING     # 回退（修复引入新问题）
└── 评估维度
    ├── 步骤完成率
    ├── 重试次数
    ├── 错误频率
    └── 上下文变化趋势
```

### PolicyEngine — 策略引擎

根据裁判评估和步骤结果，决定下一步行动：

```
PolicyEngine.decide(assessment, step_result)
├── 输入
│   ├── 健康度评估（RunJudge）
│   ├── 步骤执行结果
│   ├── 运行预算剩余
│   └── 重试历史
│
├── 决策输出
│   ├── CONTINUE   # 继续下一步
│   ├── RETRY      # 重试当前步骤
│   ├── COOLDOWN   # 冷却后继续
│   ├── REPLAN     # 重新规划步骤
│   ├── BLOCK      # 阻塞
│   ├── FAIL       # 标记失败
│   └── TIME_OUT   # 超时
│
└── 决策因素
    ├── 最大重试次数
    ├── 冷却时间
    ├── 预算限制
    └── 连续失败阈值
```

### CheckpointStore — 持久化存储

```
CheckpointStore
├── 持久化内容
│   ├── RunState          # 运行状态
│   ├── Events            # 运行事件流
│   ├── Snapshots         # 文件快照
│   ├── Reviews           # 审查记录
│   ├── TraceSpans        # 追踪数据
│   ├── Artifacts         # 产物文件
│   └── Journal           # 运行日志
│
└── 特性
    ├── 文件级持久化
    ├── 支持跨进程恢复
    └── 增量写入
```

### IterationOptimizer — 迭代优化器

```
IterationOptimizer
├── 捕获快照
│   └── 每次代码变更前记录文件状态
│
├── 评分迭代
│   ├── 比较前后快照
│   ├── 评估变更质量
│   └── 分类结果
│       ├── IMPROVEMENT    # 改进
│       ├── NEUTRAL        # 无变化
│       └── REGRESSION     # 回退
│
└── 自动插入步骤
    └── 例如：EDIT_CODE 后自动插入 RUN_TESTS
```

### RunDocGenerator — 文档生成器

运行结束后自动生成 Markdown 格式的运行报告，包含：

| 内容 | 说明 |
|------|------|
| 运行概要 | 任务描述、状态、耗时 |
| 步骤记录 | 每个步骤的执行详情 |
| 变更摘要 | 文件变更列表 |
| 经验教训 | 自动提取的经验教训 |
| 诊断信息 | 性能指标和 token 用量 |

### DispatchRoles — 角色映射

```
StepKind → Agent Role 映射：

BOOTSTRAP  → planner
ANALYZE    → analyzer
EDIT_CODE  → implementer
RUN_TESTS  → verifier
VERIFY     → verifier
FINALIZE   → reporter
```

## 审计系统

```
audit/
├── core.py           # TaskAuditProfile, TaskAuditResult, TaskAuditRegistry
└── plugins/          # 插件目录
    └── mini_jq/      # 内置审计插件
```

**插件化设计：**

```
TaskAuditRegistry
├── 内置插件
│   └── 自动发现并加载
│
├── 文件系统插件
│   └── 从指定目录加载
│
└── 插件接口
    ├── TaskAuditProfile   # 审计配置
    └── TaskAuditResult    # 审计结果
```

## 数据模型

```
RunState
├── run_id: str
├── status: RunStatus
├── steps: list[Step]
├── budget: RunBudget
├── health: RunHealth
└── events: list[HarnessEvent]

Step
├── kind: StepKind
├── status: StepStatus
├── work_items: list[WorkItem]
├── result: StepResult | None
└── retry_count: int

WorkItem
├── description: str
├── status: WorkItemStatus
├── scope: list[str]
└── result: str | None

RunBudget
├── max_tokens: int
├── max_steps: int
├── max_duration_seconds: int
└── used_tokens / used_steps / elapsed_seconds

RetryPolicy
├── max_retries: int
├── cooldown_seconds: int
└── backoff_multiplier: float
```
