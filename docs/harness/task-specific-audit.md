# Task-Specific Audit 设计

## 一、背景

当前 Harness 已具备较完整的通用运行审计能力：

- `IterationSnapshot`：记录每个 Step 的事实快照
- `IterationReview`：记录每个 Step 的结构化复盘
- `RunJudge`：判断当前 Run 的健康度
- `Documentation.md`：输出终态总结、关键决策和经验教训

这套机制足以回答下列通用问题：

- 当前 Step 是否成功
- 这轮是否产生了进展
- 测试是否通过
- Run 是否处于 stalled / regressing / blocked

但它仍然无法回答更关键的**任务特定问题**：

- 对于一个 `mini-jq` 任务，当前支持了哪些 `jq` 子集？
- 当前失败是“工程失败”还是“语义兼容失败”？
- 这轮代码修改有没有真正提高 `jq` 语义对齐度？
- 当前 Run 离“任务完成”还差哪些明确能力点？

因此，需要在通用审计之上增加一层 **Task-Specific Audit**，把“任务目标完成度”变成一等公民。

---

## 二、目标

### 核心目标

- 在 Harness 中引入一套可插拔的任务专项审计框架
- 让不同任务拥有不同的审计维度、成功标准和失败分类
- 让 `IterationReview` 和 `RunJudge` 不再只依赖通用信号，而能消费领域信号
- 让 `Documentation.md` 能输出“任务专项完成图谱”
- 为 `mini-jq` 这类目标导向验证任务提供首个落地 profile

### 非目标

- 不在第一版中做通用 benchmark 平台
- 不要求所有任务都必须有 task-specific audit
- 不把领域判断全部交给 LLM 推断
- 不引入复杂的 DSL 或远程评测基础设施

---

## 三、为什么需要 Task-Specific Audit

### 通用审计的局限

以 `mini-jq` 为例，当前系统最多只能知道：

- `pytest` 是否通过
- 某个 Step 是否返回 `success=True`
- 某轮是否有 `progress_made=True`

但对于通用性验证来说，这些信号不够。

例如：

1. 单元测试通过了，但 `mini-jq` 仍和真实 `jq` 在关键语义上不一致  
2. LLM 修改了很多代码，但支持的 filter 子集没有扩展  
3. 某轮测试失败数没变化，但错误类型从 parser failure 收敛到 runtime mismatch，这其实是进展  
4. 某轮没有新增代码，但补齐了 golden cases 和差异定位能力，这也是有效推进

也就是说，系统需要的不只是“代码改没改好”，而是“任务定义完成（Definition of Done）推进了多少”。

### 对通用性验证项目的意义

本项目当前的目标不是仅验证“Agent 能否写代码”，而是验证：

- 系统能否围绕一个明确目标持续迭代
- 系统能否根据领域反馈自我修正
- 系统能否形成稳定、可审计、可比较的闭环

Task-Specific Audit 正是这个闭环的关键补充层。

---

## 四、设计原则

### 1. 在通用骨架上扩展，而不是另起一套审计系统

Task-Specific Audit 不应替代现有 `IterationSnapshot` / `IterationReview` / `RunJudge` / `Documentation.md`。它应作为附加层嵌入现有流程。

### 2. 结构化优先，避免依赖终端文本启发式解析

专项审计的核心结果应由结构化 artifact 提供，例如 `jq_audit.json`，而不是让系统反复从自然语言输出中猜测。

### 3. 任务定义完成度应可比较

专项审计结果必须支持跨 Step、跨 Run 比较，否则无法判断是否真正改善。

### 4. 支持按 profile 插拔

不同任务应共享同一套接口，但各自拥有独立的审计维度与门槛。

### 5. 先聚焦单任务 profile，再抽象平台

第一版只需要把 `mini-jq` 跑通，不追求一开始就覆盖所有任务类型。

---

## 五、核心概念

### 1. Audit Profile

每类任务对应一个 `TaskAuditProfile`，它定义：

- profile id
- 审计维度
- 审计产物格式
- 成功门槛
- 进展判断规则
- 文档展示方式

例如：

- `mini_jq`
- `mini_sql`
- `mini_regex`
- `refactor_safety`

### 2. Task Audit Result

每次专项审计 Step 产出一个结构化结果 `TaskAuditResult`，表示当前任务在该 profile 下的真实状态。

### 3. Task Audit Judge

根据当前审计结果与上一次审计结果，判断：

- 是否改善
- 改善发生在哪些维度
- 当前 blocker 是什么
- 下一步最合理的动作是什么

### 4. Task Audit Section

在 `Documentation.md` 中增加单独的“任务专项审计”段落，展示 profile 关心的完成度信息。

---

## 六、总体架构

```text
RunHarness
  │
  ▼
SupervisorLoop
  │
  ├── 普通 Step（MAKE_PLAN / EDIT_CODE / RUN_TESTS / FINALIZE）
  ├── 专项审计 Step（RUN_TASK_AUDIT）
  │
  ├── StepRunner 执行专项审计命令/脚本
  │     └── 生成结构化 artifact（如 jq_audit.json）
  │
  ├── IterationOptimizer.capture()
  │     └── 读取 TaskAuditResult，写入 snapshot.metadata
  │
  ├── IterationOptimizer.review()
  │     └── 用 profile-specific 规则补充 score / outcome / root_cause
  │
  ├── RunJudge
  │     └── 结合 task-specific signal 判断 RunHealth
  │
  └── RunDocGenerator
        └── 输出“任务专项审计”段落
```

---

## 七、数据模型设计

### 1. RunState 扩展

建议在 `RunState.metadata` 中加入：

```text
metadata["audit_profile"] = "mini_jq"
metadata["audit_mode"] = "enabled"
metadata["task_definition"] = "实现 mini-jq 的核心子集"
```

第一版无需修改 `RunState` 主模型字段，先通过 `metadata` 传递 profile 选择即可。

### 2. TaskAuditProfile

建议新增模型：

```text
TaskAuditProfile
├── profile_id: str
├── display_name: str
├── summary: str
├── dimensions: list[str]
├── required_artifact: str                 # 例如 jq_audit.json
├── success_thresholds: dict[str, str]
└── doc_sections: list[str]
```

### 3. TaskAuditResult

建议新增模型：

```text
TaskAuditResult
├── profile_id: str
├── summary: str
├── score_total: int | None
├── dimensions: dict[str, str | int | bool]
├── blockers: list[str]
├── regressions: list[str]
├── improvements: list[str]
├── recommended_next_focus: str | None
└── raw_artifact_path: str | None
```

### 4. IterationSnapshot 扩展策略

不建议第一版直接大改 `IterationSnapshot` 顶层字段。  
建议先把 task-specific 结果写入：

```text
IterationSnapshot.metadata
├── audit_profile
├── audit_summary
├── audit_score_total
├── audit_cases_total
├── audit_cases_passed
├── audit_cases_failed
└── ...
```

等 profile 稳定后，再考虑是否把常用字段提升为一级字段。

### 5. IterationReview 扩展策略

建议在 `IterationReview.metadata` 中记录：

```text
metadata["audit_profile"] = "mini_jq"
metadata["audit_progress"] = "semantic_parity_improved"
metadata["audit_blocker"] = "array iterator not implemented"
```

这样既不破坏现有 review 结构，也能让后续文档生成器读取专项信息。

---

## 八、Step 设计

### 新增 StepKind：RUN_TASK_AUDIT

建议新增专项审计 Step：

```text
RUN_TASK_AUDIT
├── title: "Run Task Audit"
├── goal: "Execute the configured task-specific audit and produce a structured artifact."
├── inputs["profile"]: "mini_jq"
├── inputs["command"]: "uv run python scripts/task_audit/mini_jq.py"
└── artifacts["task_audit"]: ".mini_cc/runs/<run_id>/artifacts/jq_audit.json"
```

### 为什么要单独一个 Step

不建议把专项审计完全塞进 `RUN_TESTS`，原因有三点：

1. 测试通过不等于任务达成  
2. 专项审计通常会产出独立 artifact  
3. 在 timeline 中单独展示 `RUN_TASK_AUDIT` 更利于观察“测试正确性”和“任务完成度”这两种信号

### 插入时机

对大多数面向实现的任务，建议：

```text
MAKE_PLAN
  → EDIT_CODE
  → RUN_TESTS
  → RUN_TASK_AUDIT
  → FINALIZE
```

如果 `EDIT_CODE` 后无测试或无专项产物，可由 Policy / IterationOptimizer 决定是否跳过。

---

## 九、Profile 注册机制

建议新增 `TaskAuditRegistry`，负责：

- 注册 profile
- 根据 `run_state.metadata["audit_profile"]` 解析当前 profile
- 返回对应的 artifact parser、judge 和文档 renderer

### 建议模块布局

```text
src/mini_cc/harness/
├── task_audit.py                 # Profile / Result / Registry 抽象
├── task_audit_profiles/
│   ├── __init__.py
│   └── mini_jq.py               # mini-jq profile
```

### 核心接口建议

```text
TaskAuditProfile
  ├── parse_result(artifact_path) -> TaskAuditResult
  ├── compare(current, previous) -> AuditDelta
  ├── summarize_for_review(result, delta) -> ReviewHints
  └── render_doc_section(result, previous) -> str
```

第一版不必一次实现全部方法，但接口方向应尽早固定。

---

## 十、与 Iteration 系统的集成

### capture 阶段

`IterationOptimizer.capture()` 在处理 `RUN_TASK_AUDIT` 结果时：

1. 识别当前 `audit_profile`
2. 找到专项审计 artifact
3. 由对应 profile 解析成 `TaskAuditResult`
4. 将关键字段写入 `snapshot.metadata`

例如：

```text
metadata["audit_profile"] = "mini_jq"
metadata["audit_summary"] = "31/42 jq semantic cases passed"
metadata["audit_cases_total"] = "42"
metadata["audit_cases_passed"] = "31"
metadata["audit_cases_failed"] = "11"
metadata["audit_blocker"] = "array iterator syntax missing"
```

### review 阶段

`IterationOptimizer.review()` 在通用 review 之外，附加 task-specific 判断：

- 若 `cases_passed` 上升，记为改善
- 若失败数相同，但 blocker 从 parser error 下降到 runtime mismatch，也记为改善
- 若测试通过但专项审计无变化，记为 stalled
- 若专项兼容性下降，记为 regressed

这样可以避免“工程测试绿了，但任务目标没动”的误判。

---

## 十一、与 RunJudge / PolicyEngine 的集成

### RunJudge

当前 `RunJudge` 只看：

- `result.progress_made`
- `result.success`
- 失败计数
- 无进展计数

引入 task-specific audit 后，建议补充：

- 若专项审计显示能力覆盖提升，优先视为 `PROGRESSING`
- 若连续多轮 audit 指标不变，视为 `STALLED`
- 若专项审计关键指标下降，视为 `REGRESSING`
- 若 blocker 连续重复且无改善，视为 `BLOCKED`

### PolicyEngine

Policy 不需要理解具体领域细节，但可以消费 `RunJudge` 的结果和 `IterationReview.metadata` 中的 task-specific 提示：

- `audit_blocker`
- `audit_next_focus`
- `audit_profile`

从而在 REPLAN 时插入更贴近任务目标的 Step。

---

## 十二、Documentation.md 扩展

建议在 `Documentation.md` 中新增一节：

```text
## 任务专项审计

| 项目 | 值 |
|------|------|
| Profile | mini_jq |
| 总用例 | 42 |
| 通过 / 失败 | 31 / 11 |
| 当前阶段 | 基础 field access 已稳定，array iterator 未完成 |
| 主要 blocker | parser 未支持 `.[]` |
| 下一步重点 | 优先补 parser AST 与 evaluator 的 iterator 语义 |

### 能力覆盖

- `.`：通过
- `.foo`：通过
- `.foo.bar`：通过
- `.[0]`：通过
- `.[]`：失败
- `|`：部分通过

### 与目标基准的差异

- 对非数组输入执行 `.[]` 时错误语义不一致
- 对缺失字段返回值与 `jq` 不一致
```

这样 `Documentation.md` 就不再只是“本轮做了什么”，而是能回答“任务现在做到什么程度”。

---

## 十三、System Prompt 注入策略

Task-Specific Audit 的目标不是直接把整个专项审计结果塞进 prompt，而是提炼出最有用的部分：

- 当前 profile
- 当前 blocker
- 已完成能力点
- 下一步禁止重复踩坑的约束

建议后续在 `run_context` 中增加：

```text
Task audit profile: mini_jq
Current audit summary: 31/42 semantic cases passed
Current blocker: array iterator syntax is not implemented
Recent audit lessons:
- field access and index access are already stable
- do not expand scope beyond parser + evaluator
```

这部分不要求第一版立即实现，但设计上应与 `Documentation.md` 对齐。

---

## 十四、mini-jq Profile 设计

### 任务定义

`mini-jq` 的目标不是完整复刻 `jq`，而是实现一个可明确界定范围的子集。

第一版建议聚焦：

- identity：`.`
- 字段访问：`.foo`
- 嵌套字段访问：`.foo.bar`
- 数组索引：`.[0]`
- 基础管道：`|`
- 基础 array/object 输入处理

### 审计维度

建议 `mini_jq` profile 包含以下维度：

1. `filter_coverage`
- 已支持哪些 filter 语法子集

2. `semantic_parity`
- 与真实 `jq` 的输出一致度

3. `cli_contract`
- stdout / stderr / exit code 是否符合预期

4. `error_contract`
- 非法 JSON、非法 filter、类型不匹配时的行为是否稳定

5. `regression_guard`
- 已通过能力点是否回退

### 推荐 artifact 格式

建议专项审计脚本产出 `jq_audit.json`：

```json
{
  "profile": "mini_jq",
  "summary": {
    "cases_total": 42,
    "cases_passed": 31,
    "cases_failed": 11
  },
  "coverage": {
    "identity": true,
    "field_access": true,
    "nested_field_access": true,
    "array_index": true,
    "array_iterator": false,
    "pipe": "partial"
  },
  "blockers": [
    "parser does not support array iterator syntax"
  ],
  "regressions": [],
  "improvements": [
    "field access parity is stable",
    "index access parity improved from 4/10 to 9/10"
  ],
  "recommended_next_focus": "parser_and_evaluator_for_array_iterator"
}
```

这类 artifact 应作为 Harness 的专项真相源。

---

## 十五、落地顺序

建议按以下顺序实现：

### 第一阶段：最小闭环

1. `RunState.metadata` 支持 `audit_profile`
2. 新增 `RUN_TASK_AUDIT` step kind
3. 新增 `TaskAuditProfile` / `TaskAuditResult` 抽象
4. 做第一个 `mini_jq` profile
5. 让专项审计产出结构化 JSON artifact
6. 在 `Documentation.md` 中渲染 `## 任务专项审计`

### 第二阶段：接入 Iteration / Judge

1. `IterationOptimizer.capture()` 解析专项 artifact
2. `IterationOptimizer.review()` 引入专项信号
3. `RunJudge` 消费专项进展
4. `PolicyEngine` 在 REPLAN 时利用 `audit_next_focus`

### 第三阶段：跨 Run 记忆

1. 从 `Documentation.md` 中提取 task-specific lessons
2. 注入后续 Run 的 `run_context`
3. 支持同一 profile 的跨 Run 比较

---

## 十六、开放问题

### 1. TaskAuditResult 放在 metadata 还是单独模型？

第一版建议放 metadata，避免重构范围过大。  
如果 profile 数量变多、字段变稳定，再上升为一级模型。

### 2. 专项审计是否必须是独立 Step？

建议是。  
如果混入 `RUN_TESTS`，timeline、artifact 管理和后续策略都会变得模糊。

### 3. 专项审计脚本由谁维护？

建议由 repo 内脚本维护，而不是完全依赖 LLM 临时生成。  
原因是专项审计必须稳定、可回归、可比较。

### 4. 是否允许不同任务使用不同 artifact 结构？

允许。  
统一的是 profile 接口，而不是所有 artifact 的 JSON 字段必须完全一致。

---

## 十七、成功标准

当系统满足以下条件时，可认为 Task-Specific Audit 设计成立：

- Harness 能识别当前 Run 的 `audit_profile`
- 系统可以执行专项审计 Step 并生成结构化 artifact
- `IterationReview` 能反映 task-specific 进展，而非只反映通用测试结果
- `RunJudge` 能根据任务专项信号识别 progressing / stalled / blocked
- `Documentation.md` 能展示任务完成度、blocker 和下一步重点
- 对 `mini-jq` 这类任务，系统能明确回答“当前支持了什么、还缺什么、下一步该做什么”

---

## 十八、与现有 Harness 文档的关系

本设计是 [Harness 设计](./design.md) 的补充，不替代 Harness 主文档。

- `docs/harness/design.md` 负责描述通用运行控制面
- 本文负责描述“如何在通用 Harness 上挂任务专项审计层”

两者的边界应保持清晰：

- Harness 主文档关注 Run / Step / Policy / Resume / Documentation 通用机制
- Task-Specific Audit 文档关注 profile、专项 artifact、专项 judge 和任务完成度表示
