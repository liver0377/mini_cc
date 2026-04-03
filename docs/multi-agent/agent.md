# Agent 设计

## Agent 抽象

### AgentId

每个 Agent 拥有全局唯一 ID，格式为 8 位十六进制短随机字符串（如 `a3f7b2c1`），用于标识 worktree 路径、output 文件、任务归属和日志。

### AgentConfig

Agent 的不可变配置，在创建时确定，运行期间不变。

| 字段 | 说明 |
|------|------|
| `agent_id` | 全局唯一 Agent 标识 |
| `worktree_path` | 该 Agent 的隔离工作目录 |
| `is_fork` | 是否为 Forked Agent（继承父 Agent 上下文） |
| `parent_agent_id` | Forked Agent 的父 Agent ID |
| `timeout_seconds` | 同步 Agent 超时阈值，默认 120 秒 |

### SubAgent

子 Agent 运行时对象，持有独立的 `QueryEngine` + `QueryState`，共享主 Agent 的 LLM Provider。

核心操作：

| 操作 | 说明 |
|------|------|
| `run(prompt)` | 前台执行任务，yield 事件流给调用方 |
| `run_background(prompt)` | 后台执行任务，完成后将结果放入完成队列 |
| `cancel()` | 取消正在运行的任务 |

---

## Agent 生命周期

### 状态机

```
  created ──────→ running ──────→ completed
                    │     ↗
                    │    /
                    └──→ background_running ──→ completed
```

| 状态 | 含义 |
|------|------|
| `created` | 已创建，尚未开始执行 |
| `running` | 同步运行中，主 Agent 阻塞等待 |
| `background_running` | 异步运行中（原同步 Agent 超时转换，或直接创建为异步） |
| `completed` | 执行完成，结果已就绪 |

### 同步 Agent 流程

```
主 Agent                        子 Agent
  │                               │
  │── AgentTool(sync=True) ──────→│ created
  │                               │
  │   ← 阻塞等待 ←────────────── │ running...
  │                               │
  │   ← 返回结果 ←────────────── │ completed
  │                               │
  │   （若超过 120s）               │
  │   ← 返回"已转后台" ←────────  │ background_running...
  │                               │
  │   ← Queue 通知 ←──────────── │ completed
```

1. LLM 调用 `AgentTool(prompt="...", sync=True)`
2. 主 Agent 创建子 Agent，分配 worktree，注入独立 QueryEngine
3. 主 Agent **阻塞**等待子 Agent 完成
4. 120 秒内完成 → 直接返回结果
5. 超时 → 子 Agent 自动转为后台运行，主 Agent 解除阻塞，通过 asyncio.Queue 接收后续结果

### 异步 Agent 流程

1. LLM 调用 `AgentTool(prompt="...", sync=False)`
2. 主 Agent 创建子 Agent，分配 worktree
3. 子 Agent 在后台启动，主 Agent 立即收到"已启动"确认
4. 主 Agent 继续处理当前对话
5. 子 Agent 完成后，结果通过 asyncio.Queue 通知主 Agent

### Forked Agent 流程

1. LLM 调用 `AgentTool(prompt="...", fork=True)`
2. 主 Agent **深拷贝**当前 `QueryState` 作为子 Agent 的初始状态
3. 注入 `buildWorktreeNotice` 用户消息，指导 LLM 将上下文中的路径翻译到新 worktree
4. 子 Agent 在隔离 worktree 中执行读写操作
5. 完成后结果回传主 Agent

---

## AgentTool

AgentTool 是 LLM 创建子 Agent 的唯一工具入口，继承自 `BaseTool`，注册在主 Agent 的工具列表中。

### 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `prompt` | string | —（必填） | 子 Agent 要执行的任务描述 |
| `sync` | boolean | `true` | 是否同步执行（`true`=前台阻塞，`false`=后台运行） |
| `fork` | boolean | `false` | 是否继承当前对话上下文（Fork 模式） |

### 执行路由

```
AgentTool.execute(prompt, sync, fork)
        │
        ├── fork=True  ──→  创建 Forked Agent（深拷贝父 State）
        │
        ├── sync=True   ──→  创建同步 Agent → 阻塞等待（超时转异步）
        │
        └── sync=False  ──→  创建异步 Agent → 后台运行
```

### 子 Agent 工具隔离

子 Agent 的工具注册表**不包含** AgentTool，防止无限嵌套。未来可通过配置开放多层嵌套。

子 Agent 的所有文件工具、Bash 工具均以 worktree 路径为工作目录，实现文件系统隔离。

---

## AgentManager

AgentManager 管理当前会话中所有活跃的子 Agent。

| 操作 | 说明 |
|------|------|
| `create_agent(...)` | 创建子 Agent（分配 ID、创建 worktree、组装 QueryEngine） |
| `register_task(agent)` | 将 Agent 注册为 Task，返回 task_id |
| `get_agent(agent_id)` | 获取指定 ID 的活跃 Agent |
| `cleanup(agent_id)` | 清理 Agent 资源（worktree、output 文件、活跃列表移除） |

---

## 子 Agent 的 QueryEngine 组装

每个子 Agent 拥有独立的 QueryEngine，组装方式与主 Agent 一致：

1. 以 worktree 路径为根目录创建工具注册表
2. 创建 `StreamingToolExecutor` 包装注册表
3. 组装 `ToolUseContext`（schemas + 执行 + 中断信号）
4. 注入共享的 LLM Provider stream 函数，构建 `QueryEngine`

关键点：工具注册表的 `workdir` 参数指向子 Agent 的 worktree，所有文件操作自动限定在隔离目录内。

---

## 资源清理

子 Agent 完成或取消后，按顺序执行：

1. 清理 git worktree（`git worktree remove`）
2. 删除 `.output` 文件（如有）
3. 从 AgentManager 活跃列表中移除
4. 关联的 Task 状态更新为 `completed`
