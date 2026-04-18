# 横切特性 (features)

横切特性层提供跨模块的可选功能，当前包含长期记忆和上下文压缩两大特性。

## 模块结构

```
features/
├── __init__.py
├── memory/                # 长期记忆
│   ├── __init__.py
│   ├── store.py           # 记忆存储（基于 Markdown frontmatter）
│   ├── extractor.py       # 记忆提取器（LLM 驱动）
│   └── prompts.py         # 提取提示词
└── compression/           # 上下文压缩
    ├── __init__.py
    ├── compressor.py      # 压缩器（LLM 驱动摘要）
    └── prompts.py         # 压缩提示词
```

## 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                     features 层                              │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                  长期记忆 (Memory)                      │  │
│  │                                                       │  │
│  │  MemoryExtractor          MemoryStore                 │  │
│  │  · 从对话中提取记忆        · Markdown frontmatter 存储 │  │
│  │  · Fire-and-forget 异步   · load_memory_index()       │  │
│  │  · LLM 驱动分类           · save_memory()             │  │
│  │                                                       │  │
│  │  记忆类别：                                            │  │
│  │  · user        — 用户偏好                              │  │
│  │  · project     — 项目信息                              │  │
│  │  · feedback    — 反馈经验                              │  │
│  │  · reference   — 参考资料                              │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                 上下文压缩 (Compression)               │  │
│  │                                                       │  │
│  │  CompactionController     Compressor                  │  │
│  │  · 判断是否需要压缩        · compress_messages()       │  │
│  │  · 管理压缩生命周期        · estimate_tokens()         │  │
│  │  · 触发自动/被动压缩       · replace_with_summary()    │  │
│  │                           · should_auto_compact()      │  │
│  │                                                       │  │
│  │  压缩策略：                                            │  │
│  │  · 自动压缩 — token 超阈值时主动触发                    │  │
│  │  · 被动压缩 — ContextLengthExceeded 时强制触发         │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 长期记忆 (Memory)

### 设计目标

从用户的对话中自动提取和持久化重要信息，在后续对话中注入系统提示词，实现跨会话的记忆能力。

### 工作流程

```
用户对话消息
      │
      ▼
MemoryExtractor.extract(messages)
      │
      ├── 1. 将消息发送给 LLM
      │       └── 使用专用提示词指导提取
      │
      ├── 2. LLM 分析并提取记忆项
      │       ├── 用户偏好（user）
      │       ├── 项目信息（project）
      │       ├── 反馈经验（feedback）
      │       └── 参考资料（reference）
      │
      ├── 3. 分类存储
      │       └── save_memory(category, content)
      │
      └── 4. Fire-and-forget
              └── 不阻塞主对话流程
```

### 存储格式

记忆使用 Markdown 文件存储，每条记忆以 frontmatter 格式记录元数据：

```
memory/
├── user.md          # 用户偏好
├── project.md       # 项目信息
├── feedback.md      # 反馈经验
└── reference.md     # 参考资料
```

**文件格式示例概念：**

```
---
category: user
created: "2026-01-15"
updated: "2026-01-15"
---
- 用户偏好使用 TypeScript
- 用户喜欢函数式编程风格
- ...
```

### 与系统提示词的集成

```
SystemPromptBuilder.assemble()
      │
      ├── ...（其他片段）
      │
      ├── 记忆索引
      │       │
      │       ▼
      │   load_memory_index()
      │       │
      │       ▼
      │   将所有记忆条目注入系统提示词
      │
      └── ...（其他片段）
```

## 上下文压缩 (Compression)

### 设计目标

在对话上下文超出 LLM 窗口限制之前或之时，通过 LLM 生成摘要来压缩历史消息，确保对话可以持续进行。

### 压缩流程

```
消息列表（累积增长）
      │
      ▼
should_auto_compact()
      │
      ├── 估算 token 数量
      │       └── estimate_tokens() — 使用 tiktoken
      │
      ├── 超过阈值？
      │       ├── 是 → 触发自动压缩
      │       └── 否 → 继续正常流程
      │
      ▼
compress_messages(messages)
      │
      ├── 保留最近 N 条消息（上下文窗口）
      │
      ├── 将历史消息发送给 LLM
      │       └── 使用压缩提示词生成摘要
      │
      ├── LLM 返回结构化摘要
      │
      └── 返回新消息列表
              ├── [SystemMessage]
              ├── [SummaryMessage]    ← 新生成的摘要
              └── [RecentMessages]    ← 保留的近期消息
```

### 两种压缩模式

| 模式 | 触发条件 | 行为 |
|------|----------|------|
| 自动压缩 | token 估算超过预设阈值 | 主动压缩，删除最早的历史消息 |
| 被动压缩 | LLM 返回 `ContextLengthExceeded` 错误 | 强制压缩后重试，压缩比更高 |

### CompactionController 集成

```
CompactionController
├── 在 QueryEngine 每轮循环后检查
│
├── should_compact_fn()
│   ├── 调用 estimate_tokens()
│   └── 与阈值比较
│
├── compact_fn()
│   ├── 调用 compress_messages()
│   └── 返回压缩后的消息列表
│
└── replace_summary_fn()
    ├── 替换 QueryState 中的消息列表
    └── yield CompactOccurred 事件
```

### token 估算

```
estimate_tokens(messages)
├── 使用 tiktoken 库
├── 对每条消息的 content 进行 token 计数
├── 加上工具调用的 token 估算
└── 返回总 token 数
```
