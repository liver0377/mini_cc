# 基础设施：Worktree 隔离与消息同步

---

## 一、Worktree 隔离

### 概述

每个子 Agent（包括同步、异步、Forked）都在独立的 git worktree 中运行，实现文件系统层面的工作目录隔离。子 Agent 的所有工具操作（文件读写、bash 执行、搜索）都以 worktree 路径为根目录。

### 目录结构

```
<project_root>/
├── .mini_cc/
│   └── worktrees/
│       ├── a3f7b2c1/       # 子 Agent a3f7b2c1 的 worktree
│       ├── f8e4d9c0/       # 子 Agent f8e4d9c0 的 worktree
│       └── ...
├── src/
├── tests/
└── ...
```

### 创建流程

```
AgentManager.create_agent()
        │
        ▼
  生成 agent_id（8位十六进制）
        │
        ▼
  git worktree add .mini_cc/worktrees/<agent_id> HEAD
        │
        ▼
  将 worktree 路径写入 AgentConfig
        │
        ▼
  以 worktree 为 workdir 创建工具注册表
        │
        ▼
  组装独立 QueryEngine
```

### 清理流程

1. 强制移除 worktree（`git worktree remove --force`）
2. 若有未提交变更，先 stash 或提示用户
3. 由子 Agent 的 Task 完成事件触发清理

### Forked Agent 的路径翻译

Forked Agent 继承了父 Agent 的完整对话上下文，但工作目录不同。系统注入一条用户消息（`buildWorktreeNotice`）指导 LLM：

> 你继承了父代理在 /project 的对话上下文。
> 你现在在隔离的 git worktree /project/.mini_cc/worktrees/agent-a3f7b2c1 中。
> 上下文中的路径指向父目录——请翻译到你的 worktree。
> 编辑前请重新读取文件（父代理可能已修改）。

路径翻译**没有程序化的翻译函数**，靠 LLM 自行理解并翻译。只有 Fork 模式需要此通知——同步/异步子 Agent 从零开始，不继承历史路径。

### 并发安全

- 同一项目下可同时存在多个 worktree
- Git worktree 天然支持同仓库多工作目录并行
- 文件锁（fcntl.flock）避免同一文件被多个 Agent 同时写入
- `.mini_cc/worktrees/` 应加入 `.gitignore`

---

## 二、消息同步

### 概述

主 Agent 与子 Agent 之间的消息同步分为两种模式：**同步阻塞**和**异步通知**。

### 同步模式

```
主 Agent                     子 Agent
  │                            │
  │── AgentTool(sync=True) ──→│
  │                            │
  │◄── 阻塞等待事件流 ◄───────│  run() yield Event
  │                            │
  │   （正常完成）              │
  │◄── ToolResult ◄───────────│  completed
  │                            │
  │   （或超过 120s）           │
  │◄── "已转后台" ◄───────────│  → background_running
  │                            │
  │◄── Queue 通知 ◄───────────│  completed
```

1. LLM 调用 AgentTool，指定 `sync=True`
2. AgentTool 阻塞等待子 Agent 的 `run()` 事件流
3. 120 秒内完成 → 收集结果，返回 `ToolResult`
4. 超时 → 子 Agent 转为后台运行，返回"已转后台"提示，后续结果通过 asyncio.Queue 通知

### 异步模式

```
主 Agent                     子 Agent
  │                            │
  │── AgentTool(sync=False) ─→│
  │                            │
  │◄── "已启动" ToolResult ◄──│
  │                            │  asyncio.create_task()
  │  （主 Agent 继续          │  run_background()...
  │   处理当前对话）           │
  │                            │
  │  ... 多轮对话 ...          │  后台执行中...
  │                            │
  │◄── Queue 通知 ◄───────────│  completed
  │    + .output 文件已写入    │
```

1. LLM 调用 AgentTool，指定 `sync=False`
2. 子 Agent 在后台启动（`asyncio.create_task`），立即返回"已启动"确认
3. 主 Agent 继续处理当前对话，不阻塞
4. 子 Agent 完成后：将输出写入 `.output` 文件 + 向 asyncio.Queue 发送完成事件

### AgentCompletionEvent

子 Agent 完成时发送的通知事件，包含以下字段：

| 字段 | 说明 |
|------|------|
| `agent_id` | 完成的子 Agent ID |
| `task_id` | 关联的 Task ID |
| `success` | 是否成功完成 |
| `output` | 结果摘要（截断至 500 字符） |
| `output_path` | 完整输出的文件路径 |

### 主 Agent 消费异步结果

主 Agent 在 `_query_loop` 的每个 turn 开始前，检查 asyncio.Queue 中是否有子 Agent 完成的通知。若有，yield 一个 `AgentCompletionNotificationEvent` 事件，作为新增的 Event 类型传递给上层。

### .output 文件

异步子 Agent 的完整输出持久化到文件系统：

```
<session_dir>/tasks/
├── a3f7b2c1.output    # 子 Agent a3f7b2c1 的完整输出
├── f8e4d9c0.output    # 子 Agent f8e4d9c0 的完整输出
└── ...
```

每个 .output 文件包含：agent_id、task_id、status、完整文本输出、工具调用次数、创建/完成时间。

用途：

1. **进程恢复** — 主 Agent 重启后可从文件读取子 Agent 结果
2. **调试审计** — 记录子 Agent 完整执行过程
3. **结果回溯** — 主 Agent 可随时通过 file_read 读取

### 多层嵌套通知

若未来支持多层 Agent 嵌套（子 Agent 也创建子 Agent），通知沿层级向上传递：

```
孙 Agent ──Queue──→ 子 Agent ──Queue──→ 主 Agent
```

每层 Agent 的 `run_background` 完成后，向父 Agent 的完成队列发送通知。
