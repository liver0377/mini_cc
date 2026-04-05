# 工具系统设计

本文件描述整个项目工具系统的设计。

## 工具分类

### 文件工具

| 工具 | 名称 | 用途 | 安全等级 |
|------|------|------|----------|
| FileRead | file_read | 读取文本文件内容 | 安全（可并发） |
| FileEdit | file_edit | 字符串替换编辑（需唯一匹配） | 危险（串行） |
| FileWrite | file_write | 全量写入文件（自动创建父目录） | 危险（串行） |

### Bash 工具

| 工具 | 名称 | 用途 | 安全等级 |
|------|------|------|----------|
| Bash | bash | 执行 shell 命令，支持超时控制 | 危险（串行） |

需要进行严格的安全审核。后台执行时自动创建 local_bash 类型的 Task。

### 搜索工具

两个工具均基于 ripgrep（rg）实现，搜索结果设定长度限制。

| 工具 | 名称 | 用途 | 安全等级 |
|------|------|------|----------|
| GlobTool | glob | 按文件名模式搜索（基于 rg --files） | 安全（可并发） |
| GrepTool | grep | 按正则表达式搜索文件内容 | 安全（可并发） |

### Agent 工具

| 工具 | 名称 | 用途 | 安全等级 |
|------|------|------|----------|
| AgentTool | agent | 创建子 Agent 执行独立任务 | 危险（串行） |

AgentTool 支持三种模式（写 Agent、只读 Agent、Fork Agent），详见 [multi-agent/agent.md](../multi-agent/agent.md)。

## 并发执行模型

StreamingToolExecutor 将工具分为**安全工具**和**危险工具**两类，采用不同的执行策略：

### 安全工具（可并发）

- file_read、glob、grep
- 通过 asyncio.as_completed 并发执行
- 这些工具均为只读操作，互不干扰

### 危险工具（串行）

- file_edit、file_write、bash、agent
- 逐个顺序执行
- 写操作需要串行以保证文件一致性

### Pre-Execute Hook

StreamingToolExecutor 支持可选的 pre_execute_hook，在工具执行前被调用。当前用于 SnapshotService：写 Agent 的工具注册表在 file_edit / file_write 执行前自动快照原始文件。

## 工具注册表

### BaseTool 抽象

所有工具继承自 BaseTool 抽象基类，提供统一接口：

| 方法 | 说明 |
|------|------|
| name | 工具名称字符串 |
| description | 工具描述（中文） |
| input_schema | Pydantic BaseModel，定义输入参数 |
| execute() | 同步执行工具逻辑 |
| async_execute() | 异步执行（在线程池中运行同步逻辑） |
| to_api_format() | 转换为 OpenAI function calling 格式 |

### ToolResult

每个工具执行后返回统一的 ToolResult：

| 字段 | 说明 |
|------|------|
| output | 工具输出文本 |
| error | 错误信息（如有） |
| success | 是否成功执行 |

### ToolRegistry

工具注册表是一个基于字典的注册中心：

| 操作 | 说明 |
|------|------|
| register(tool) | 注册工具实例 |
| get(name) | 按名称获取工具 |
| all() | 获取所有已注册工具 |
| to_api_format() | 返回所有工具的 API 格式列表 |

### 预定义注册表

| 工厂函数 | 包含的工具 | 用途 |
|----------|-----------|------|
| create_default_registry() | file_read, file_edit, file_write, bash, glob, grep + agent | 主 Agent / 写 Agent |
| create_readonly_registry() | file_read, glob, grep, bash | 只读 Agent |

注：AgentTool 在默认注册表创建后单独注册，因为它依赖 AgentManager 实例。

## 模块结构

```
src/mini_cc/tools/
├── __init__.py              公共导出
├── base.py                  BaseTool、ToolResult、ToolRegistry
├── file_read.py             FileRead
├── file_write.py            FileWrite
├── file_edit.py             FileEdit
├── bash.py                  Bash
├── glob.py                  GlobTool
├── grep.py                  GrepTool
└── agent_tool.py            AgentTool + 注册表工厂函数
```
