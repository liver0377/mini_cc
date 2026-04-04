# Agent Loop 设计

此文档记录整个项目 Agent Loop 的实现机制。详细设计见 [query-engine.md](./query-engine.md)。

## 流式输出

### 事件处理状态机

LLM 流式响应中的每个 chunk 被转换为以下事件类型：

| 事件 | 含义 | 何时产生 |
|------|------|----------|
| `message_start` | 消息开始 | 新一轮响应开始 |
| `content_block_start` | 内容块开始 | 文本块或工具调用块开始 |
| `content_block_delta` | 内容增量 | 文本片段或工具参数片段 |
| `content_block_stop` | 内容块结束 | 单个内容块输出完毕 |
| `message_delta` | 消息级增量 | stop_reason 等消息级信息 |
| `message_stop` | 消息结束 | 整个响应结束 |

### 错误处理

| 错误类型 | 说明 |
|----------|------|
| 网络断开 | 连接中断，需要重试或通知用户 |
| API 限流 | 请求频率超限，需要退避重试 |
| Token 超额 | 上下文超过模型限制，需要压缩或截断 |
