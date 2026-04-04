# memory — 记忆系统

对应源码：`cc/memory/`（session_memory.py, extractor.py）
对应 TS 源码：`memdir/`, `services/extractMemories/`

## 模块概述

memory 模块实现了跨对话的持久化记忆系统，包含两个组件：

- **session_memory.py** — 记忆的存储基础设施：目录管理、文件读写、索引加载
- **extractor.py** — 记忆的自动提取：对话结束后后台分析并保存有价值的记忆

## 存储架构

```
~/.claude/projects/{project_id}/memory/
│
├── MEMORY.md                    索引入口（被加载到系统提示词中）
│   ├── - [用户角色](user_role.md) — 用户画像描述
│   ├── - [测试反馈](feedback_testing.md) — 测试偏好
│   └── ... （限制 200 行）
│
├── user_role.md                 记忆文件（含 frontmatter）
│   ---
│   name: user_role
│   description: 用户的角色和偏好
│   type: user
│   ---
│   用户是数据科学家，关注可观测性...
│
├── feedback_testing.md
├── project_release_plan.md
└── ...
```

### 项目 ID 生成

```
cwd 路径 → sha256 hash → 取前 12 位
例: /home/user/projects/myapp → "a1b2c3d4e5f6"

特点：确定性，同一目录永远映射到同一 ID
修复：使用 hashlib.sha256 而非 hash()（后者受 PYTHONHASHSEED 影响不稳定）
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
│  内容：外部系统指针（Linear 项目、Slack 频道、监控面板）        │
│  时机：了解到外部资源的位置和用途                               │
│  用途：需要外部信息时知道去哪找                                 │
└─────────────────────────────────────────────────────────────┘
```

## 自动提取流程

```
REPL 中每轮对话结束后（main.py）
│
├── 检查增量消息数 ≥ MIN_NEW_MESSAGES(4)?
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
│   ├── 调用 call_model（max_tokens=1024）
│   │
│   ├── 解析 JSON 响应
│   │   ├── {"memories": [{"name", "type", "content"}, ...]}
│   │   └── 容错：提取 ```json``` 代码块
│   │
│   └── 逐个 save_memory()
│       └── 文件名清洗 → 写入 .md 文件
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

## 记忆在系统提示词中的注入

```
main.py._build_system()
│
├── get_memory_dir(cwd)           确定记忆目录路径
├── load_memory_index(cwd)        读取 MEMORY.md 内容
│
└── build_system_prompt(...)
    └── build_memory_prompt(memory_dir, memory_index_content)
        ├── 四类记忆行为指导（保存/访问/验证规则）
        └── MEMORY.md 内容（截断到 200 行）
```

模型通过系统提示词了解记忆系统的存在、知道如何读写记忆文件、以及 MEMORY.md 索引的当前内容。

## 关键设计决策

1. **后台异步提取**：记忆提取使用 `asyncio.create_task` 作为 fire-and-forget 后台任务，不阻塞用户下一轮输入。即使提取失败也不影响主流程。

2. **最小增量阈值**：少于 4 条新消息不触发提取，避免在短对话中浪费 API 调用。

3. **文件名清洗**：非字母数字字符替换为 `_`，防止文件名注入。

4. **MEMORY.md 索引截断**：超过 200 行截断，索引过大时模型收到警告提示。

5. **读不创建目录**：`load_memories` 在目录不存在时返回空列表，`save_memory` 在写入时才创建目录。避免在读操作时产生副作用。

## 与其他模块的交互

```
memory/
  ├── 被调用 ← main.py                 后台记忆提取（每轮对话后）
  ├── 被调用 ← prompts/builder.py      加载 MEMORY.md 索引
  ├── 调用 → api (通过 call_model)      提取记忆需要 API 调用
  ├── 被依赖 → prompts/sections.py     build_memory_prompt 引用常量
  └── 被测试 ← tests/unit/tools/test_memory.py
      tests/unit/tools/test_extractor.py
```