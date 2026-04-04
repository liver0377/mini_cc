# Agent 设计

## Agent 抽象

### AgentId

每个 Agent 拥有全局唯一 ID，格式为 8 位十六进制短随机字符串（如 `a3f7b2c1`），用于标识 worktree 路径、快照目录、output 文件、任务归属和日志。

### AgentConfig

Agent 的不可变配置，在创建时确定，运行期间不变。

| 字段 | 说明 |
|------|------|
| agent_id | 全局唯一 Agent 标识 |
| worktree_path | Agent 的工作目录（写 Agent = project_root，只读 Agent = worktree 路径） |
| is_fork | 是否为 Forked Agent（继承父 Agent 上下文） |
| is_readonly | 是否为只读 Agent |
| parent_agent_id | Forked Agent 的父 Agent ID |
| timeout_seconds | 同步 Agent 超时阈值，默认 120 秒 |

### SubAgent

子 Agent 运行时对象，持有独立的 QueryEngine 和 QueryState，共享主 Agent 的 LLM Provider。

| 操作 | 说明 |
|------|------|
| run(prompt) | 前台执行任务，yield 事件流给调用方（用于写 Agent） |
| run_background(prompt) | 后台执行任务，完成后将结果放入完成队列（用于只读 Agent） |
| cancel() | 取消正在运行的任务 |

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
| created | 已创建，尚未开始执行 |
| running | 同步运行中，主 Agent 阻塞等待 |
| background_running | 异步运行中（只读 Agent 的后台执行模式） |
| completed | 执行完成，结果已就绪 |

### 写 Agent 流程

```
主 Agent                              子 Agent
  │                                      │
  │── AgentTool(readonly=False) ──────→│ created
  │                                      │
  │   ← 阻塞等待 ←────────────────── │ running...
  │   （子 Agent 直接修改主工作区）    │
  │   （修改前自动快照原始文件）       │
  │                                      │
  │   ← 返回结果 ←────────────────── │ completed
```

1. 主 Agent 调用 AgentTool，readonly=false
2. AgentManager 创建子 Agent，不创建 worktree，注入 SnapshotService
3. 主 Agent **阻塞等待**子 Agent 完成
4. 子 Agent 执行期间直接修改主工作区文件，修改前通过 SnapshotService 自动备份
5. 完成后返回文本结果给主 Agent

### 只读 Agent 流程

1. 主 Agent 调用 AgentTool，readonly=true
2. AgentManager 创建子 Agent，分配独立 git worktree
3. 子 Agent 在后台异步启动，主 Agent 立即收到"已启动"确认
4. 主 Agent 继续处理当前对话
5. 子 Agent 完成后，结果通过 asyncio.Queue 通知主 Agent
6. 主 Agent 轮询完成后，将结果作为新的用户消息再次提交给 LLM，生成最终汇总

### Forked Agent 流程

Fork 模式是写 Agent 的变体（fork=true, readonly=false）：

1. 主 Agent 调用 AgentTool，fork=true
2. **深拷贝**当前 QueryState 作为子 Agent 的初始状态
3. 注入路径上下文提示（直接操作主工作区，无需路径翻译）
4. 子 Agent 在主工作区中执行读写操作，修改前自动快照
5. 完成后结果回传主 Agent

---

## AgentTool

AgentTool 是 LLM 创建子 Agent 的唯一工具入口，注册在主 Agent 的工具列表中。

### 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| prompt | string | 必填 | 子 Agent 要执行的任务描述 |
| readonly | boolean | false | true = 只读探索（异步 + worktree），false = 写操作（同步 + 直写主工作区 + 快照备份） |
| fork | boolean | false | 是否继承当前对话上下文（仅对写 Agent 有意义） |

### 参数组合

| readonly | fork | 行为 |
|----------|------|------|
| false（默认） | false | 写 Agent，独立状态，直写主工作区，同步阻塞，文件快照备份 |
| false | true | 写 Agent，继承父上下文，直写主工作区，同步阻塞 + 文件快照 |
| true | 忽略 | 只读 Agent，worktree 隔离，异步后台运行 |

### 执行路由

```
AgentTool.execute(prompt, readonly, fork)
        │
        ├── readonly=True   ──→  创建只读 Agent → worktree 隔离 → 后台运行
        │
        └── readonly=False  ──→  创建写 Agent → 直写主工作区 → 阻塞等待
              │
              ├── fork=True   ──→  深拷贝父 State（Forked Agent）
              └── fork=False  ──→  独立 State
```

---

## AgentManager

AgentManager 管理当前会话中所有活跃的子 Agent，负责创建、注册和清理。

### 职责

| 操作 | 说明 |
|------|------|
| create_agent(readonly, fork, ...) | 创建子 Agent：分配 AgentId，按类型条件创建 worktree 和 SnapshotService，组装独立 QueryEngine |
| get_agent(agent_id) | 获取指定 ID 的活跃 Agent |
| cleanup(agent_id) | 清理 Agent 资源（worktree 或快照、output 文件、从活跃列表移除） |

### QueryEngine 组装

每个子 Agent 拥有独立的 QueryEngine，组装方式因类型而异：

**写 Agent：**
- 不创建 worktree，工作目录 = project_root
- 使用全量工具注册表（file_read, file_edit, file_write, bash, glob, grep）
- 创建 SnapshotService，并通过 pre_execute_hook 注入 StreamingToolExecutor
- 注入共享的 LLM Provider stream 函数

**只读 Agent：**
- 创建 git worktree，工作目录 = worktree 路径
- 使用只读工具注册表（file_read, glob, grep, bash）
- 无 SnapshotService，无 pre_execute_hook
- 注入共享的 LLM Provider stream 函数

---

## 资源清理

### 写 Agent 清理

1. 从 AgentManager 活跃列表中移除
2. 删除 output 文件（如有）
3. 清理快照目录
4. 无 worktree 需要清理
5. 关联的 Task 状态更新为 completed

### 只读 Agent 清理

1. 强制移除 git worktree
2. 删除 output 文件（如有）
3. 从 AgentManager 活跃列表中移除
4. 关联的 Task 状态更新为 completed
