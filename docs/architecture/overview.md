# 架构总览

## 系统定位

Mini Claude Code 是一个基于异步事件驱动的多智能体协作编程助手。系统接收用户输入，通过流式 LLM 推理生成响应，执行工具调用（文件读写、Bash 命令、搜索等），并支持将复杂任务委派给多个子智能体并行处理。

## 分层架构

系统采用四层架构设计，上层依赖下层，下层不感知上层：

```
┌─────────────────────────────────────────────────────────┐
│                     应用层 (app)                         │
│   CLI 入口  ·  REPL 交互循环  ·  Textual TUI 界面        │
├─────────────────────────────────────────────────────────┤
│                    运行时核心 (runtime)                   │
│   QueryEngine  ·  ToolExecutor  ·  AgentManager         │
│   RuntimeFacade  ·  Compaction  ·  AgentCoordinator     │
├─────────────────────────────────────────────────────────┤
│                   基础设施层 (infrastructure)             │
│   Providers  ·  Tools  ·  Context  ·  Task  ·  Models   │
├─────────────────────────────────────────────────────────┤
│                    横切特性层 (features)                  │
│   长期记忆 (Memory)  ·  上下文压缩 (Compression)          │
└─────────────────────────────────────────────────────────┘
```

## 模块依赖关系图

```
                        ┌──────────┐
                        │   CLI    │
                        └────┬─────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         ┌────────┐    ┌─────────┐   ┌──────────┐
         │  REPL  │    │   TUI   │   │ Harness  │
         └───┬────┘    └────┬────┘   └────┬─────┘
             │              │             │
             └──────┬───────┘             │
                    ▼                     │
             ┌──────────────┐             │
             │ RuntimeFacade│◄────────────┘
             └──────┬───────┘
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
  ┌──────────┐ ┌─────────┐ ┌──────────────┐
  │  Query   │ │  Agent  │ │  Execution   │
  │ Engine   │ │ Manager │ │  Executor    │
  └────┬─────┘ └────┬────┘ └──────┬───────┘
       │            │             │
       │     ┌──────┴──────┐      │
       │     ▼             ▼      │
       │ ┌────────┐ ┌──────────┐  │
       │ │SubAgent│ │ EventBus │  │
       │ └────────┘ └──────────┘  │
       │                          │
       └──────────┬───────────────┘
                  ▼
    ┌─────────────────────────────┐
    │       EngineContext         │
    └──────────┬──────────────────┘
               │
   ┌───────────┼───────────────┬────────────┐
   ▼           ▼               ▼            ▼
┌────────┐ ┌────────┐   ┌───────────┐ ┌──────────┐
│Provider│ │Context │   │   Tools   │ │ Features │
│        │ │Assembler│  │ Registry  │ │ Mem/Comp │
└────────┘ └────────┘   └───────────┘ └──────────┘
```

## 核心设计决策

| 决策 | 说明 |
|------|------|
| 异步流式架构 | 所有 LLM 交互均采用异步流式处理，基于 `AsyncGenerator` 逐步产生事件 |
| 事件状态机 | Agent Loop 通过事件类型（TextDelta / ToolCallStart / ToolResultEvent 等）驱动状态流转 |
| 工具分类执行 | 安全工具（只读）并发执行，非安全工具（写入/执行）串行执行 |
| 子智能体隔离 | 子智能体通过 git worktree 实现工作区隔离，失败时通过快照回滚 |
| 上下文压缩 | 支持自动和被动两种压缩模式，防止上下文溢出 |
| 文件持久化 | 任务状态和运行状态均使用文件持久化，支持跨进程恢复 |

## 关键交互流程

### 用户消息处理主流程

```
用户输入
   │
   ▼
CLI / TUI / REPL
   │
   ▼
RuntimeFacade.submit_message()
   │
   ▼
EngineContext.submit_message()
   │
   ▼
QueryEngine（Agent Loop）
   │
   ├──► SystemPromptBuilder.assemble()  ──► 组装系统提示词
   │
   ├──► LLMProvider.stream()            ──► 流式获取 LLM 响应
   │        │
   │        ▼
   │    产生事件流（TextDelta / ToolCallStart ...）
   │
   ├──► collect_tool_calls()            ──► 收集工具调用
   │
   ├──► StreamingToolExecutor.execute() ──► 执行工具
   │        │
   │        ├── 安全工具 ──► 并发执行
   │        └── 非安全工具 ──► 串行执行
   │
   ├──► CompactionController            ──► 检查并执行上下文压缩
   │
   └──► 循环直到无更多工具调用
   │
   ▼
事件流返回上层渲染
```

### 子智能体委派流程

```
QueryEngine 检测到 agent 工具调用
   │
   ▼
AgentTool.execute()
   │
   ├── 写入型智能体 ──► AgentManager.dispatch_foreground()
   │                         │
   │                         ▼
   │                    创建 SubAgent + worktree
   │                         │
   │                         ▼
   │                    构建独立 EngineContext
   │                         │
   │                         ▼
   │                    运行 QueryEngine 直到完成
   │                         │
   │                         ▼
   │                    收集输出，合并结果
   │
   └── 只读型智能体 ──► AgentManager.dispatch_background()
                             │
                             ▼
                        异步执行，通过 EventBus 通知完成
```
