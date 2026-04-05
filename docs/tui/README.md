# TUI 界面设计

## 概述

Mini Claude Code 提供基于 Textual 框架的终端用户界面（TUI），作为主要的交互方式。TUI 支持流式显示、工具调用折叠、子 Agent 管理面板、斜杠命令补全和文件路径补全等功能。

## 整体布局

```
┌─ ChatScreen ──────────────────────────────────────────┐
│                                                        │
│  ┌─ ChatArea（可滚动）───────────────────────────────┐ │
│  │  用户消息                                          │ │
│  │  Agent 回复（Markdown 渲染）                       │ │
│  │  工具调用结果（CollapsibleTool 可折叠）             │ │
│  │  子 Agent 活动（AgentToolStrip 实时进度条）        │ │
│  │  系统提示                                          │ │
│  └────────────────────────────────────────────────────┘ │
│                                                        │
│  ┌─ InputArea ───────────────────────────────────────┐ │
│  │  多行输入框                                        │ │
│  │  Enter 发送 / Shift+Enter 换行                    │ │
│  │  Tab 切换 Plan/Build 模式                          │ │
│  │  / 触发斜杠命令补全                                │ │
│  │  @ 触发文件路径补全                                │ │
│  └────────────────────────────────────────────────────┘ │
│                                                        │
│  ┌─ StatusBar ───────────────────────────────────────┐ │
│  │  模式指示 │ 模型名称 │ 活动动画 │ 快捷键提示      │ │
│  └────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────┘
```

## 模块结构

```
src/mini_cc/tui/
├── __init__.py
├── app.py                   MiniCCApp：TUI 根应用
├── theme.py                 Theme：颜色主题常量
├── commands.py              斜杠命令定义与匹配
├── screens/
│   ├── chat_screen.py       ChatScreen：主聊天屏幕
│   └── agent_screen.py      AgentScreen：子 Agent 管理面板
└── widgets/
    ├── chat_area.py         ChatArea：消息滚动区域
    ├── input_area.py        InputArea：多行输入框
    ├── status_bar.py        StatusBar：底部状态栏
    ├── collapsible_tool.py  CollapsibleTool：工具结果折叠组件
    ├── agent_tool_strip.py  AgentToolStrip：子 Agent 实时进度条
    └── completion_popup.py  CompletionPopup：补全弹窗
```

## 屏幕

### ChatScreen（主屏幕）

ChatScreen 是应用启动后的主屏幕，管理完整的聊天流程：

**核心职责：**
- 管理 QueryState（对话状态）和运行模式（Plan/Build）
- 处理用户输入并发送给 QueryEngine
- 流式渲染所有事件类型（文本、工具调用、Agent 事件、压缩通知等）
- 管理流式任务的生命周期（启动、中断、完成）
- 支持队列输入（当前任务完成后自动发送下一条消息）

**快捷键绑定：**

| 快捷键 | 功能 |
|--------|------|
| Tab | 切换 Plan/Build 模式 |
| Ctrl+A | 打开 Agent 管理面板 |
| Esc | 中断当前流式响应 |
| Ctrl+D / Ctrl+Q | 退出应用 |

**斜杠命令处理：**

| 命令 | 功能 |
|------|------|
| /help | 显示帮助信息 |
| /compact | 手动触发上下文压缩 |
| /clear | 清空聊天记录 |
| /mode | 切换 Plan/Build 模式 |
| /agents | 打开 Agent 管理面板 |
| /exit | 退出应用 |

### AgentScreen（Agent 管理面板）

AgentScreen 提供子 Agent 的可视化管理界面：

**功能：**
- 列出所有活跃和已完成的子 Agent
- 每个条目显示状态图标和颜色标识
- 支持上下键导航、Enter 查看详情、C 取消 Agent、R 刷新
- 详情面板显示 Agent 配置、worktree 路径、fork 状态、输出预览

## 组件

### ChatArea（聊天区域）

ChatArea 是主要的消息显示区域，继承自 Textual 的 VerticalScroll：

**渲染方法：**
- 用户消息：带角色标签的格式化文本
- Agent 回复：流式 Markdown 渲染（实时追加文本片段）
- 工具调用/结果：使用 CollapsibleTool 可折叠展示
- 子 Agent 事件：使用 AgentToolStrip 实时进度条
- 系统消息：灰色/黄色提示文本

**Agent 颜色分配：** 每个子 Agent 自动分配不同颜色，来自预定义的 agent_colors 调色板。

### InputArea（输入区域）

InputArea 是多行文本输入框，基于 Textual 的 TextArea：

**功能：**
- Enter 发送消息，Shift+Enter 插入换行
- 上下箭头浏览命令历史
- Tab 键切换 Plan/Build 模式
- / 触发斜杠命令补全弹窗
- @ 触发文件路径补全弹窗
- Ctrl+P 打开命令面板

### CompletionPopup（补全弹窗）

CompletionPopup 提供两种补全模式：

**斜杠命令补全：**
- 由 / 字符触发
- 显示所有可用命令及其描述
- 支持前缀模糊匹配

**文件路径补全：**
- 由 @ 字符触发
- 使用 git ls-files 扫描项目文件（回退到 rglob）
- 30 秒 TTL 文件缓存
- 基于段的模糊匹配评分
- 防抖异步文件扫描

### StatusBar（状态栏）

StatusBar 显示在底部，包含以下信息：

| 区域 | 内容 |
|------|------|
| 模式指示 | Plan（只读）或 Build（读写） |
| 模型名称 | 当前使用的 LLM 模型 |
| 活动动画 | Braille 点阵旋转动画（思考中/Agent 活动中） |
| 快捷键提示 | Tab 切换模式、Ctrl+A Agent 面板 |

### CollapsibleTool（工具折叠组件）

可折叠的工具调用结果展示：

- 标题栏显示工具名称和成功/失败图标
- 默认折叠，点击展开查看完整输出
- 截断预览显示工具结果的前几行

### AgentToolStrip（Agent 进度条）

子 Agent 的实时活动进度展示：

- 运行中的工具显示闪烁动画
- 已完成的工具显示成功/失败标记
- Agent 完成时显示汇总行（工具调用次数、耗时等）

## 与其他模块的交互

| 交互 | 说明 |
|------|------|
| context/engine_context.py | 通过 create_engine() 获取 EngineContext（包含 QueryEngine、SystemPromptBuilder 等） |
| query_engine/engine.py | 调用 submit_message() 获取事件流，逐事件渲染 |
| compression/compressor.py | /compact 命令触发手动压缩 |
| agent/manager.py | 通过 AgentManager 跟踪子 Agent 状态 |

## 主题

Theme 数据类定义了所有颜色常量，确保 TUI 界面风格一致。采用暗色方案，各组件引用统一的颜色变量。
