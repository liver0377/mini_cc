本文档记录了系统的流式输出设计

### 事件处理状态机
包含以下五种事件类型:
- message_start
- content_block_start
- content_block_delta
- content_block_stop
- message_delta
- message_stop

### 错误处理
包含三种错误：
#### 网络断开

#### API限流

#### Token超额
