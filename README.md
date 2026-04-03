# Mini Claude Code

[![CI](https://github.com/liver0377/mini_cc/actions/workflows/ci.yml/badge.svg)](https://github.com/liver0377/mini_cc/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://docs.astral.sh/ruff/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 基于 Python 实现的轻量级多 Agent 协作代码助手 CLI

## 项目愿景

Mini Claude Code 旨在用纯 Python 构建一个轻量、易扩展的命令行代码智能体，支持多 Agent 协作，能够理解自然语言指令并自动完成代码编写、文件操作、测试执行等任务。

## 规划中特性

- [ ] 多 Agent 协作与通信
- [ ] 任务自动分解与调度
- [x] File Tool、Shell Tool、Git Tool操作
- [ ] 短期/长期记忆机制
- [ ] 会话持久化
- [️x] 自动测试与静态检查集成
- [ ] slash命令

## 技术栈

### 核心依赖

| 技术 | 用途 |
| --- | --- |
| Python 3.11+ | 核心开发语言 |
| uv | 包管理与虚拟环境 |
| Typer | CLI 框架 |
| Pydantic | 数据校验与模型定义 |
| bubblewrap | Sandbox |

### 工程质量

| 工具 | 用途 |
| --- | --- |
| Ruff | 代码格式化与静态检查 |
| mypy | 类型检查 |
| pytest, pytest-async | 单元测试 |
| pre-commit | Git 提交钩子 |
| commitzen | 提交信息规范 |
| GitHub Actions | 持续集成 |

## 使用
本系统被设计为仅支持Linux/WSL系统上可用

## 许可证

[MIT](LICENSE)
