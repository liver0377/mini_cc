# Context Management

## 概述

系统提示词（System Prompt）由**静态部分**和**动态部分**拼接而成。

- **静态部分**：以 Markdown 模板文件存储于 context/prompts/，程序启动时加载一次并缓存。
- **动态部分**：每次调用 build() 时重建，包含环境信息、AGENTS.md 内容和记忆索引。

最终拼接结果作为 system 角色消息注入 QueryState 的首条消息。

## 静态部分

从 context/prompts/*.md 读取，按固定顺序拼接。系统为主 Agent 和子 Agent 分别维护不同的静态模板。

### 主 Agent 模板

| 文件 | 用途 |
|------|------|
| intro.md | 角色定义 — 你是一个 code agent |
| rules.md | 行为规则 — 不要过度设计、先读后改、不谎报结果 |
| caution.md | 谨慎行动指南 — 风险操作需确认、三思而后行 |
| tool_guide.md | 工具使用策略 — 优先用 glob/grep 而非 bash find、file_edit 精确替换 |

### 子 Agent 模板

| 文件 | 用途 |
|------|------|
| intro_sub.md | 子 Agent 角色定义 |
| rules_sub.md | 子 Agent 行为规则 |
| tool_guide_sub.md | 子 Agent 工具使用策略 |

主 Agent 和子 Agent 共享 caution.md。

## 动态部分

### 环境信息

在 build() 调用时自动收集以下环境信息，包裹在 \<env\> 标签中：

- 工作目录路径
- 是否为 git 仓库
- 平台（linux）
- Shell 类型
- OS 版本
- 当前模型名称和 ID

### AGENTS.md

从当前工作目录（cwd）读取 AGENTS.md，注入到系统提示词末尾。如文件不存在则跳过。

### 记忆索引

从 memory 模块加载 MEMORY.md 索引内容（跨会话持久记忆的目录），追加到系统提示词中。详见 [memory/design.md](../memory/design.md)。

## 运行模式

项目支持两种全局运行模式，通过 Tab 键切换：

- **Plan 模式（只读）**：LLM 只能执行读取操作（file_read、glob、grep），不能修改任何文件或执行写操作。
- **Build 模式（读写）**：LLM 可以执行所有工具，包括文件编辑、写入、bash 命令等。

当前模式作为环境信息的一部分注入系统提示词。

## 两种构建方法

SystemPromptBuilder 提供两个构建方法：

| 方法 | 用途 | 使用的模板 |
|------|------|-----------|
| build() | 主 Agent 系统提示词 | intro.md, rules.md, caution.md, tool_guide.md |
| build_for_sub_agent() | 子 Agent 系统提示词 | intro_sub.md, rules_sub.md, caution.md, tool_guide_sub.md |

## 不属于系统提示词

### 工具调用摘要

工具调用摘要**不注入 prompt**，而是渲染到终端显示给用户，用于缓解等待焦虑。属于 REPL 显示层，在 repl.py 的 render_event() 中处理。

## 架构

```
src/mini_cc/context/
├── __init__.py              公共导出
├── system_prompt.py         SystemPromptBuilder、EnvInfo、collect_env_info()
├── tool_use.py              工具执行上下文类型定义（ToolUseContext）
├── engine_context.py        EngineContext、create_engine() 工厂函数
└── prompts/
    ├── intro.md             主 Agent 角色定义
    ├── intro_sub.md         子 Agent 角色定义
    ├── rules.md             主 Agent 行为规则
    ├── rules_sub.md         子 Agent 行为规则
    ├── caution.md           谨慎行动指南（共用）
    ├── tool_guide.md        主 Agent 工具使用策略
    └── tool_guide_sub.md    子 Agent 工具使用策略
```

### 集成点

1. engine_context.py 的 create_engine() 中创建 SystemPromptBuilder 实例
2. cli.py 的 chat 命令中将 build() 结果作为 QueryState 首条 system 消息
3. agent/manager.py 中通过 build_for_sub_agent() 为子 Agent 组装独立的系统提示词
