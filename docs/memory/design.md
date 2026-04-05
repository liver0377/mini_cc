# 中期记忆系统设计文档

## 概述

中期记忆解决的是**跨会话持久化**问题——让 agent 在不同会话中记住用户的偏好、项目的决策、以及外部资源的位置。

记忆系统完全基于文件，不包含任何数据库或向量存储，只有 Markdown。所有记忆文件存储在 ~/.mini_cc/projects/{project_id}/memory/ 目录下。

> 上下文压缩（会话级短期摘要）是独立子系统，参见 compression 设计文档。

## 存储路径

```
~/.mini_cc/
└── projects/{project_id}/
    └── memory/
        ├── MEMORY.md              索引入口，被注入系统提示词（≤ 200 行）
        ├── user_role.md           各记忆文件，含 YAML frontmatter
        ├── feedback_testing.md
        ├── project_release.md
        └── reference_ci.md
```

### 项目 ID 生成

项目 ID 由当前工作目录路径经 SHA-256 哈希后取前 12 位生成。同一目录永远映射到同一 ID（确定性）。使用 hashlib.sha256，不受 PYTHONHASHSEED 影响。

### 记忆文件格式

每个记忆文件使用 Markdown 格式，头部为 YAML frontmatter：

| 字段 | 说明 |
|------|------|
| name | 记忆名称（同时作为文件名） |
| description | 简要说明 |
| type | 分类（user / feedback / project / reference） |

frontmatter 下方为自由格式的 Markdown 正文。

## 四类记忆分类

| 类型 | 内容 | 保存时机 | 用途 |
|------|------|----------|------|
| **user** | 用户角色、偏好、专业领域、目标 | 了解到用户身份信息时 | 根据用户画像调整交互风格 |
| **feedback** | 用户对 Agent 行为的纠正/确认 | 用户说"不要/停止/就这样"时 | 避免重复犯错 |
| **project** | 项目进展、决策、截止日期 | 了解到"谁在做什么、为什么、什么时候" | 维护项目上下文（信息衰减快） |
| **reference** | 外部系统指针（CI 平台、文档站点、监控面板） | 了解到外部资源的位置和用途 | 需要外部信息时知道去哪找 |

feedback 类型有固定格式：规则 → Why → How to apply。

## 自动提取流程

### 触发条件

QueryEngine._query_loop 每轮结束后，检查增量消息数是否 ≥ MIN_NEW_MESSAGES（默认 4）。不够则跳过提取。

### 提取过程

提取以 fire-and-forget 后台任务方式运行（asyncio.create_task），不阻塞下一轮对话：

1. 加载已有记忆列表（用于去重）
2. 格式化最近消息为文本
3. 构建提取 prompt（EXTRACTION_SYSTEM_PROMPT + 已有记忆 + 最近对话）
4. 调用 LLM（max_tokens=1024）
5. 解析 JSON 响应（{"memories": [{"name", "type", "content"}, ...]}）
6. 容错处理：支持从 \`\`\`json\`\`\` 代码块中提取
7. 逐个 save_memory()：文件名清洗 → 写入 .md 文件 → 更新 MEMORY.md 索引

### 后台任务管理

- 后台任务加入 _bg_tasks set 防止 GC 回收
- 任务完成后通过 done_callback 自动从 set 中移除
- 异常被内部 try/except 捕获，仅 debug 日志记录

### MEMORY.md 索引

- 超过 200 行自动截断
- 索引过大时模型收到警告提示
- 每条记忆保存后自动更新索引

## 系统提示词注入

SystemPromptBuilder.build() 在已有的 static prompts + env info + AGENTS.md 之后，追加 MEMORY.md 索引内容。

完整的系统提示词构成：

1. 静态 prompt（intro.md, rules.md, caution.md, tool_guide.md）
2. 环境信息（\<env\> 标签）
3. AGENTS.md（用户手动维护的项目指令）
4. MEMORY.md 索引（中期记忆目录，≤ 200 行）

## 模块结构

```
src/mini_cc/memory/
├── __init__.py              公共导出
├── store.py                 存储基础设施
│   ├── project_id()            生成项目 ID（sha256(cwd)[:12]）
│   ├── get_memory_dir()        获取记忆目录路径
│   ├── load_memory_index()     读取 MEMORY.md
│   ├── list_memories()         列出所有记忆元信息
│   ├── save_memory()           保存记忆 + 更新索引
│   ├── _sanitize_filename()    文件名清洗
│   └── _rebuild_index()        重建 MEMORY.md
├── extractor.py              中期记忆提取
│   ├── MemoryExtractor         提取器类
│   ├── should_extract()        判断是否需要提取
│   ├── fire_and_forget()       启动后台提取任务
│   ├── extract_memories()      执行提取
│   └── _parse_extraction_response()  解析 LLM 响应
└── prompts.py                提取用 prompt 模板
    └── EXTRACTION_SYSTEM_PROMPT
```

## 集成点

| 位置 | 说明 |
|------|------|
| context/system_prompt.py | build() 中注入 MEMORY.md 索引 |
| query_engine/engine.py | _query_loop() 每轮结束后触发中期记忆提取（作为 post_turn_hook） |
| context/engine_context.py | create_engine() 中创建 MemoryExtractor 实例 |

## 关键设计决策

1. **增量提取**：每轮对话后异步提取，增量消息 ≥ 4 条时触发。不怕异常退出，后台任务不阻塞主流程。

2. **四类分类**：user / feedback / project / reference，覆盖用户偏好、行为纠正、项目事实、外部资源四个维度。

3. **纯文件存储**：Markdown + YAML frontmatter，无数据库，无向量存储。

4. **读不创建目录**：load_memories 在目录不存在时返回空列表，save_memory 在写入时才创建目录。避免在读操作时产生副作用。

5. **MEMORY.md 索引截断**：超过 200 行截断，索引过大时模型收到警告提示。

6. **文件名清洗**：非字母数字字符替换为下划线，防止文件名注入。

7. **复用 LLMProvider**：记忆提取直接用项目的 LLMProvider，不引入新的 API 客户端。

## 与其他模块的交互

- **被 query_engine/engine.py 调用**：每轮对话后触发记忆提取
- **被 context/system_prompt.py 调用**：加载 MEMORY.md 索引注入系统提示词
- **调用 providers/base.py**：提取记忆需要 LLM 调用（通过 LLMProvider）
- **被 tests/memory/ 测试**
