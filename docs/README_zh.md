# Mini Claude Code

[![CI](https://github.com/liver0377/mini_cc/actions/workflows/ci.yml/badge.svg)](https://github.com/liver0377/mini_cc/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://docs.astral.sh/ruff/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[**English**](../README.md)

> 基于 Python 实现的轻量级多 Agent 协作代码助手 CLI

## 项目愿景

Mini Claude Code 旨在用纯 Python 构建一个轻量、易扩展的命令行代码智能体，支持多 Agent 协作，能够理解自然语言指令并自动完成代码编写、文件操作、测试执行等任务。

## 效果展示
![](../assets/mini-cc.gif)

## 功能特性

- [x] 多 Agent 协作与通信（AgentManager、SubAgent、事件系统）
- [x] File Tool、Shell Tool、Glob/Grep 搜索工具
- [x] TUI 界面（Textual：聊天区、工具折叠、Agent 管理面板、状态栏）
- [x] Sub-Agent Worktree 隔离
- [x] 文件快照回滚（SnapshotService）
- [x] Plan/Build 模式切换（Tab 键）
- [x] 异步 Agent Loop + 流式输出
- [x] OpenAI 兼容 Provider
- [x] 中断/取消支持（Esc 键）
- [x] 短期记忆（上下文压缩）+ 长期记忆（跨会话持久化）
- [x] 斜杠命令（/help、/compact、/clear、/mode、/agents、/exit）
- [x] 文件路径补全（@ 触发）+ 斜杠命令补全
- [x] 上下文压缩（自动 / 反应式 / 手动 /compact）
- [x] 自动测试与静态检查集成
- [ ] 任务自动分解与调度
- [ ] 会话持久化
- [ ] Sandbox（bubblewrap 沙箱）
- [ ] 流式工具调度：收到完整工具调用后立即执行，无需等待 LLM 响应全部完成

## 代码量

纯 Python 约 3700 行代码，57 个源文件。

## 技术栈

### 核心依赖

| 技术 | 用途 |
| --- | --- |
| Python 3.11+ | 核心开发语言 |
| uv | 包管理与虚拟环境 |
| Typer | CLI 框架 |
| Pydantic | 数据校验与模型定义 |
| Textual | TUI 框架 |
| tiktoken | Token 计数（上下文压缩） |
| bubblewrap | Sandbox（规划中） |

### 工程质量

| 工具 | 用途 |
| --- | --- |
| Ruff | 代码格式化与静态检查 |
| mypy | 类型检查（strict 模式） |
| pytest, pytest-asyncio | 单元测试 |
| pre-commit | Git 提交钩子 |
| commitizen | 提交信息规范 |
| GitHub Actions | 持续集成（Python 3.11 + 3.12） |

## 使用

本系统仅支持 Linux/WSL。

安装依赖后即可使用：

- `mini-cc tui` — 启动 TUI 界面（默认）
- `mini-cc chat` — 启动命令行 REPL

## 许可证

[MIT](../LICENSE)
