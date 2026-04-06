# Mini Claude Code

[![CI](https://github.com/liver0377/mini_cc/actions/workflows/ci.yml/badge.svg)](https://github.com/liver0377/mini_cc/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://docs.astral.sh/ruff/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[**中文文档**](docs/README_zh.md)

> A lightweight multi-agent collaborative coding assistant CLI built in pure Python.

## Vision

Mini Claude Code aims to build a lightweight, extensible command-line coding agent in pure Python. It supports multi-agent collaboration and can understand natural language instructions to automatically complete tasks such as code writing, file operations, and test execution.

## Demo

![](./assets/mini-cc.gif)

## Features

- [x] Multi-agent collaboration & communication (AgentManager, SubAgent, event system)
- [x] File Tool, Shell Tool, Glob/Grep search tools
- [x] TUI interface (Textual: chat area, collapsible tool results, agent management panel, status bar)
- [x] Sub-Agent worktree isolation
- [x] File snapshot rollback (SnapshotService)
- [x] Plan/Build mode switching (Tab key)
- [x] Async agent loop + streaming output
- [x] OpenAI-compatible provider
- [x] Interrupt/cancel support (Esc key)
- [x] Short-term memory (context compression) + long-term memory (cross-session persistence)
- [x] Slash commands (/help, /compact, /clear, /mode, /agents, /exit)
- [x] File path completion (@ trigger) + slash command completion
- [x] Context compression (auto / reactive / manual /compact)
- [x] Automated testing & static analysis integration
- [ ] Automatic task decomposition & scheduling
- [ ] Session persistence
- [ ] Sandbox (bubblewrap)
- [ ] Streaming tool dispatch: execute each tool call as soon as it completes in the LLM stream, without waiting for the full response

## Codebase

Pure Python, ~4900 lines of code, 58 source files.

## Tech Stack

### Core Dependencies

| Technology | Purpose |
| --- | --- |
| Python 3.11+ | Core language |
| uv | Package manager & virtual environment |
| Typer | CLI framework |
| Pydantic | Data validation & model definitions |
| Textual | TUI framework |
| tiktoken | Token counting (context compression) |
| bubblewrap | Sandbox (planned) |

### Engineering Quality

| Tool | Purpose |
| --- | --- |
| Ruff | Formatting & linting |
| mypy | Type checking (strict mode) |
| pytest, pytest-asyncio | Unit testing |
| pre-commit | Git hooks |
| commitizen | Commit message convention |
| GitHub Actions | CI (Python 3.11 + 3.12) |

## Getting Started

> This project only supports Linux/WSL.

### Prerequisites

- [Python 3.11+](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/) — Python package manager
- [ripgrep](https://github.com/BurntSushi/ripgrep) — required by glob/grep tools
- [git](https://git-scm.com/)

### Installation

```bash
git clone https://github.com/liver0377/mini_cc.git
cd mini_cc
uv sync
```

### Configuration

Create a `.env` file in the project root:

```bash
# Required
OPENAI_API_KEY=sk-xxx

# Optional
OPENAI_BASE_URL=https://api.openai.com/v1   # Custom API base URL (e.g. DashScope, DeepSeek)
OPENAI_MODEL=gpt-4o                          # Model name
AUTO_COMPACT_THRESHOLD=80000                 # Token threshold for auto-compression
```

### Usage

```bash
# Launch TUI (default)
mini-cc tui

# Or launch REPL
mini-cc chat
```

## License

[MIT](LICENSE)
