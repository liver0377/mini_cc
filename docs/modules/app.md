# 应用层 (app)

应用层是系统的最上层，负责用户交互和界面渲染。包含三种交互模式：CLI 入口、REPL 交互循环、Textual TUI 全屏界面。

## 模块结构

```
app/
├── cli.py              # Typer CLI 命令定义
├── repl.py             # REPL 交互循环 + 事件渲染
├── presentation.py     # 共享展示逻辑
└── tui/                # Textual TUI 子系统
    ├── app.py          # 根应用
    ├── theme.py        # 主题配色
    ├── commands.py     # 斜杠命令
    ├── widgets/        # UI 组件
    │   ├── chat_area.py          # 聊天消息区
    │   ├── status_bar.py         # 状态栏
    │   ├── input_area.py         # 输入区
    │   ├── collapsible_tool.py   # 可折叠工具结果
    │   ├── agent_tool_strip.py   # 智能体工具条
    │   └── completion_popup.py   # 自动补全弹窗
    └── screens/        # 界面屏幕
        ├── chat_screen.py  # 聊天主屏幕
        ├── run_screen.py   # 运行时间线浏览器
        └── agent_screen.py # 智能体管理屏幕
```

## 架构图

```
                    ┌─────────────────┐
                    │    __main__      │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │     cli.py      │
                    │  Typer CLI App  │
                    └──┬──────────┬───┘
                       │          │
              ┌────────▼──┐  ┌───▼──────────┐
              │  chat 命令 │  │  tui 命令     │
              │  (REPL)    │  │  (Textual)    │
              └────┬───────┘  └───┬──────────┘
                   │               │
          ┌────────▼──────┐ ┌─────▼──────────┐
          │   repl.py     │ │   tui/app.py   │
          │               │ │  MiniCCApp     │
          │ ┌───────────┐ │ └───────┬────────┘
          │ │REPLConfig │ │         │
          │ │render_    │ │  ┌──────▼──────────┐
          │ │ event()   │ │  │  ChatScreen     │
          │ │run_       │ │  │  (主聊天界面)     │
          │ │ message() │ │  └──┬─────┬────────┘
          │ └───────────┘ │     │     │
          └───────┬───────┘     │     │
                  │        ┌────▼┐ ┌──▼──────────┐
                  │        │Widgets│ │RunScreen   │
                  │        │      │ │AgentScreen │
                  │        └──────┘ └────────────┘
                  │
         ┌────────▼──────────┐
         │  presentation.py  │
         │  rebuild_system_  │
         │  message()        │
         └───────────────────┘
```

## CLI 入口

`cli.py` 使用 Typer 框架定义两个子命令：

| 命令 | 说明 | 默认 |
|------|------|------|
| `tui` | 启动 Textual 全屏 TUI 界面 | 是（无子命令时默认执行） |
| `chat` | 启动 prompt_toolkit REPL 交互循环 | 否 |

## REPL 模式

REPL 模式提供基于终端的交互式对话体验：

| 组件 | 职责 |
|------|------|
| `REPLConfig` | REPL 配置（模型、API 密钥等） |
| `render_event()` | 将事件渲染为 Rich 格式输出（文本、工具调用、工具结果） |
| `run_message()` | 同步执行单条消息，启动异步 Agent Loop 并渲染事件流 |

**交互流程：**

```
用户输入
   │
   ▼
prompt_toolkit 读取输入
   │
   ▼
run_message() ──► 启动异步循环
   │
   ▼
消费事件流 ──► render_event() ──► Rich 控制台输出
   │
   ▼
等待下一条用户输入
```

## TUI 模式

TUI 模式基于 Textual 框架，提供全屏富交互界面。

### 屏幕层级

```
MiniCCApp（根应用）
   │
   └── ChatScreen（主屏幕）
         │
         ├── RunScreen（弹出：运行时间线浏览）
         └── AgentScreen（弹出：智能体管理）
```

### Widget 组成

```
ChatScreen
├── ChatArea                 # 聊天消息列表，支持 Markdown 流式渲染
│   ├── 用户消息
│   ├── 助手文本（流式增量）
│   └── 工具调用结果（CollapsibleTool，可折叠）
│
├── StatusBar                # 底部状态栏
│   ├── 当前模式
│   ├── 使用模型
│   ├── 活跃智能体数量
│   └── 加载旋转器
│
└── InputArea                # 输入区域
    ├── TextArea（多行输入）
    ├── 历史记录（上下箭头翻阅）
    └── CompletionPopup
        ├── 斜杠命令补全
        └── @文件路径模糊匹配
```

### 斜杠命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助信息 |
| `/clear` | 清空对话历史 |
| `/compact` | 手动触发上下文压缩 |
| `/model` | 切换模型 |
| `/agents` | 查看活跃智能体列表 |
| `/run` | 启动/查看 Harness 运行 |

### 主题系统

`Theme` 数据类定义全局配色方案，`DEFAULT_THEME` 提供默认值。涵盖：背景色、前景色、强调色、工具调用色、错误色、智能体相关色等。

## presentation.py

共享展示逻辑模块，提供：

| 组件 | 职责 |
|------|------|
| `rebuild_system_message()` | 在压缩后重建系统消息 |
| `COMPACT_LABELS` | 事件类型到显示标签的映射字典 |
