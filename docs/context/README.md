# Context Management

## Overview

系统提示词（System Prompt）由 **静态部分** 和 **动态部分** 拼接而成。

- **静态部分**：以 Markdown 模板文件存储于 `src/mini_cc/context/prompts/`，程序启动时加载一次并缓存。
- **动态部分**：每次启动时重建，包含环境信息和 AGENTS.md 内容。

最终拼接结果作为 `system` 角色消息注入 `QueryState` 的首条消息。

## 静态部分（跨用户可缓存）

从 `src/mini_cc/context/prompts/*.md` 读取，按固定顺序拼接。

| 文件 | 用途 |
|------|------|
| `intro.md` | 角色定义 — 你是一个 code agent … |
| `rules.md` | 行为规则 — 不要过度设计、先读后改、不谎报结果 … |
| `caution.md` | 谨慎行动指南 — 风险操作需确认、三思而后行 … |
| `tool_guide.md` | 工具使用策略 — 优先用 glob/grep 而非 bash find、file_edit 精确替换 … |

## 动态部分（每次启动重建）

### 环境信息

```xml
<env>
Working directory: /path/to/project
Is directory a git repo: Yes
Platform: linux
Shell: bash
OS Version: 6.8.0-xx-generic
</env>
You are powered by the model named <model_name>. The exact model ID is <model_id>.
```

由 `SystemPromptBuilder` 在启动时自动收集。

### AGENTS.md

从当前工作目录（`cwd`）读取 `AGENTS.md`，注入到系统提示词末尾。如文件不存在则跳过。

### 运行模式

项目支持两种全局运行模式，通过 Tab 键切换：

- **Plan 模式（只读）**：LLM 只能执行读取操作（file_read、glob、grep），不能修改任何文件或执行写操作。
- **Build 模式（读写）**：LLM 可以执行所有工具，包括文件编辑、写入、bash 命令等。

当前模式作为环境信息的一部分注入系统提示词。

## 不属于系统提示词

### 工具调用摘要

工具调用摘要**不注入 prompt**，而是渲染到终端显示给用户，用于缓解等待焦虑。属于 REPL 显示层，在 `repl.py` 的 `render_event()` 中处理。

## 架构

```
src/mini_cc/context/
├── __init__.py           # re-exports
├── tool_use.py           # 工具执行上下文（已有）
├── system_prompt.py      # SystemPromptBuilder
└── prompts/
    ├── intro.md          # 角色定义
    ├── rules.md          # 行为规则
    ├── caution.md        # 谨慎行动指南
    └── tool_guide.md     # 工具使用策略
```

### SystemPromptBuilder

```python
class SystemPromptBuilder:
    def __init__(self, static_parts: list[str]) -> None: ...
    def build(self, env_info: EnvInfo, mode: str) -> str: ...
```

- `__init__`：加载 `prompts/*.md`，缓存静态文本。
- `build`：拼接 `静态文本 + 环境信息 + 运行模式 + AGENTS.md`，返回完整系统提示词。

### 集成点

1. `repl.py` 的 `create_engine()` 中创建 `SystemPromptBuilder` 实例。
2. `cli.py` 的 `chat` 命令中将 `Message(role=SYSTEM, content=builder.build(...))` 作为 `QueryState` 的首条消息。
