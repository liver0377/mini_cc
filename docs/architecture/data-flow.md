# 数据流与事件系统

## 事件驱动架构

整个系统的核心数据流由事件驱动。LLM 的流式响应被解析为一系列结构化事件，上层消费者（REPL/TUI）通过消费这些事件来渲染界面。

## 事件类型体系

```
Event（联合类型）
├── TextDelta                    # LLM 文本增量
├── ToolCallStart                # 工具调用开始（含工具名、调用 ID）
├── ToolCallDelta                # 工具调用参数增量
├── ToolCallEnd                  # 工具调用结束
├── ToolResultEvent              # 工具执行结果
├── AgentStartEvent              # 子智能体启动
├── AgentTextDeltaEvent          # 子智能体文本增量
├── AgentToolCallEvent           # 子智能体工具调用
├── AgentToolResultEvent         # 子智能体工具结果
├── AgentCompletionEvent         # 子智能体完成
├── CompactOccurred              # 上下文压缩发生
└── ContextLengthExceededError   # 上下文长度超限错误
```

## Agent Loop 数据流

以下展示了单轮 Agent Loop 的完整数据流转过程：

```
┌──────────────────────────────────────────────────────────────┐
│                        QueryEngine                           │
│                                                              │
│  ① 构建消息列表                                               │
│     system_prompt + conversation_history + user_message       │
│              │                                               │
│              ▼                                               │
│  ② 调用 LLMProvider.stream(messages)                         │
│              │                                               │
│              ▼                                               │
│  ③ 消费原始流，产生结构化事件                                    │
│     ┌───────────────────────────────────────────┐            │
│     │  原始流片段                                  │            │
│     │    ├── 文本内容 ──► TextDelta               │            │
│     │    ├── 工具调用开始 ──► ToolCallStart        │            │
│     │    ├── 工具参数片段 ──► ToolCallDelta        │            │
│     │    └── 工具调用结束 ──► ToolCallEnd          │            │
│     └───────────────────────────────────────────┘            │
│              │                                               │
│              ▼                                               │
│  ④ yield 事件给上层消费者（REPL / TUI）                        │
│              │                                               │
│              ▼                                               │
│  ⑤ 收集所有 ToolCall（collect_tool_calls）                    │
│              │                                               │
│              ▼                                               │
│     ┌─── 存在工具调用？ ───┐                                  │
│     │                      │                                  │
│    是                     否                                  │
│     │                      │                                  │
│     ▼                      ▼                                  │
│  ⑥ 执行工具             本轮结束                              │
│     StreamingToolExecutor                                    │
│     │  ├── 安全工具并发执行                                    │
│     │  └── 非安全工具串行执行                                  │
│     │                                                        │
│     ▼                                                        │
│  ⑦ 产出 ToolResultEvent                                      │
│     │                                                        │
│     ▼                                                        │
│  ⑧ 将工具结果追加到消息列表                                    │
│     │                                                        │
│     ▼                                                        │
│  ⑨ 检查是否需要压缩（CompactionController）                    │
│     │                                                        │
│     ▼                                                        │
│  ⑩ 回到步骤 ②，继续下一轮循环                                 │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## 事件传播路径

```
LLMProvider                    QueryEngine               上层消费者
    │                              │                        │
    │ ── TextDelta ──────────────► │ ── yield ────────────► │ 渲染文本
    │                              │                        │
    │ ── ToolCallStart ──────────► │ ── yield ────────────► │ 显示工具开始
    │ ── ToolCallDelta ──────────► │ ── yield ────────────► │ 更新参数
    │ ── ToolCallEnd ────────────► │ ── yield ────────────► │ 工具调用完成
    │                              │                        │
    │                              │ ── execute tools ──►   │
    │                              │                        │
    │                              │ ── ToolResultEvent ──► │ 显示工具结果
    │                              │    yield               │
```

## 子智能体事件流

子智能体通过独立的 `AgentEventBus` 进行生命周期管理，主引擎通过 `AgentCompletionCoordinator` 收集结果。

```
AgentManager                    AgentEventBus              AgentCompletionCoordinator
    │                               │                           │
    │ ── dispatch agent ─────────►  │                           │
    │                               │                           │
    │                          SubAgent 运行中                   │
    │                               │                           │
    │                               │ ── AgentStartEvent ────►  │
    │                               │ ── AgentTextDelta ─────►  │
    │                               │ ── AgentToolCall ──────►  │
    │                               │ ── AgentToolResult ────►  │
    │                               │ ── AgentCompletion ────►  │
    │                               │                           │
    │                               │                    收集所有完成事件
    │                               │                           │
    │ ◄── 代理完成通知 ───────────── │ ◄── drain all ──────────  │
```

## 上下文压缩流

```
消息列表累积
      │
      ▼
CompactionController.should_compact()
      │
      ├── token 估算超过阈值 ──► 触发自动压缩
      └── ContextLengthExceeded ──► 触发被动压缩
      │
      ▼
compress_messages()
      │
      ├── 取最近 N 条消息保留
      ├── 历史消息发送给 LLM 生成摘要
      └── 用摘要替换历史消息
      │
      ▼
yield CompactOccurred
      │
      ▼
replace_with_summary()
      │
      ▼
继续 Agent Loop
```

## Harness 事件流

自主运行线束拥有独立的事件循环：

```
RunHarness
   │
   ▼
SupervisorLoop（主循环）
   │
   ├── Scheduler.schedule()     ──► 选择下一 WorkItem
   │
   ├── StepRunner.execute()     ──► 执行 WorkItem
   │        │
   │        ├── 构建 prompt ──► QueryEngine ──► 收集结果
   │        └── 或执行 bash / 委派智能体
   │
   ├── RunJudge.assess()        ──► 评估健康度
   │
   ├── PolicyEngine.decide()    ──► 决策下一步行动
   │        │
   │        ├── CONTINUE  ──► 继续下一 WorkItem / Step
   │        ├── RETRY     ──► 重试当前步骤
   │        ├── COOLDOWN  ──► 冷却等待
   │        ├── REPLAN    ──► 重新规划
   │        ├── BLOCK     ──► 保护性失败终止
   │        ├── FAIL      ──► 标记失败
   │        └── TIME_OUT  ──► 超时终止
   │
   ├── IterationOptimizer       ──► 捕获快照、评分迭代
   │
   └── RunDocGenerator          ──► 生成运行文档
```
