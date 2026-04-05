# Agent Loop 设计

此文档记录整个项目 Agent Loop 的实现机制。详细设计见 [query-engine.md](./query-engine.md)。

## 流式输出

### 内部事件模型

LLM 的流式响应由 Provider 解析后，转换为以下内部事件类型，供 QueryEngine 和 UI 层消费：

| 事件 | 含义 | 产生者 |
|------|------|--------|
| TextDelta | LLM 输出的文本片段 | Provider |
| ToolCallStart | 工具调用开始（携带 ID 和工具名） | Provider |
| ToolCallDelta | 工具参数的增量 JSON 片段 | Provider |
| ToolCallEnd | 工具参数传输完毕 | Provider |
| ToolResultEvent | 工具执行结果（成功/失败 + 输出） | ToolExecutor |
| CompactOccurred | 上下文压缩已发生（auto / reactive） | QueryEngine |
| AgentStartEvent | 子 Agent 启动 | AgentTool |
| AgentTextDeltaEvent | 子 Agent 文本输出片段 | AgentTool（从后台队列排空） |
| AgentToolCallEvent | 子 Agent 调用了工具 | AgentTool（从后台队列排空） |
| AgentToolResultEvent | 子 Agent 工具执行结果 | AgentTool（从后台队列排空） |
| AgentCompletionEvent | 子 Agent 完成 | AgentTool（从完成队列排空） |

### 事件流示例

一次 `file_read` 调用的完整事件流：

1. TextDelta — "让我读取这个文件。"
2. ToolCallStart — 携带调用 ID 和工具名 `file_read`
3. ToolCallDelta — 参数片段（可能分多片传输）
4. ToolCallEnd — 参数传输完毕
5. （QueryEngine 将分片参数拼装为完整 ToolCall）
6. ToolResultEvent — 工具执行结果，包含文件内容和成功标志

### 错误处理

| 错误类型 | 说明 |
|----------|------|
| 网络断开 | 连接中断，需要重试或通知用户 |
| API 限流 | 请求频率超限，需要退避重试 |
| Token 超额 | 上下文超过模型限制，触发反应式压缩或向上抛出异常 |
