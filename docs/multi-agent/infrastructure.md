# 基础设施：隔离策略与消息同步

## 一、隔离策略

系统根据子 Agent 类型采用不同的隔离策略：

| 子 Agent 类型 | 隔离方式 | 原因 |
|---------------|----------|------|
| 写 Agent | 无隔离，直写主工作区 + SnapshotService 快照备份 | 变更立即可见，无需合并 |
| 只读 Agent | 无隔离，仅允许只读工具 + 版本戳校验 | 可并行运行，并在结果回流时标记是否过期 |

### 目录结构

```
<project_root>/
├── .mini_cc/
│   ├── snapshots/
│   │   └── d4e5f6a0/           写 Agent 的快照
│   │       ├── _manifest.json
│   │       └── src/foo.py      原始文件备份（保持相对路径结构）
│   └── tasks/                  output 文件
├── src/
├── tests/
└── ...
```

### SnapshotService

SnapshotService 为写 Agent 提供文件级快照备份，**不操作 git**。

**核心特性：**
- **幂等快照**：每个文件只在第一次修改前备份一次，后续修改不重复备份
- **轻量**：只备份被修改的文件，不做全量快照
- **两种文件状态**：modified（文件已存在，备份原始内容）和 created（文件是 Agent 新建的）
- **拦截机制**：通过 StreamingToolExecutor 的 pre_execute_hook，在 file_edit / file_write 执行前自动快照原始文件

**manifest 记录每个被修改文件的相对路径和状态：**

| 状态 | 含义 | 回滚操作 |
|------|------|----------|
| modified | 文件修改前已存在，备份了原始内容 | 从快照目录复制回项目根目录 |
| created | 文件是 Agent 新建的 | 从项目根目录删除 |

**回滚场景：**

| 场景 | 触发者 | 操作 |
|------|--------|------|
| 子 Agent 修改有误 | 主 Agent LLM | 通过 SnapshotService.restore_all() 恢复所有文件 |
| 用户对变更不满意 | 用户手动 | 目前需手动操作，未来可在 TUI 中提供回滚按钮 |
| 子 Agent 运行中被取消 | SubAgent.cancel() | 已修改的文件已写入磁盘，可从快照恢复 |

### 并发安全

- 写 Agent 通过 scope lease 控制并发；scope 重叠时拒绝启动新的写 Agent
- 只读 Agent 可并行，但运行前后都会记录版本戳，若主工作区发生变化则结果标记为 stale
- .mini_cc/ 目录应加入 .gitignore（覆盖 snapshots/ 和 tasks/）

---

## 二、消息同步

主 Agent 与子 Agent 之间的消息同步机制因类型而异。

### 写 Agent —— 同步阻塞

1. LLM 调用 AgentTool，readonly=false
2. AgentTool 阻塞等待子 Agent 的 run() 事件流
3. 子 Agent 执行期间，SnapshotService 通过 pre_execute_hook 自动备份被修改的原始文件
4. 完成后收集结果，返回 ToolResult 给主 Agent LLM

```
主 Agent                              子 Agent
  │                                      │
  │── AgentTool(readonly=False) ──────→│
  │                                      │
  │◄── 阻塞等待事件流 ◄─────────────────│  run() yield Event
  │                                      │
  │   （子 Agent 直接修改主工作区文件）  │
  │   （修改前自动快照原始文件）        │
  │                                      │
  │◄── ToolResult ◄─────────────────────│  completed
```

### 只读 Agent —— 异步后台

1. LLM 调用 AgentTool，readonly=true
2. 子 Agent 在后台启动（asyncio.create_task），立即返回"已启动"确认
3. 主 Agent 继续处理当前对话，不阻塞
4. 子 Agent 完成后：将输出写入 output 文件 + 向 asyncio.Queue 发送完成事件

```
主 Agent                              子 Agent
  │                                      │
  │── AgentTool(readonly=True) ───────→│
  │                                      │
  │◄── "只读子 Agent 已启动" ◄─────────│
  │                                      │  asyncio.create_task()
  │  （主 Agent 继续                    │  run_background()...
  │   处理当前对话）                     │
  │                                      │
  │  ... 多轮对话 ...                    │  后台执行中...
  │                                      │
  │◄── Queue 通知 ◄─────────────────────│  completed
```

### 结果反馈循环

只读 Agent 完成后，主 Agent 需要将结果反馈给 LLM 生成最终汇总：

1. 提交用户消息，流式处理 LLM 响应。LLM 可能调用 AgentTool 创建多个只读 Agent
2. LLM 响应结束（无更多 tool_call）
3. **_poll_remaining_completions()**：轮询 asyncio.Queue，等待所有后台只读 Agent 完成。每收到一个完成事件显示通知。用户可按 Esc 跳过等待
4. 无完成结果 → 显示完成标记
5. 有完成结果 → **_submit_agent_results()**：
   - 将所有子 Agent 的结果格式化为摘要文本
   - 作为新的用户消息调用 submit_message()（而非直接操作 state.messages）
   - 流式处理 LLM 的汇总响应
   - 显示完成标记

**关键设计：** 使用 submit_message 而非直接操作 state.messages，因为 submit_message 内部会自动将 prompt 追加为 user message。直接手动追加会导致重复消息。

### AgentCompletionEvent

子 Agent 完成时发送的通知事件：

| 字段 | 说明 |
|------|------|
| agent_id | 完成的子 Agent ID |
| task_id | 关联的 Task ID |
| success | 是否成功完成 |
| output | 结果摘要（截断至 500 字符） |
| output_path | 完整输出的文件路径 |
| base_version_stamp | 子 Agent 启动时看到的工作区版本 |
| completed_version_stamp | 子 Agent 完成时的工作区版本 |
| is_stale | 若版本发生变化则为 true，表示结果可能过期 |

---

## 三、Output 持久化

异步子 Agent 的完整输出持久化到文件系统，存储在 session 目录的 tasks/ 子目录下，每个子 Agent 对应一个 .output 文件。

每个 output 文件包含：agent_id、task_id、status、完整文本输出、工具调用次数、创建/完成时间。

**用途：**

| 场景 | 说明 |
|------|------|
| 结果反馈 | 主 Agent 通过 completion_queue 收到通知后读取 output 进行汇总 |
| 调试审计 | 记录子 Agent 完整执行过程 |
| 结果回溯 | 主 Agent 可随时通过 file_read 读取历史结果 |

---

## 四、多层嵌套通知（未来）

若未来支持多层 Agent 嵌套（子 Agent 也创建子 Agent），通知沿层级向上传递：

```
孙 Agent ──Queue──→ 子 Agent ──Queue──→ 主 Agent
```

每层 Agent 的 run_background 完成后，向父 Agent 的完成队列发送通知。
