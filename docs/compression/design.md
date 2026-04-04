# 上下文压缩设计文档

## 概述

上下文压缩（Context Compression）解决的是长对话中上下文窗口不够的问题。当对话轮次增多、上下文 token 数接近模型限制时，用结构化摘要替代完整的对话历史，让模型在有限的上下文窗口内仍能保持对全局的理解。

压缩本质上是一种**会话级的短期记忆**——生命周期仅限当前会话，会话结束即废弃。

## 存储路径

```
~/.mini_cc/
└── projects/{project_id}/
    └── sessions/
        └── {session_id}/
            └── session-memory.md  # 压缩摘要（会话结束可清理）
```

### 项目 ID 生成

```
cwd 路径 → sha256 hash → 取前 12 位
例: /home/user/projects/myapp → "a1b2c3d4e5f6"

特点：确定性，同一目录永远映射到同一 ID
实现：使用 hashlib.sha256（不受 PYTHONHASHSEED 影响）
```

### Session ID

每次 REPL 启动时生成（`secrets.token_hex(4)`），用于隔离不同会话的压缩文件。

## session-memory.md 模板

压缩文件使用固定的模板结构，LLM 每次更新时保持该结构：

```markdown
# Session Title
A short and distinctive 5-10 word descriptive title

# Current State
What is actively being worked on right now?

# Task specification
What did the user ask to build?

# Files and Functions
What are the important files?

# Workflow
What bash commands are usually run and in what order?

# Errors & Corrections
Errors encountered and how they were fixed.

# Codebase and System Documentation
What are important system components?

# Learnings
What has worked well? What has not?

# Key results
Exact output the user requested (table, answer, etc.)

# Worklog
Step by step, what was attempted, done?
```

每个 section 有独立的 token 限额（`MAX_SECTION_LENGTH`），整个文件有总 token 上限（`MAX_TOTAL_SESSION_MEMORY_TOKENS`）。

## 触发机制

定义两个阈值：

- `minimumTokensBetweenUpdate`：若当前上下文 token 数 - 上一次更新时的上下文 token 数 < 该阈值，不触发压缩
- `minimumToolCallsBetweenUpdates`：若自上次更新时，工具的调用次数 < 该阈值，不触发压缩

此外，若 当前上下文 token 数 - 上一次更新时的上下文 token 数 > `minimumTokensBetweenUpdate` 且最后一轮没有工具调用，也触发压缩。

## 实现方式

每次触发时：

1. 读取当前 `session-memory.md` 内容（如存在）
2. 取最近几轮的对话消息
3. LLM 根据最近对话内容和当前压缩文件内容，重写整个 `session-memory.md`，保持模板结构
4. 写入更新后的文件

## 控制参数

| 参数 | 说明 |
|------|------|
| `MAX_SECTION_LENGTH` | 每个 section 的 token 限额 |
| `MAX_TOTAL_SESSION_MEMORY_TOKENS` | 文件总 token 上限 |
| `minimumTokensBetweenUpdate` | 触发更新的最小 token 增量 |
| `minimumToolCallsBetweenUpdates` | 触发更新的最小工具调用次数 |

## 注入方式

作为系统提示词的一部分。在 `SystemPromptBuilder.build()` 中：

```
系统提示词构成：
1. 静态 prompt（intro.md, rules.md, caution.md, tool_guide.md）
2. 环境信息（<env> ... </env>）
3. AGENTS.md（用户手动维护的项目指令）
4. session-memory.md 摘要（上下文压缩）  ← 本模块
5. MEMORY.md 索引（中期记忆）
```

## 模块结构

```
src/mini_cc/compression/
├── __init__.py              # 公共导出
├── store.py                 # 存储基础设施
│   - project_id(cwd) -> str            # sha256(cwd)[:12]
│   - get_session_dir(cwd, session_id) -> Path
│   - load_session_memory(cwd, session_id) -> str | None
│   - save_session_memory(cwd, session_id, content) -> None
│
├── compressor.py            # 压缩逻辑
│   - SessionMemoryConfig (frozen dataclass)
│   - update_session_memory(messages, current_memory) -> str
│   - should_trigger(token_delta, tool_call_count) -> bool
│
└── prompts.py               # 压缩用 prompt 模板
    - COMPRESSION_SYSTEM_PROMPT
```

## 集成点

| 位置 | 修改 |
|------|------|
| `context/system_prompt.py` | `build()` 中注入 session-memory.md 内容 |
| `query_engine/engine.py` | `_query_loop()` 每轮结束后检查是否触发压缩 |
| `repl.py` | 传入 `LLMProvider` 给压缩模块用于 LLM 调用 |

## 关键设计决策

1. **结构化模板**：固定 section 模板确保信息分类清晰，LLM 不会遗漏关键信息。

2. **增量触发**：不是每轮都压缩，而是基于 token 增量和工具调用次数的阈值判断，避免在短对话中浪费 API 调用。

3. **全量重写**：每次触发时重写整个文件（而非追加），确保信息去重和优先级排序。

4. **会话级生命周期**：压缩文件随会话创建、随会话结束而废弃，不跨会话。

5. **读不创建目录**：`load_session_memory` 在目录不存在时返回 None，`save_session_memory` 在写入时才创建目录。避免在读操作时产生副作用。

6. **复用 LLMProvider**：压缩调用直接用项目的 `LLMProvider`，不引入新的 API 客户端。

## 与其他模块的交互

```
compression/
  ├── 被调用 ← query_engine/engine.py     每轮对话后检查是否触发压缩
  ├── 被调用 ← context/system_prompt.py   加载 session-memory.md
  ├── 调用 → providers/base.py            压缩需要 LLM 调用（通过 LLMProvider）
  └── 被测试 ← tests/compression/
      ├── test_store.py
      └── test_compressor.py
```
