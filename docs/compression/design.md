# 上下文压缩设计文档

## 概述

上下文压缩（Context Compression）解决的是长对话中上下文窗口不够的问题。当对话轮次增多、上下文 token 数接近模型限制时，用 LLM 生成的结构化摘要替代完整的对话历史，让模型在有限的上下文窗口内仍能保持对全局的理解。

压缩本质上是一种**会话级的短期记忆**——生命周期仅限当前会话，会话结束即废弃。与中期记忆系统（见 memory 设计文档）不同，压缩不跨会话持久化。

系统支持**三种压缩场景**，覆盖主动预防、被动恢复和用户主动触发：

### 场景 1：自动压缩（Auto Compact）

- **触发时机**：_query_loop 每轮开始前，在 API 调用之前检测
- **触发条件**：token 估算值 ≥ AUTO_COMPACT_THRESHOLD（默认 80000）
- **行为**：压缩后 yield CompactOccurred(reason="auto") 通知 UI
- **额外效果**：重置 has_attempted_reactive_compact 标记

### 场景 2：反应式压缩（Reactive Compact）

- **触发时机**：API 返回 413 / context_length_exceeded 错误时
- **行为**：在错误恢复阶段执行压缩
- **限制**：仅尝试一次（has_attempted_reactive_compact 标记防止无限循环）
- **失败处理**：压缩后仍超限，异常继续向上抛出

### 场景 3：手动压缩（/compact 命令）

- **触发时机**：用户输入 /compact
- **行为**：UI 层直接调用 compress_messages()
- **结果**：替换当前 state.messages

## Token 计数

### 实现方式

使用 tiktoken 库进行精确 token 计数。编码器选择策略：

1. 优先通过模型名查找专用编码器（tiktoken.encoding_for_model）
2. 查找失败时（非 OpenAI 模型），回退到 cl100k_base（GPT-4 通用编码器）
3. cl100k_base 对大多数 OpenAI 兼容模型（GLM、DeepSeek、Qwen 等）的估算精度足够用于阈值判断

### 计数范围

对每条消息计算：
- 每条消息固定 4 token 的格式开销（角色标签等）
- 消息内容的 token 数
- 工具调用参数的 token 数
- 工具名称的 token 数

### 控制参数

| 参数 | 来源 | 默认值 | 说明 |
|------|------|--------|------|
| AUTO_COMPACT_THRESHOLD | 环境变量 | 80000 | 自动压缩的 token 阈值 |
| 编码器 | 模型名自动选择 | cl100k_base | tiktoken 编码器 |

## 核心流程：compress_messages()

三种场景最终都调用同一个核心函数，签名为：

compress_messages(messages, stream_fn, existing_summary=None) → str

### 执行步骤

1. 保留首条消息（system message）
2. 将所有非 system 消息格式化为文本（角色标签 + 内容，工具调用结果摘要）
3. 构建压缩 prompt：COMPRESSION_SYSTEM_PROMPT + 格式化的对话文本
4. 通过 stream_fn（复用项目的 LLMProvider）调用 LLM 生成摘要
5. 返回摘要文本

### 压缩后的消息替换

压缩完成后，state.messages 被替换为：

1. **[0] system message**（保持不变）
2. **[1] user message**（"以下是之前对话的摘要：" + 摘要内容）

后续对话正常追加。当再次超过阈值时，摘要 + 新消息一起被再次压缩，形成增量压缩。

## 压缩 Prompt 结构

COMPRESSION_SYSTEM_PROMPT 使用中文定义了结构化摘要格式，包含五个部分：

- **Goal**：当前对话的总体目标
- **Completed**：已完成的工作
- **Discoveries**：重要发现和结论
- **Pending**：尚未完成的任务
- **Context**：需要保留的上下文信息

## 自动压缩流程

在 _query_loop 每轮开始前执行：

1. 计算当前 messages 的 token 估算值
2. 如果 token 数 ≥ AUTO_COMPACT_THRESHOLD：
   - 调用 compress_messages() 生成摘要
   - 用摘要替换 messages
   - yield CompactOccurred(reason="auto")
   - 重置 has_attempted_reactive 标记
3. 继续正常的 API 调用

**设计考量**：
- 在 API 调用前执行，主动预防，避免浪费一次注定失败的 API 调用
- 每次压缩后重置 reactive 标记，压缩后 token 数已降低，允许新的 reactive 尝试

## 反应式压缩流程

在 stream_fn 抛出 ContextLengthExceeded 异常时触发：

### 错误检测链路

1. OpenAI API 返回 400 BadRequest（错误码 context_length_exceeded）
2. providers/openai.py 捕获，转换为自定义 ContextLengthExceeded
3. query_engine/engine.py 捕获，执行反应式压缩

### 流程

1. 捕获 ContextLengthExceeded 异常
2. 检查 has_attempted_reactive 标记
3. 如果已尝试过 → 不再重试，异常继续向上抛出
4. 如果未尝试 → 设置标记，执行压缩，continue 重试当前 turn

**设计考量**：
- 仅尝试一次：防止压缩后仍超限导致的无限重试循环
- 压缩失败则异常上抛：让 UI 层显示错误信息
- continue 重试：压缩成功后重新进入循环

## 手动压缩流程

用户在输入框中输入 /compact 时触发：

- **TUI 模式**：ChatScreen 的消息发送逻辑中拦截 /compact，调用 compress_messages() 后在聊天区显示系统提示
- **CLI 模式**：chat 循环中拦截 /compact，调用 compress_messages() 后在终端显示提示

## CompactOccurred 事件

| 字段 | 说明 |
|------|------|
| reason | 触发原因："auto" / "reactive" / "manual" |

UI 渲染方式：
- auto：灰色提示「上下文已自动压缩」
- reactive：黄色警告「上下文超出限制，已自动压缩后重试」
- manual：灰色提示「对话已手动压缩」

## ContextLengthExceeded 异常

在 providers/openai.py 中捕获 OpenAI BadRequestError，检测错误码 context_length_exceeded，转换为项目自定义的 ContextLengthExceeded 异常。

## 模块结构

```
src/mini_cc/compression/
├── __init__.py              公共导出
├── compressor.py            核心压缩逻辑
│   ├── ContextLengthExceeded   自定义异常
│   ├── estimate_tokens()       Token 估算
│   ├── compress_messages()     核心压缩函数
│   ├── should_auto_compact()   自动压缩判定
│   └── replace_with_summary()  消息替换
└── prompts.py               压缩用 prompt 模板
    └── COMPRESSION_SYSTEM_PROMPT
```

## 集成点

| 位置 | 修改 |
|------|------|
| models/events.py | CompactOccurred 定义在 Event union 中 |
| query_engine/engine.py | _query_loop() 中加入自动压缩 + 反应式压缩 |
| providers/openai.py | stream() 中捕获 BadRequestError，转换为 ContextLengthExceeded |
| tui/screens/chat_screen.py | 拦截 /compact 命令；渲染 CompactOccurred 事件 |
| cli.py | chat 循环中拦截 /compact 命令 |

## 关键设计决策

1. **tiktoken + cl100k_base 兜底**：精确计数优先，非 OpenAI 模型使用通用编码器兜底，兼容 GLM、DeepSeek 等模型。

2. **环境变量配置阈值**：AUTO_COMPACT_THRESHOLD 可通过环境变量自定义，默认 80000。

3. **三种场景共用 compress_messages()**：统一核心逻辑，不同场景只决定触发时机和后续行为。

4. **单条 user 摘要**：压缩后用一条 user message 携带摘要，结构简洁。下次压缩时摘要 + 新消息一起被再次压缩。

5. **反应式压缩仅一次**：防止压缩后仍超限导致的无限重试循环。

6. **主动压缩在 API 调用前**：不浪费一次注定失败的 API 调用。

7. **复用 LLMProvider**：压缩调用直接用项目的 StreamFn，不引入新的 API 客户端。

## 与其他模块的交互

- **被 query_engine/engine.py 调用**：自动压缩 + 反应式压缩
- **被 tui/screens/chat_screen.py 调用**：手动压缩（/compact）
- **被 cli.py 调用**：手动压缩（/compact）
- **调用 providers/base.py**：压缩需要 LLM 调用（通过 StreamFn）
- **被 tests/compression/ 测试**
