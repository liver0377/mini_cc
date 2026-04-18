# 工具系统 (tools)

工具系统为 LLM 提供与外部环境交互的能力。每个工具封装一种特定的操作（文件读写、命令执行、搜索等），通过统一的接口注册和调用。

## 模块结构

```
tools/
├── base.py          # BaseTool 抽象基类、ToolResult、ToolRegistry
├── bash.py          # Bash 命令执行工具
├── file_read.py     # 文件读取工具
├── file_write.py    # 文件写入工具
├── file_edit.py     # 文件编辑工具（精确字符串替换）
├── glob.py          # 文件模式匹配工具（基于 ripgrep）
├── grep.py          # 文件内容搜索工具（基于 ripgrep）
├── scan_dir.py      # 目录结构扫描工具
├── plan_agents.py   # 智能体调度计划生成工具
└── agent_tool.py    # 智能体委派工具
```

## 架构图

```
┌───────────────────────────────────────────────────────────┐
│                    ToolRegistry                            │
│                                                           │
│  注册表模式：工具名 → BaseTool 实例                         │
│                                                           │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐        │
│  │  bash   │ │file_read│ │file_write│ │file_edit│        │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘        │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐        │
│  │  glob   │ │  grep   │ │scan_dir │ │  agent  │        │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘        │
│  ┌───────────┐                                           │
│  │plan_agents│                                           │
│  └───────────┘                                           │
│                                                           │
│  ┌─────────────────────────────────────────┐              │
│  │          BaseTool (ABC)                 │              │
│  │  name: str                              │              │
│  │  description: str                       │              │
│  │  input_schema: Pydantic Model           │              │
│  │  execute(input, ctx) → ToolResult       │              │
│  └─────────────────────────────────────────┘              │
└───────────────────────────────────────────────────────────┘
```

## BaseTool 抽象基类

所有工具必须继承 `BaseTool` 抽象基类：

```
BaseTool (ABC)
├── 属性
│   ├── name          # 工具名称（如 "bash", "file_read"）
│   ├── description   # 工具描述（提供给 LLM）
│   └── input_schema  # Pydantic Model 类（输入参数定义）
│
└── 抽象方法
    └── execute(input, ctx) → ToolResult
        ├── input: 已验证的输入参数（Pydantic 实例）
        └── ctx: ToolUseContext（工具使用上下文）
```

## ToolResult — 统一结果模型

所有工具的执行结果均以 `ToolResult` 返回：

```
ToolResult
├── success: bool         # 是否成功
├── output: str | None    # 成功时的输出
└── error: str | None     # 失败时的错误信息
```

**设计要点：** 工具错误不通过异常抛出，而是以 `success=False` 的结果返回，确保 Agent Loop 不会因工具错误而中断。

## ToolRegistry — 工具注册表

```
ToolRegistry
├── register(tool)           # 注册工具
├── get(name) → BaseTool     # 按名称获取工具
├── list_tools() → list      # 列出所有工具
├── get_schemas() → list     # 获取所有工具的 JSON Schema
└── filter(allowed) → Registry # 返回过滤后的注册表
```

## 工具分类

### 安全工具（只读）

安全工具不修改任何文件或状态，可并发执行：

| 工具 | 功能 | 输入 |
|------|------|------|
| `file_read` | 读取文件内容 | 文件路径、偏移量、行数限制 |
| `glob` | 按模式匹配文件 | glob 模式、搜索目录 |
| `grep` | 按正则搜索文件内容 | 正则模式、文件过滤器、目录 |
| `scan_dir` | 扫描目录结构 | 目录路径 |

### 非安全工具（写入/执行）

非安全工具可能修改文件或执行命令，必须串行执行：

| 工具 | 功能 | 输入 |
|------|------|------|
| `bash` | 执行 Shell 命令 | 命令字符串、超时时间、工作目录 |
| `file_write` | 写入文件 | 文件路径、内容 |
| `file_edit` | 编辑文件（精确替换） | 文件路径、旧字符串、新字符串 |
| `agent_tool` | 委派子智能体 | 任务描述、作用域、是否只读 |
| `plan_agents` | 生成智能体调度计划 | 任务描述 |

## 各工具详细设计

### Bash

```
Bash
├── 异步子进程执行
│   ├── asyncio.create_subprocess_exec()
│   ├── stdout/stderr 实时捕获
│   └── 可配置超时（默认 120s）
│
├── 安全限制
│   ├── 工作目录限定
│   └── ExecutionPolicy 中的 Bash 限制
│
└── 中断支持
    └── 用户可通过 interrupt 标志中止执行
```

### FileRead

```
FileRead
├── 支持参数
│   ├── filePath（必需）：绝对路径
│   ├── offset：起始行号（1-indexed）
│   └── limit：最大行数（默认 2000）
│
├── 特殊能力
│   ├── 支持读取图片和 PDF
│   └── 超长行自动截断（2000 字符）
│
└── 错误处理
    ├── 文件不存在 → ToolResult(error="...")
    ├── UnicodeDecodeError → 提示二进制文件
    └── 权限不足 → ToolResult(error="...")
```

### FileWrite

```
FileWrite
├── 输入
│   ├── filePath：绝对路径
│   └── content：写入内容
│
├── 行为
│   ├── 覆盖现有文件
│   └── 自动创建父目录
│
└── 安全
    └── 需先读取文件（防止盲目覆盖）
```

### FileEdit

```
FileEdit
├── 输入
│   ├── filePath：文件路径
│   ├── oldString：要替换的文本
│   ├── newString：替换后的文本
│   └── replaceAll：是否全局替换
│
├── 行为
│   ├── 精确字符串匹配替换
│   ├── oldString 必须唯一（除非 replaceAll=True）
│   └── 多处匹配时报错
│
└── 安全检查
    └── 必须先读取文件后才能编辑
```

### Glob

```
Glob
├── 基于文件名模式匹配
│   ├── 使用 ripgrep（rg）底层实现
│   └── 支持标准 glob 模式（**/*.py 等）
│
├── 输入
│   ├── pattern：glob 模式
│   └── path：搜索目录（可选）
│
└── 输出
    └── 匹配文件路径列表（按修改时间排序）
```

### Grep

```
Grep
├── 基于内容正则搜索
│   ├── 使用 ripgrep（rg）底层实现
│   └── 支持完整正则语法
│
├── 输入
│   ├── pattern：正则表达式
│   ├── include：文件过滤器（如 *.py）
│   └── path：搜索目录（可选）
│
└── 输出
    └── 匹配文件路径 + 行号列表
```

### ScanDir

```
ScanDir
├── 扫描目录结构
│   └── 返回目录树形结构
│
├── 输入
│   └── path：目录路径
│
└── 输出
    └── 目录条目列表（子目录以 / 后缀标记）
```

### PlanAgents

```
PlanAgents
├── 生成子智能体调度计划
│   └── 调用 LLM 分析任务，生成 JSON 格式的分配方案
│
├── 输入
│   └── 任务描述
│
└── 输出
    └── JSON 格式的智能体分配计划
```

### AgentTool

```
AgentTool
├── 委派任务给子智能体
│
├── 调度模式
│   ├── 写入型智能体
│   │   └── 同步前台执行，等待完成后返回结果
│   │
│   └── 只读型智能体
│       └── 异步后台执行，通过 EventBus 通知完成
│
├── 批量调度
│   └── 支持 PlanAgentsTool 生成的调度计划
│       └── 多个只读智能体并行执行
│
└── 输入
    ├── task：任务描述
    ├── scope：文件作用域
    └── readonly：是否只读
```

## 执行策略

工具执行受 `ExecutionPolicy` 约束：

```
ExecutionPolicy
├── readonly_only: bool        # 是否强制只读模式
├── allowed_tools: set[str]    # 工具白名单
├── scope_path: Path | None    # 限制操作路径
└── bash_restricted: bool      # 是否限制 Bash
```

**策略应用层级：**

| 场景 | 策略 |
|------|------|
| 主引擎 | 完整工具集，无路径限制 |
| 写入型子智能体 | 全部工具，限定路径到 worktree |
| 只读型子智能体 | 只读工具 + 无 agent_tool，限定路径 |
| Harness 步骤 | 根据步骤类型动态配置 |
