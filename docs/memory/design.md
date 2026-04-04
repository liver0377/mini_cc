# 中期记忆系统设计文档

## 概述

中期记忆解决的是**跨会话持久化**问题——让 agent 在不同会话中记住用户的偏好、项目的决策、以及外部资源的位置。

记忆系统完全基于文件，不包含任何数据库或向量存储，只有 Markdown。所有记忆文件存储在 `~/.mini_cc/projects/{project_id}/memory/` 目录下。

> 上下文压缩（会话级短期摘要）是独立子系统，参见 `docs/compression/`。

## 存储路径

```
~/.mini_cc/
└── projects/{project_id}/
    └── memory/
        ├── MEMORY.md              # 索引入口，被注入系统提示词（<= 200 行）
        │   # 格式：- [描述](filename.md) — 简要说明
        │
        ├── user_role.md           # 各记忆文件，含 YAML frontmatter
        │   ---
        │   name: user_role
        │   description: 用户的角色和偏好
        │   type: user
        │   ---
        │   用户是 ...，偏好 ...
        │
        ├── feedback_testing.md
        │   ---
        │   name: feedback_testing
        │   type: feedback
        │   ---
        │   规则: ...
        │   Why: ...
        │   How to apply: ...
        │
        ├── project_release.md
        └── reference_ci.md
```

### 项目 ID 生成

```
cwd 路径 → sha256 hash → 取前 12 位
例: /home/user/projects/myapp → "a1b2c3d4e5f6"

特点：确定性，同一目录永远映射到同一 ID
实现：使用 hashlib.sha256（不受 PYTHONHASHSEED 影响）
```

## 四类记忆分类

```
┌─────────────────────────────────────────────────────────────┐
│  type: user                                                 │
│  内容：用户角色、偏好、专业领域、目标                           │
│  时机：了解到用户身份信息时保存                                 │
│  用途：根据用户画像调整交互风格                                 │
├─────────────────────────────────────────────────────────────┤
│  type: feedback                                             │
│  内容：用户对 agent 行为的纠正/确认                            │
│  时机：用户说"不要/停止/就这样"时保存                           │
│  格式：规则 → Why: → How to apply:                          │
├─────────────────────────────────────────────────────────────┤
│  type: project                                              │
│  内容：项目进展、决策、截止日期                                 │
│  时机：了解到"谁在做什么、为什么、什么时候"                     │
│  特点：信息衰减快，需要验证时效性                               │
├─────────────────────────────────────────────────────────────┤
│  type: reference                                            │
│  内容：外部系统指针（CI 平台、文档站点、监控面板）               │
│  时机：了解到外部资源的位置和用途                               │
│  用途：需要外部信息时知道去哪找                                 │
└─────────────────────────────────────────────────────────────┘
```

## 自动提取流程

```
QueryEngine._query_loop 每轮结束后
│
├── 增量消息数 >= MIN_NEW_MESSAGES (4)?
│   └── 否 → 跳过提取
│
├── fire-and-forget 后台任务（asyncio.create_task）
│   │
│   ├── 加载已有记忆列表（用于去重）
│   ├── 格式化最近消息为文本
│   │
│   ├── 构建提取 prompt
│   │   ├── EXTRACTION_SYSTEM_PROMPT（四类定义 + 保存规则）
│   │   └── 用户消息（已有记忆 + 最近对话）
│   │
│   ├── 调用 LLM（max_tokens=1024）
│   │
│   ├── 解析 JSON 响应
│   │   ├── {"memories": [{"name", "type", "content"}, ...]}
│   │   └── 容错：提取 ```json``` 代码块
│   │
│   └── 逐个 save_memory()
│       └── 文件名清洗 → 写入 .md 文件 + 更新 MEMORY.md 索引
│
└── 不阻塞下一轮对话
```

### 后台任务管理

```
asyncio.create_task(_bg_extract(...))
│
├── 加入 _bg_tasks set（防止 GC 回收）
├── task.add_done_callback(_bg_tasks.discard)（完成后自动清理）
└── 异常被内部 try/except 捕获，仅 debug 日志
```

### MEMORY.md 索引

- 超过 200 行截断
- 索引过大时模型收到警告提示
- 每条记忆保存后自动更新索引

## 系统提示词注入

修改 `SystemPromptBuilder.build()`，在已有的 static prompts + env info + AGENTS.md 之后，追加 MEMORY.md 索引内容：

```
系统提示词构成：
1. 静态 prompt（intro.md, rules.md, caution.md, tool_guide.md）
2. 环境信息（<env> ... </env>）
3. AGENTS.md（用户手动维护的项目指令）
4. session-memory.md 摘要（上下文压缩，见 docs/compression/）
5. MEMORY.md 索引（中期记忆目录，<= 200 行）    ← 本模块
```

## 模块结构

```
src/mini_cc/memory/
├── __init__.py              # 公共导出
├── store.py                 # 存储基础设施
│   - project_id(cwd) -> str            # sha256(cwd)[:12]
│   - get_memory_dir(cwd) -> Path       # 记忆目录路径
│   - load_memory_index(cwd) -> str     # 读取 MEMORY.md
│   - list_memories(cwd) -> list[MemoryMeta]
│   - save_memory(cwd, name, type, content)  # 保存 + 更新索引
│   - _sanitize_filename(name) -> str   # 文件名清洗
│   - _rebuild_index(cwd)               # 重建 MEMORY.md
│
├── extractor.py              # 中期记忆提取
│   - MIN_NEW_MESSAGES = 4
│   - extract_memories(messages, existing_memories) -> list[MemoryItem]
│   - _bg_extract(...)        # 后台异步任务
│   - _parse_extraction_response(text) -> list[MemoryItem]
│
└── prompts.py                # 提取用 prompt 模板
    - EXTRACTION_SYSTEM_PROMPT
```

## 集成点

| 位置 | 修改 |
|------|------|
| `context/system_prompt.py` | `build()` 中注入 MEMORY.md 索引 |
| `query_engine/engine.py` | `_query_loop()` 每轮结束后触发中期记忆提取 |
| `repl.py` | 传入 `LLMProvider` 给记忆模块用于提取调用 |

## 关键设计决策

1. **增量提取**：每轮对话后异步提取，增量消息 >= 4 条时触发。不怕异常退出，后台任务不阻塞主流程。

2. **四类分类**：user / feedback / project / reference，覆盖用户偏好、行为纠正、项目事实、外部资源四个维度。

3. **纯文件存储**：Markdown + YAML frontmatter，无数据库，无向量存储。

4. **读不创建目录**：`load_memories` 在目录不存在时返回空列表，`save_memory` 在写入时才创建目录。避免在读操作时产生副作用。

5. **MEMORY.md 索引截断**：超过 200 行截断，索引过大时模型收到警告提示。

6. **文件名清洗**：非字母数字字符替换为 `_`，防止文件名注入。

7. **复用 LLMProvider**：记忆提取直接用项目的 `LLMProvider`，不引入新的 API 客户端。

## 与其他模块的交互

```
memory/
  ├── 被调用 ← query_engine/engine.py     每轮对话后触发记忆提取
  ├── 被调用 ← context/system_prompt.py   加载 MEMORY.md 索引
  ├── 调用 → providers/base.py            提取记忆需要 LLM 调用（通过 LLMProvider）
  └── 被测试 ← tests/memory/
      ├── test_store.py
      └── test_extractor.py
```
