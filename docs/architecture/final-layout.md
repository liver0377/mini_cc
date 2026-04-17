# Mini Claude Code 最终架构与无兼容层重构方案

## 一、目标

本方案描述 `src/mini_cc` 的最终目标形态。

目标不是“在旧路径上继续加 wrapper”，而是：

- 完全移除兼容层
- 让目录结构直接表达系统分层
- 让包级导出成为唯一稳定入口
- 禁止仓库内部继续依赖旧命名空间

完成后，仓库内部不应再存在这些目录作为一等实现层：

- `mini_cc.tui`
- `mini_cc.compression`
- `mini_cc.memory`
- `mini_cc.agent`
- `mini_cc.query_engine`
- `mini_cc.tool_executor`
- `mini_cc.harness.task_audit_plugins`

它们要么被删除，要么只在一次性迁移分支中短暂存在，不应进入最终主线。

---

## 二、最终目录

目标目录：

```text
src/mini_cc/
├── __init__.py
├── __main__.py
├── app/
│   ├── __init__.py
│   ├── cli.py
│   ├── repl.py
│   └── tui/
│       ├── __init__.py
│       ├── app.py
│       ├── commands.py
│       ├── theme.py
│       ├── screens/
│       └── widgets/
├── context/
│   ├── __init__.py
│   ├── engine_context.py
│   ├── system_prompt.py
│   ├── tool_use.py
│   └── prompts/
├── features/
│   ├── __init__.py
│   ├── compression/
│   │   ├── __init__.py
│   │   ├── compressor.py
│   │   └── prompts.py
│   └── memory/
│       ├── __init__.py
│       ├── extractor.py
│       ├── prompts.py
│       └── store.py
├── harness/
│   ├── __init__.py
│   ├── audit/
│   │   ├── __init__.py
│   │   ├── core.py
│   │   └── plugins/
│   ├── bootstrap.py
│   ├── checkpoint.py
│   ├── dispatch_roles.py
│   ├── doc_generator.py
│   ├── events.py
│   ├── iteration.py
│   ├── judge.py
│   ├── models.py
│   ├── policy.py
│   ├── runner.py
│   ├── scheduler.py
│   ├── step_runner.py
│   └── supervisor.py
├── models/
│   ├── __init__.py
│   ├── agent.py
│   ├── events.py
│   ├── message.py
│   ├── query.py
│   └── task.py
├── providers/
│   ├── __init__.py
│   ├── base.py
│   └── openai.py
├── runtime/
│   ├── __init__.py
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── bus.py
│   │   ├── dispatcher.py
│   │   ├── manager.py
│   │   ├── snapshot.py
│   │   └── sub_agent.py
│   ├── execution/
│   │   ├── __init__.py
│   │   └── executor.py
│   └── query/
│       ├── __init__.py
│       └── engine.py
├── task/
│   ├── __init__.py
│   └── service.py
└── tools/
    ├── __init__.py
    ├── agent_tool.py
    ├── base.py
    ├── bash.py
    ├── file_edit.py
    ├── file_read.py
    ├── file_write.py
    ├── glob.py
    ├── grep.py
    ├── plan_agents.py
    └── scan_dir.py
```

---

## 三、最终分层

### 1. `app`

职责：

- 处理 CLI 入口
- 处理 REPL 交互
- 处理 TUI 展示

约束：

- 不直接承担核心业务逻辑
- 只组装 `context`、`runtime`、`harness`
- 不定义协议级模型

### 2. `context`

职责：

- 组装系统提示词
- 构建运行环境信息
- 维护 tool-use 上下文
- 构建 `EngineContext`

约束：

- 不直接实现 Agent 生命周期
- 不直接实现调度器

### 3. `features`

职责：

- 放横切能力
- 当前包括：
  - `compression`
  - `memory`

约束：

- 不直接依赖 TUI
- 不直接承担 run 编排

### 4. `runtime`

职责：

- 放交互运行时核心
- 当前包括：
  - `agents`
  - `query`
  - `execution`

约束：

- 不依赖 `app`
- 不依赖 `harness` 的上层策略
- 只负责执行，不负责 run 级调度

### 5. `harness`

职责：

- 长时运行与恢复
- step/work item 编排
- scheduler / policy / trace / checkpoint
- task-specific audit

约束：

- 通过 `runtime` 调执行能力
- 不直接变成 UI 层

### 6. `models`

职责：

- 共享协议模型
- 消息、事件、query、task、agent 基础数据模型

约束：

- 只放跨层通用模型
- 不放强业务行为

---

## 四、最终导入规则

### 1. 仓库内部禁止引用旧兼容路径

最终主线中，以下导入必须为零：

```python
from mini_cc.tui ...
from mini_cc.compression ...
from mini_cc.memory ...
from mini_cc.agent ...
from mini_cc.query_engine ...
from mini_cc.tool_executor ...
```

### 2. 优先使用包级入口

仓库内部优先使用：

```python
from mini_cc.runtime.agents import AgentManager
from mini_cc.runtime.query import QueryEngine
from mini_cc.runtime.execution import StreamingToolExecutor
from mini_cc.features.memory import MemoryExtractor
from mini_cc.features.compression import compress_messages
```

只在包内部实现需要时，才允许引用更细粒度模块。

### 3. `__init__.py` 是稳定出口

要求：

- 每个一等目录都必须通过 `__init__.py` 暴露稳定 API
- 上层模块默认依赖包级导出，而不是文件级路径

---

## 五、必须删除的兼容层

最终删除清单：

- `src/mini_cc/tui/`
- `src/mini_cc/compression/`
- `src/mini_cc/memory/`
- `src/mini_cc/agent/`
- `src/mini_cc/query_engine/`
- `src/mini_cc/tool_executor/`
- `src/mini_cc/harness/task_audit_plugins/`

注意：

- 删除前必须确保 `src/`、`tests/`、`scripts/`、`docs/` 中对旧路径的引用全部清零
- 删除后必须更新所有文档、测试和脚本

---

## 六、推荐迁移顺序

### Phase A：主实现迁移

目标：

- 新目录成为真实实现位置
- 旧目录仍保留兼容层

完成标准：

- 主干源码全部改到新路径

### Phase B：测试与脚本迁移

目标：

- `tests/` 和 `scripts/` 不再引用旧路径

完成标准：

- `rg` 在 `tests scripts` 中搜索不到旧路径导入

### Phase C：包级 API 收口

目标：

- 改用包级导出
- 减少文件级路径耦合

完成标准：

- 上层模块优先依赖包级 `__init__`

### Phase D：兼容层删除

目标：

- 真正删除旧目录

完成标准：

- 仓库中不存在旧兼容目录
- 全量测试通过
- 文档索引更新完毕

---

## 七、当前仓库与最终目标的差距

截至当前状态，已经完成：

- `app/` 已成为 CLI / REPL / TUI 的真实实现层
- `features/` 已成为 `compression` / `memory` 的真实实现层
- `runtime/` 已成为 `agents` / `query` / `execution` 的真实实现层
- `harness/audit/` 已成为审计插件真实实现层
- 主干源码与测试已大面积迁移到新路径

仍未完成：

- 旧兼容目录尚未物理删除
- 兼容目录依然存在 wrapper 文件
- 文档中仍然混杂少量旧命名空间表述

因此，当前状态应定义为：

- “分层重构已完成”
- “兼容层剥离未完成”

---

## 八、完成判定

当且仅当满足以下条件，才算“目录重构完成”：

1. `src/mini_cc` 中不再存在旧兼容目录
2. `src/` 内部不存在旧路径导入
3. `tests/` 内不存在旧路径导入
4. `scripts/` 内不存在旧路径导入
5. 全量 `pytest` 通过
6. 全量 `ruff check .` 通过
7. 目标范围内 `mypy` 通过
8. `docs/README.md` 与相关模块文档已切换到新结构描述

在此之前，不应宣称“完全重构完成”。
