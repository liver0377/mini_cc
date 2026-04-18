# 上下文组装 (context)

上下文模块负责系统提示词的构建、工具使用上下文的管理，以及引擎上下文的组装。它是 LLM 与系统之间的桥梁，确保 LLM 接收到正确的系统指令和工具定义。

## 模块结构

```
context/
├── assembler.py           # 组件组装工厂
├── system_prompt.py       # 系统提示词构建器
├── tool_use.py            # 工具使用上下文
├── engine_context.py      # 引擎上下文（中央协调器）
└── prompts/               # 静态提示词模板
    ├── intro.md           #   主智能体介绍
    ├── intro_sub.md       #   子智能体介绍
    ├── rules.md           #   主智能体规则
    ├── rules_sub.md       #   子智能体规则
    ├── tool_guide.md      #   主智能体工具指南
    ├── tool_guide_sub.md  #   子智能体工具指南
    └── caution.md         #   注意事项
```

## 架构图

```
┌────────────────────────────────────────────────────────────┐
│                      assembler.py                          │
│                    create_engine() 工厂                     │
│                                                            │
│  组装顺序:                                                  │
│  Provider → Tools → SystemPromptBuilder → QueryEngine      │
│  → CompactionController → MemoryExtractor → EngineContext   │
└──────────────────────────┬─────────────────────────────────┘
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
┌──────────────────┐ ┌───────────┐ ┌───────────────┐
│ SystemPrompt     │ │ ToolUse   │ │ EngineContext │
│ Builder          │ │ Context   │ │               │
└────────┬─────────┘ └─────┬─────┘ └───────────────┘
         │                 │
         ▼                 ▼
┌──────────────────┐ ┌───────────────────┐
│ 静态模板文件      │ │ 工具注册表         │
│ prompts/*.md     │ │ ToolRegistry      │
└──────────────────┘ └───────────────────┘
```

## SystemPromptBuilder — 系统提示词构建器

系统提示词由多个片段按顺序组装而成：

```
SystemPromptBuilder.assemble()
   │
   ├── ① 静态模板
   │   ├── intro.md / intro_sub.md        # 智能体自我介绍
   │   ├── rules.md / rules_sub.md        # 行为规则
   │   ├── tool_guide.md / tool_guide_sub.md  # 工具使用指南
   │   └── caution.md                     # 注意事项
   │
   ├── ② 动态环境信息
   │   ├── EnvInfo（平台、日期、工作目录等）
   │   └── 根据运行环境动态注入
   │
   ├── ③ AGENTS.md（如果存在）
   │   └── 项目级自定义指令
   │
   ├── ④ 记忆索引
   │   └── 长期记忆摘要（Memory Feature）
   │
   ├── ⑤ Harness 运行上下文（如果在 Harness 模式下）
   │   ├── 经验教训（lessons）
   │   ├── 审查记录（reviews）
   │   └── 运行日志（journal）
   │
   └── ⑥ 工具定义
       └── 当前可用工具的 JSON Schema 描述
```

**主智能体与子智能体的区别：**

| 片段 | 主智能体 | 子智能体 |
|------|----------|----------|
| 介绍 | `intro.md` | `intro_sub.md` |
| 规则 | `rules.md` | `rules_sub.md` |
| 工具指南 | `tool_guide.md` | `tool_guide_sub.md` |
| 环境信息 | 包含完整信息 | 限定作用域信息 |
| 工具集 | 全部可用工具 | 根据角色限定 |

## ToolUseContext — 工具使用上下文

封装工具调用的运行时上下文：

```
ToolUseContext
├── 工具查找
│   └── 通过名称查找工具 → BaseTool 实例
│
├── 工具执行
│   ├── 输入校验（Pydantic Schema）
│   ├── 权限检查（ExecutionPolicy）
│   └── 执行工具 → ToolResult
│
├── 中断状态
│   └── 检测用户中断信号
│
└── 工具 Schema
    └── 提供工具的 JSON Schema 给 LLM
```

## EngineContext — 引擎上下文

引擎上下文是运行时的中央协调器，持有所有核心组件的引用：

```
EngineContext
│
├── 持有的组件
│   ├── query_engine           # QueryEngine 实例
│   ├── prompt_builder         # SystemPromptBuilder 实例
│   ├── agent_manager          # AgentManager 实例
│   ├── lifecycle_bus          # AgentEventBus 实例
│   ├── memory_extractor       # MemoryExtractor 实例
│   ├── compaction_ctrl        # CompactionController 实例
│   └── tool_use_ctx           # ToolUseContext 实例
│
├── 协程安全的上下文变量（contextvars）
│   ├── run_id                 # 当前运行 ID
│   ├── mode                   # 运行模式（PLAN / BUILD 等）
│   ├── budget                 # 预算控制
│   └── interrupt              # 中断标志
│
└── 核心方法
    ├── submit_message()       # 提交消息到 QueryEngine
    ├── new_query_state()      # 创建新的查询状态
    ├── execution_scope()      # 执行作用域（上下文管理器）
    │   ├── __aenter__: 设置 run_id、mode、budget
    │   └── __aexit__: 清理上下文变量
    └── compact_state()        # 触发上下文压缩
```

## create_engine() 组装工厂

`assembler.py` 中的 `create_engine()` 函数按以下顺序组装所有组件：

```
create_engine(config)
   │
   ├── 1. 创建 LLMProvider（ProviderFactory）
   │
   ├── 2. 创建工具注册表（ToolingFactory）
   │       ├── 注册所有基础工具
   │       └── 可选注册 agent_tool、plan_agents
   │
   ├── 3. 创建 SystemPromptBuilder
   │       └── 加载静态模板文件
   │
   ├── 4. 创建 QueryEngine
   │
   ├── 5. 创建 CompactionController
   │       └── 注入压缩函数和判断函数
   │
   ├── 6. 创建 MemoryExtractor
   │       └── 使用独立的 LLM 提供者
   │
   ├── 7. 创建 ToolUseContext
   │       └── 包装工具注册表和执行策略
   │
   └── 8. 创建 EngineContext
           └── 组装所有组件
```
