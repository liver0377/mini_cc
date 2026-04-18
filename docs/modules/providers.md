# LLM 提供者 (providers)

LLM 提供者模块定义了与 LLM 服务交互的标准接口，并提供了 OpenAI API 的具体实现。所有 LLM 交互均采用异步流式处理。

## 模块结构

```
providers/
├── base.py       # LLMProvider 协议定义
└── openai.py     # OpenAI 兼容 API 实现
```

## 架构图

```
┌───────────────────────────────────┐
│          LLMProvider              │
│      （Protocol 接口）             │
│                                   │
│  stream(messages) → AsyncGen[Event]│
│                                   │
└───────────────┬───────────────────┘
                │
                │ 实现
                ▼
┌───────────────────────────────────┐
│         OpenAIProvider            │
│                                   │
│  · OpenAI Python SDK              │
│  · 流式响应解析                    │
│  · 事件转换                        │
│  · ContextLengthExceeded 处理     │
│                                   │
└───────────────────────────────────┘
```

## LLMProvider 协议

`LLMProvider` 是一个 `@runtime_checkable` 的 Protocol 类，定义了所有 LLM 提供者必须实现的接口：

| 方法 | 签名 | 说明 |
|------|------|------|
| `stream` | `(messages) -> AsyncGenerator[Event, None]` | 流式发送消息并返回事件流 |

**协议设计要点：**

- 使用 `@runtime_checkable` Protocol，支持运行时类型检查
- 输入为消息列表，输出为事件的异步生成器
- 提供者负责将原始 LLM 响应转换为系统内部的事件类型
- 调用方无需关心底层 API 差异

## OpenAIProvider

OpenAI 兼容 API 的具体实现，支持任何 OpenAI 兼容的服务端点。

**核心流程：**

```
OpenAIProvider.stream(messages)
   │
   ├── 1. 调用 OpenAI SDK 的 chat.completions.create()
   │       ├── 启用 stream=True
   │       └── 传入消息列表 + 工具定义
   │
   ├── 2. 逐块消费流式响应
   │       │
   │       ├── 文本内容块
   │       │   └── yield TextDelta(content=...)
   │       │
   │       ├── 工具调用块
   │       │   ├── 新工具调用 → yield ToolCallStart
   │       │   ├── 参数增量 → yield ToolCallDelta
   │       │   └── 调用结束 → yield ToolCallEnd
   │       │
   │       └── 结束块
   │           └── 流结束
   │
   └── 3. 错误处理
           ├── ContextLengthExceeded
           │   └── yield ContextLengthExceededError
           └── 其他 API 错误
               └── 传播异常
```

**配置项：**

| 配置 | 说明 |
|------|------|
| API 密钥 | OpenAI API Key 或兼容服务的密钥 |
| Base URL | API 端点地址（支持自定义） |
| 模型名称 | 使用的模型标识 |
| 温度 | 生成温度参数 |

## 与其他模块的关系

```
QueryEngine
   │
   │ 调用
   ▼
LLMProvider.stream(messages)
   │
   │ 返回事件流
   ▼
Event Stream → TextDelta / ToolCallStart / ToolCallDelta / ToolCallEnd
   │
   │ 被 QueryEngine 消费
   ▼
传递给上层消费者（REPL / TUI）
```

## 扩展新的 LLM 提供者

要添加新的 LLM 提供者，只需实现 `LLMProvider` 协议：

```
新提供者需要实现:
├── stream(messages) 方法
│   ├── 接收标准消息列表
│   ├── 调用对应 LLM API
│   ├── 解析响应为事件类型
│   └── 返回 AsyncGenerator[Event, None]
│
└── 事件类型映射
    ├── 文本响应 → TextDelta
    ├── 工具调用开始 → ToolCallStart
    ├── 工具参数增量 → ToolCallDelta
    ├── 工具调用结束 → ToolCallEnd
    └── 上下文超限 → ContextLengthExceededError
```
