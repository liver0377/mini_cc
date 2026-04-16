# 迭代优化系统设计（MVP）

## 一、目标

本文档定义 Mini Claude Code 的最小迭代优化系统，用于在现有 Harness 之上实现“每一轮都基于上一轮结果进行修正”的闭环。

当前项目已经具备：

- QueryEngine：多轮工具调用与文本生成
- RunHarness：长期运行、Step 编排、Checkpoint
- Policy / Judge：基础的继续、重试、重规划判定
- Run Timeline：观测每个 Run 的状态和步骤

但这些能力还不足以保证“下一轮优于上一轮”。  
MVP 迭代优化系统的目标不是让模型自动变聪明，而是让系统具备以下能力：

- 每轮结束后输出结构化复盘
- 从复盘中提炼下一轮约束
- 将约束注入下一轮 Step
- 比较相邻两轮的效果是否改善

---

## 二、非目标

本阶段不实现以下内容：

- 跨 Run 的长期策略学习
- LLM 驱动的复杂自我反思系统
- 自动权重调优
- 复杂多 Agent 协同优化
- 全自动 prompt evolution

MVP 先采用**规则驱动**方式完成“复盘 -> 约束 -> 下一轮调整”。

---

## 三、核心闭环

最小闭环如下：

```
Step 执行完成
   │
   ▼
采集事实数据（Snapshot）
   │
   ▼
生成复盘结论（Review）
   │
   ▼
提炼下一轮约束（Constraints）
   │
   ▼
改写下一轮 Step / Prompt
   │
   ▼
继续执行并比较效果
```

这一层位于：

- QueryEngine 之上
- SupervisorLoop 之内
- PolicyEngine / RunJudge 之间

---

## 四、总体架构

建议新增模块：

```text
src/mini_cc/harness/
└── iteration.py
```

该模块只包含 MVP 所需的最小对象：

- `IterationOutcome`
- `IterationScore`
- `IterationSnapshot`
- `IterationReview`
- `IterationOptimizer`

### 与现有模块关系

| 模块 | 职责 |
|------|------|
| `StepRunner` | 执行 Step，并提供结果与 artifacts |
| `IterationOptimizer` | 采集 snapshot、生成 review、注入下一轮约束 |
| `PolicyEngine` | 结合 review 决定 continue / retry / replan / block |
| `SupervisorLoop` | 串起 review 流程并持久化 |
| `Run Timeline` | 展示 review 与每轮优化轨迹 |

---

## 五、数据模型

### 1. IterationOutcome

用于标识本轮相较上一轮的总体结论：

- `improved`
- `stalled`
- `regressed`
- `blocked`

### 2. IterationScore

用于量化“这一轮是否更好”。  
MVP 采用规则打分，而非 LLM 判定。

建议字段：

```text
IterationScore
├── total: int
├── test_improvement: int
├── diff_quality: int
├── tool_efficiency: int
├── artifact_signal: int
└── penalty: int
```

只要能够区分“变好 / 原地打转 / 变差”即可，不要求数学上精确。

### 3. IterationSnapshot

表示一轮执行完成后的**事实数据**，不做判断。

建议字段：

```text
IterationSnapshot
├── run_id: str
├── step_id: str
├── step_kind: str
├── success: bool
├── summary: str
├── error: str | None
├── progress_made: bool
├── changed_files: list[str]
├── patch_size: int
├── tool_calls: int
├── tool_failures: int
├── test_summary: str
├── artifact_paths: dict[str, str]
└── metadata: dict[str, str]
```

### 4. IterationReview

表示系统对该轮的结构化复盘结论。

建议字段：

```text
IterationReview
├── outcome: IterationOutcome
├── score: IterationScore
├── root_cause: str
├── useful_actions: list[str]
├── wasted_actions: list[str]
├── next_constraints: list[str]
├── next_recommended_step: str
└── metadata: dict[str, str]
```

其中最关键的是：

- `root_cause`
- `next_constraints`
- `next_recommended_step`

---

## 六、IterationOptimizer 设计

`IterationOptimizer` 负责三件事：

1. 从 `StepResult + RunState + artifacts` 采集 `IterationSnapshot`
2. 将当前 snapshot 与上一轮 snapshot 比较，生成 `IterationReview`
3. 将 review 中的约束注入下一轮 Step

当前已落地的最小自动化规则：

- `EDIT_CODE` 成功后自动补一个 `RUN_TESTS`
- `RUN_TESTS` 失败后自动补一个 `INSPECT_FAILURES`
- 每个 step 执行完后都会将 snapshot、review 和 journal 追加到 run 目录
- 系统 prompt 会回灌最近 run 的 review/journal tail，step 执行时优先使用当前 run 的上下文

建议接口：

```python
class IterationOptimizer:
    def capture(
        self,
        run_state: RunState,
        step: Step,
        result: StepResult,
    ) -> IterationSnapshot: ...

    def review(
        self,
        current: IterationSnapshot,
        previous: IterationSnapshot | None,
    ) -> IterationReview: ...

    def apply_review(
        self,
        run_state: RunState,
        step: Step,
        review: IterationReview,
    ) -> list[Step]: ...
```

---

## 七、Snapshot 采集规则

MVP 不要求复杂静态分析，只采集最关键的信号。

### 可直接采集的数据

从 `StepResult` 获得：

- `success`
- `summary`
- `error`
- `progress_made`
- `artifact_paths`

从 `RunState` 获得：

- `run_id`
- `current_step_id`
- 当前 step kind

### 可附加采集的数据

#### 1. 变更文件

在代码修改轮后执行：

```bash
git diff --name-only
```

#### 2. Patch 大小

在代码修改轮后执行：

```bash
git diff --stat
```

或直接按 diff 文本长度计算。

#### 3. 测试摘要

从 `run_tests` 这类 step 的 artifact 中提取：

- 失败数
- 通过数
- 首个错误摘要

#### 4. 工具调用统计

MVP 可先从已有 `tool_events` 或 `ToolResultEvent` 聚合获得：

- 工具调用次数
- 工具失败次数

---

## 八、Review 生成规则

当前使用规则生成 review，不依赖 LLM。

### 1. Outcome 判定

| 条件 | 结论 |
|------|------|
| 相同错误重复出现 | `blocked` |
| 失败 且无进展 | `regressed` |
| 成功 且无进展 | `stalled` |
| 首个 Step | `improved`（若成功）或 `stalled`（若成功但无进展） |
| score > 0 | `improved` |
| score < 0 | `regressed` |
| score = 0 | `stalled` |

### 2. Root Cause 生成

使用模板化规则，根据 Step 类型和结果生成。

示例：

- `"未先定位最小失败测试，导致验证成本过高"`
- `"修改范围过大，但没有带来测试改善"`
- `"重复执行相同命令，未获取新信息"`
- `"未读取关键源码文件即开始修改"`

### 3. Useful Actions / Wasted Actions

从 snapshot 中提炼：

**useful_actions**

- 成功运行了相关测试
- 缩小了改动范围
- 读取到了关键文件

**wasted_actions**

- 重复运行相同命令
- 修改文件过多
- 工具失败率过高

### 4. Next Constraints

这是 MVP 最关键的输出。

约束示例：

- 先读取 `src/mini_cc/query_engine/engine.py`
- 只允许修改 2 个文件
- 先跑相关单测，不跑全量测试
- 不要重复执行相同 bash 命令
- 先查看最近失败日志，再修改代码

---

## 九、评分规则

当前已落地的评分系统采用六信号加权求和。

### 评分信号

| 信号 | 说明 | 得分 |
|------|------|------|
| success_signal | Step 是否成功 | +3 或 0 |
| progress_signal | 是否有实质进展（progress_made） | +2 或 0 |
| verification_signal | 测试通过数 - 失败数 - 错误数；成功测试轮额外 +2 | 整数范围 |
| artifact_signal | 是否产生了工件 | +1 或 0 |
| comparison_signal | 与上一步对比：从失败恢复 +2，测试失败数改善按 delta 计，重复相同错误 -1 | -1 ~ +2 |
| penalty | 是否有错误 | 0 或 -2 |

最终：`total = success_signal + progress_signal + verification_signal + artifact_signal + comparison_signal - penalty`

只要支持相邻两轮比较即可。

---

## 十、下一轮约束注入机制

MVP 不修改 system prompt 生成逻辑，直接把约束注入 step prompt。

### 注入方式

如果 review 产出：

```text
next_constraints = [
  "先读取 src/mini_cc/query_engine/engine.py",
  "只允许修改 2 个文件",
  "不要运行全量测试"
]
```

则下一轮 `EDIT_CODE` step 的输入 prompt 改写为：

```text
上轮复盘：
- 根因：修改范围过大，未先确认关键失败位置

本轮约束：
1. 先读取 src/mini_cc/query_engine/engine.py
2. 只允许修改 2 个文件
3. 不要运行全量测试

用户目标：
<original goal>
```

这样可以在不重构 PromptBuilder 的前提下完成最小闭环。

---

## 十一、与现有 Harness 的集成点

### 1. StepRunner

在 step 完成后，提供足够的 snapshot 原始数据：

- `StepResult`
- artifact 路径
- 可选 diff/test/tool 统计

### 2. SupervisorLoop

在 `result = await step_runner.run_step(...)` 之后插入：

1. `snapshot = optimizer.capture(...)`
2. `review = optimizer.review(snapshot, previous_snapshot)`
3. 保存 review artifact
4. 基于 review 调整下一轮 step
5. 再交给 `PolicyEngine` 做继续 / 重试 / 阻塞判定

### 3. PolicyEngine

增加对 `IterationReview.outcome` 的利用：

- `improved` -> `continue`
- `stalled` -> 插入 `inspect_failures`
- `regressed` -> 插入 `make_plan`
- `blocked` -> 转 `blocked`

### 4. Run Timeline

在 Run Timeline 中增加：

- 最近 review 结果
- 每轮 score
- next constraints

---

## 十二、Artifact 持久化

建议每轮保存两个文件：

```text
.mini_cc/runs/<run_id>/artifacts/
├── iteration-step-0003-snapshot.json
└── iteration-step-0003-review.json
```

### snapshot artifact

保存事实数据，便于后续分析。

### review artifact

保存复盘结论，便于：

- Run Timeline 展示
- 调试优化策略
- 后续引入更强策略时复用历史数据

---

## 十三、MVP 执行流程

```
执行 Step
  └── 返回 StepResult
        └── capture -> IterationSnapshot
              └── review -> IterationReview
                    ├── 保存 snapshot/review artifacts
                    ├── 生成 next constraints
                    ├── 修改下一轮 Step
                    └── 交给 PolicyEngine 做最终判定
```

---

## 十四、一个典型示例

目标：修复某个 failing test。

### 第 1 轮

- 修改了 6 个文件
- 跑了全量测试
- 失败仍然相同

Review：

- outcome: `regressed`
- root_cause: `"修改范围过大，但未改善测试结果"`
- next_constraints:
  - `"只修改 1-2 个相关文件"`
  - `"先定位最小失败测试"`
  - `"不要运行全量测试"`

### 第 2 轮

系统把这些约束注入下一轮 `EDIT_CODE` step，下一轮行为收敛为：

- 先读失败测试相关文件
- 只修改目标模块
- 只跑相关单测

如果测试结果改善，则下一轮 review 为 `improved`。

---

## 十五、实现优先级

建议按以下顺序实现：

### Step 1

新增 `iteration.py`，定义：

- `IterationOutcome`
- `IterationScore`
- `IterationSnapshot`
- `IterationReview`
- `IterationOptimizer`

### Step 2

在 `SupervisorLoop` 中接入：

- `capture`
- `review`
- snapshot/review artifact 持久化

### Step 3

实现最小的 `next_constraints` 注入：

- 先作用于 `EDIT_CODE`
- 再扩展到 `MAKE_PLAN` / `INSPECT_FAILURES`

### Step 4

让 `PolicyEngine` 使用 review outcome

### Step 5

在 Run Timeline 中展示 review

---

## 十六、成功标准

如果 MVP 满足以下条件，则认为迭代优化系统成立：

- 每轮都有结构化 snapshot 与 review
- review 会影响下一轮 step
- 系统不再只会机械重试
- 能区分 `improved / stalled / regressed / blocked`
- Run Timeline 能展示每轮复盘结果

---

## 十七、总结

最小迭代优化系统的核心不是复杂智能，而是把多轮执行改造成一个明确闭环：

- 采集事实
- 结构化复盘
- 注入下一轮约束
- 比较效果是否提升

在现有 Mini Claude Code 架构中，这一层最适合落在 Harness 内部，以最小代价把“多轮运行”提升为“多轮优化”。
