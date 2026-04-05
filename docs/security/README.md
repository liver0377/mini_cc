# 安全设计

此文档记录本项目的安全系统设计。

## Sandbox

Sandbox 用于限制 code agent 的能力范围，确保 agent 的行为在安全边界内。基于 bubblewrap（bwrap）实现 Linux 命名空间级别的隔离。

### 文件系统隔离

- Agent 只能看到项目根目录下的文件
- Agent 只能修改项目根目录下的文件
- 通过 bind mount 将项目目录映射到沙箱内部

### 命令限制

- 禁止 sudo 及其他提权操作
- 禁止执行 rm -f 等危险命令
- 禁止网络访问（可选）

### 资源限制

- 不能无限输出，设置输出大小上限
- 不能吃满 CPU，设置进程资源限制
- 设置执行超时阈值，防止长时间运行

### 状态

目前 Sandbox 功能处于规划阶段，尚未完整实现。

## Plan & Build 模式

Plan 和 Build 是 code agent 的两个全局运行模式，通过 Tab 键实时切换。

### Plan 模式（只读）

- Agent 只能执行只读工具：file_read、glob、grep
- 不能修改任何文件或执行写操作
- 根据用户需求，输出执行计划和建议
- 适用于审查代码、分析问题、制定方案

### Build 模式（读写）

- Agent 可以执行所有工具，包括文件编辑、写入、bash 命令
- 可以修改整个代码仓库
- 适用于实际的编码、重构、修复工作

### 权限实现

权限控制通过 ToolUseContext 的 check_permission 回调实现：

1. QueryEngine 在执行工具前调用 check_permission(tool_name)
2. 根据当前模式判断工具是否被允许
3. Plan 模式下，写操作类工具被拒绝，返回权限拒绝事件
4. 所有工具调用被拒绝时，QueryEngine 终止循环
