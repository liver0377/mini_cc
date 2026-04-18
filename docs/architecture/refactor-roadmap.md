# Mini Claude Code 架构重构路线图

## 一、目标

本路线图用于把当前项目从“功能可用、边界开始松动”的状态，推进到“边界清晰、约束可验证、后续功能可持续演进”的状态。

本路线图解决的不是单点 bug，而是以下系统性问题：

- `create_engine()` 过重，装配层与运行策略耦合
- `QueryEngine` 职责过胖，内部混合多种编排语义
- 多 Agent 的 `readonly` / `scope` 主要停留在提示词约定，缺乏执行期强约束
- 写 Agent 直接改主工作区，但并发一致性机制不足
- `TaskService` 更像轻量文件存储，不像可靠的编排状态机
- 文档中的目标架构与当前代码事实开始漂移

## 二、重构原则

### 1. 先收紧边界，再扩展功能

在完成运行时边界收敛前，暂停继续扩展以下方向：

- 更多子 Agent 能力
- 更复杂的自动派工
- 更复杂的 harness 编排
- 更多工具类型

### 2. 先做强约束，再做智能策略

优先级顺序应为：

1. 执行期约束
2. 状态模型清晰化
3. 依赖反转
4. 调度智能化

### 3. 每一阶段都必须有“完成判定”

重构不是“代码看起来更舒服”，而是：

- 模块职责更清晰
- 关键约束可测试
- 文档与实现一致
- 后续阶段能在更稳定的基础上继续推进

## 三、阶段划分

路线图分四个阶段，建议严格按顺序推进。

### Phase 1：收紧运行时边界

目标：

- 把当前最危险的设计缺口先补上
- 让“只读”“作用域”“主/子 Agent 边界”从软约束变成硬约束

#### 1.1 收紧工具权限模型

当前问题：

- readonly agent 仍然拥有 `bash`
- `file_edit` / `file_write` 不校验 scope
- `scope_paths` 只在调度层检查，不在执行层生效

改造目标：

- 定义统一的执行约束模型，例如 `ExecutionPolicy` / `ToolExecutionGuard`
- 在工具执行前统一检查：
  - 当前 Agent 是否只读
  - 当前工具是否允许
  - 当前文件路径是否落在声明 scope 内
  - bash 是否允许执行，以及允许的能力范围

涉及模块：

- `src/mini_cc/runtime/execution/executor.py`
- `src/mini_cc/tools/file_read.py`
- `src/mini_cc/tools/file_edit.py`
- `src/mini_cc/tools/file_write.py`
- `src/mini_cc/tools/bash.py`
- `src/mini_cc/runtime/agents/manager.py`
- `src/mini_cc/context/engine_context.py`

建议落地方式：

- 新增执行期策略对象，由 `AgentManager` / 主引擎在创建 executor 时注入
- 不再依赖 prompt 告知 agent “不要越界”，而是在 executor 层拒绝执行
- 对 readonly agent 默认移除 `bash`，或提供明确的只读 shell 模式并限制命令白名单

完成判定：

- readonly agent 无法通过任何工具修改文件
- write agent 对 scope 外路径写入会失败
- 对上述约束补齐单元测试和集成测试

#### 1.2 明确主 Agent 与子 Agent 的隔离语义

当前问题：

- 写 Agent 直接改主工作区
- 只读 Agent 和写 Agent 可并发运行，但一致性机制不足
- stale 标记只是结果提示，不是执行期协调机制

改造目标：

- 在设计文档中明确支持的并发模型，而不是维持模糊状态

可选方案：

1. 保持直写主工作区，但补齐文件级锁与严格 scope guard
2. 写 Agent 改为 worktree/分支隔离，再由主 Agent 合并结果

建议：

- 短期采用方案 1，先收紧一致性
- 中期评估方案 2，把“安全隔离”与“易用性”分开处理

涉及模块：

- `src/mini_cc/runtime/agents/manager.py`
- `src/mini_cc/runtime/execution/executor.py`
- `src/mini_cc/runtime/agents/snapshot.py`
- `docs/multi-agent/infrastructure.md`

完成判定：

- 文档中明确当前支持的隔离模型
- 同文件读写冲突有执行期处理策略
- `docs/TODO.md` 中相关条目被迁移或收敛

### Phase 2：拆分 Query Runtime 的职责

目标：

- 把 `QueryEngine` 从“大一统循环”拆成可组合对象
- 让 query loop、agent completion 回流、compact、turn state 分开演进

#### 2.1 收敛 `QueryEngine` 的职责

当前问题：

- `QueryEngine` 同时负责：
  - 用户消息提交
  - LLM 流处理
  - tool loop
  - 自动/反应式压缩
  - 子 Agent 完成结果回流
  - turn tracking

改造目标：

- `QueryEngine` 只保留“对一轮 query 的顶层驱动”
- 其余能力拆成独立协作者

建议拆分：

- `TurnDriver`：负责一次 assistant turn 的流式处理
- `ToolLoopRunner`：负责 tool call 收集与执行
- `AgentCompletionCoordinator`：负责后台 agent completion 的等待与结果回流
- `CompactionController`：负责 auto/reactive compact 决策
- `QueryRecorder`：负责 turn record 与 tracing

涉及模块：

- `src/mini_cc/runtime/query/engine.py`
- `src/mini_cc/context/tool_use.py`
- `src/mini_cc/models/events.py`
- `src/mini_cc/models/query.py`

完成判定：

- `QueryEngine` 构造参数减少
- `_query_loop()` 明显缩短
- 子 Agent 回流逻辑不再直接伪装成普通 user message 拼接

#### 2.2 区分“用户消息”“系统注入”“内部回流”

当前问题：

- 子 Agent 完成结果当前通过 `Message(role=Role.USER, ...)` 重新注入
- 这会污染对话语义，也会影响 memory/compression

改造目标：

- 引入明确的内部消息类型，或在 `Message` 中加入 source / channel 元数据

建议：

- 至少区分：
  - `user`
  - `assistant`
  - `tool`
  - `system-injected`
  - `agent-summary`

涉及模块：

- `src/mini_cc/models/message.py`
- `src/mini_cc/models/query.py`
- `src/mini_cc/runtime/query/engine.py`
- `src/mini_cc/features/compression/compressor.py`
- `src/mini_cc/features/memory/extractor.py`

完成判定：

- memory/compression 不再把 agent completion summary 当作普通用户话语处理
- UI 能区分内部回流消息与真实用户输入

### Phase 3：收敛装配层与依赖反转

目标：

- 让 `context` 回归“上下文构建”
- 让运行时依赖通过接口拼装，而不是通过巨型工厂硬连线

#### 3.1 拆分 `create_engine()`

当前问题：

- `create_engine()` 既是配置入口，又是服务定位器，又包含运行策略
- 还通过闭包和 `ctx_ref` 修补相互依赖

改造目标：

- 拆成清晰的 composition root

建议拆分：

- `EngineConfigLoader`：环境变量 / `.env` / CLI 参数
- `ProviderFactory`：LLM provider 创建
- `ToolingFactory`：tool registry + executor
- `AgentRuntimeFactory`：agent manager + dispatcher + event bus
- `EngineAssembler`：最终组装 `EngineContext`

涉及模块：

- `src/mini_cc/context/engine_context.py`
- `src/mini_cc/providers/base.py`
- `src/mini_cc/providers/openai.py`
- `src/mini_cc/tools/__init__.py`

完成判定：

- `create_engine()` 变为薄封装或被更明确的 assembler 替代
- 不再直接写入其他对象私有字段
- 不再依赖 `ctx_ref` 这种闭包回填技巧

#### 3.2 真正落实 provider 抽象

当前问题：

- 虽然有 `LLMProvider` Protocol，但当前装配明显只围绕 OpenAI provider 设计
- 许多策略直接绑定在 `provider.stream`

改造目标：

- provider 只负责模型交互
- 压缩、记忆提取、query runtime 不直接绑死某个 provider 实现细节

建议：

- 在 assembler 层统一注入 `LLMProvider`
- 横切能力只依赖 provider 协议，不依赖具体类

完成判定：

- 更换 provider 时无需修改 runtime 核心逻辑
- provider 相关测试不依赖 `OpenAIProvider` 的具体行为

### Phase 4：升级任务与编排模型

目标：

- 把当前 task 从“文件化状态记录”提升为“可靠编排基础设施”
- 收敛 runtime 和 harness 之间的语义边界

#### 4.1 重做 `TaskService` 的并发语义

当前问题：

- `_next_id` 生成不具备可靠并发安全
- `_with_lock()` 使用阻塞式 `time.sleep()`
- `metadata` 更新策略粗糙
- task 更像 JSON 存储，不像状态机

改造目标：

- 明确 task 是：
  - 仅本地 session 追踪
  - 还是 harness / agent 的统一编排底座

建议：

- 如果只做本地 session 追踪：
  - 保持轻量，但修复并发和状态更新语义
- 如果做统一编排底座：
  - 引入显式状态迁移规则
  - 引入 append-only event log 或 revision 字段
  - 避免直接全量覆盖 metadata

涉及模块：

- `src/mini_cc/task/service.py`
- `src/mini_cc/models/task.py`
- `docs/multi-agent/task.md`

完成判定：

- task id 分配具备明确并发安全策略
- 状态迁移可验证
- task 文档中写清楚“这是记录层还是编排层”

#### 4.2 收敛 runtime 与 harness 的关系

当前问题：

- 仓库里存在两条编排演进线：
  - 交互式 runtime
  - 长时运行 harness
- 两边共享了一部分模型和事件，但边界还不彻底

改造目标：

- 定义明确的分工：
  - runtime 负责“单次交互执行”
  - harness 负责“跨 step / 跨轮 / 可恢复编排”

建议：

- harness 不直接侵入 runtime 内部状态
- runtime 暴露可复用执行接口给 harness
- 把当前跨层共享但语义模糊的对象重新分类

涉及模块：

- `src/mini_cc/runtime/**`
- `src/mini_cc/harness/**`
- `docs/harness/design.md`
- `docs/architecture/final-layout.md`

完成判定：

- runtime 与 harness 的依赖方向在文档中明确
- 新功能开发时能明确知道该放在哪一层

## 四、推荐执行顺序

建议按以下顺序拆分任务：

1. 先做执行期约束
2. 再做 QueryEngine 拆分
3. 再做 composition root 重构
4. 最后升级 task / harness 编排模型

原因：

- Phase 1 直接降低错误修改和并发风险
- Phase 2 先降低 runtime 内部复杂度
- Phase 3 再把依赖关系理顺，避免一边拆一边继续耦合
- Phase 4 属于更高层的系统收敛，应建立在下层已经稳定的前提上

## 五、建议拆分为 issue / PR 的粒度

不建议一次性大重构。建议拆成以下 PR 粒度：

1. readonly / scope 执行期强约束
2. `bash` 权限模型收紧
3. 文件级并发控制或明确隔离模型
4. `QueryEngine` 中 agent completion 回流逻辑剥离
5. `QueryEngine` 中 compact 控制器剥离
6. `create_engine()` 拆分为 assembler + factories
7. `TaskService` 并发安全修复
8. runtime / harness 边界文档收敛
9. 文档与实现一致性清理

每个 PR 都应包含：

- 设计文档更新
- 对应测试补充
- 完成判定清单

## 六、配套文档更新要求

本路线图推进过程中，以下文档需要同步维护：

- `docs/README.md`
- `docs/architecture/final-layout.md`
- `docs/agent-loop/query-engine.md`
- `docs/multi-agent/agent.md`
- `docs/multi-agent/infrastructure.md`
- `docs/multi-agent/task.md`
- `docs/harness/design.md`
- `docs/TODO.md`

更新原则：

- 文档必须描述“当前事实”与“目标状态”，不要只写目标
- 如果设计已变更但代码未落地，必须明确标记“planned”
- 如果某项 TODO 已经进入正式路线图，应从零散 TODO 收敛到对应架构文档

## 七、完成定义

当以下条件同时满足时，可以认为本轮重构完成：

- 主/子 Agent 的权限与 scope 约束在执行期可验证
- `QueryEngine` 不再承担多种横切编排职责
- `create_engine()` 不再是巨型装配工厂
- task 层具备清晰的定位和可靠的状态语义
- runtime 与 harness 的分工在代码和文档中一致
- `docs/` 内的架构文档可以作为真实实现的可信入口

## 八、不建议现在做的事

在上述阶段完成前，不建议优先投入：

- 更复杂的自动派工算法
- 更多子 Agent 角色
- 更复杂的记忆策略
- 更复杂的 TUI 可视化
- 更多 provider 兼容层

原因很简单：底层边界还没有收紧，继续加功能只会放大后续重构成本。
