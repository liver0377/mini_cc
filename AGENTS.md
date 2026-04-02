# AGENTS.md

Guidance for agentic coding agents working in this repository.

## Project Overview

Mini Claude Code (`mini_cc`) is a lightweight multi-agent collaborative coding assistant CLI built in pure Python. It aims to understand natural language instructions and automatically perform code writing, file operations, and test execution. The project is in early development — core source files under `src/mini_cc/` are mostly empty scaffolds.

**Target platform:** Linux/WSL only.

## Repository Structure

```
mini_cc/
├── src/mini_cc/          # Main source package
│   ├── __init__.py       # Package init (currently empty)
│   └── cli.py            # CLI entry point (Typer app, currently empty)
├── docs/                 # Design documents (in Chinese)
│   ├── Agent-Loop/       # Agent loop mechanism & streaming design
│   ├── context/          # Context management (system prompt injection)
│   ├── memory/           # Memory system (pure Markdown, no database)
│   ├── security/         # Security (sandbox, Plan/Build modes)
│   └── tools/            # Tool system (File, Bash, Glob, Grep tools)
├── tests/                # Unit tests (pytest, directory not yet created)
├── pyproject.toml        # Project config, dependencies, tool settings
├── .pre-commit-config.yaml
└── .github/workflows/ci.yml
```

## Build, Lint, and Test Commands

All commands use `uv run` to execute within the managed virtual environment.

```bash
# Install dependencies (includes dev dependencies)
uv sync

# Lint
uv run ruff check .

# Lint with auto-fix
uv run ruff check . --fix

# Format check
uv run ruff format --check .

# Format (write changes)
uv run ruff format .

# Type check
uv run mypy .

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_foo.py

# Run a single test function
uv run pytest tests/test_foo.py::test_bar

# Run a single test with verbose output
uv run pytest tests/test_foo.py::test_bar -v

# Run tests matching a keyword
uv run pytest -k "pattern"

# Install git hooks (do once after clone)
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg
```

## Code Style Guidelines

### Formatting (Ruff)

- **Target Python version:** 3.11+
- **Line length:** 120 characters
- Use `ruff format` for formatting — it replaces Black in this project.

### Lint Rules (Ruff)

The following rule categories are enabled in `pyproject.toml`:

| Code | Category |
|------|----------|
| E    | pycodestyle errors |
| F    | pyflakes |
| I    | isort (import sorting) |
| N    | pep8-naming |
| W    | pycodestyle warnings |
| UP   | pyupgrade (use modern Python syntax) |

### Imports

- Follow isort conventions (enforced by Ruff rule `I`).
- stdlib → third-party → local imports, separated by blank lines.
- Use absolute imports from the package root: `from mini_cc.module import Class`.

### Type Annotations (mypy)

- **mypy strict mode is enabled** (`strict = true` in `pyproject.toml`).
- All functions must have complete type annotations for parameters and return types.
- Use `py.typed` marker if building a distributable package.
- Avoid `Any`; use concrete types or generics.
- Prefer `str | None` over `Optional[str]` (UP rule enforces modern union syntax).

### Naming Conventions (PEP 8, enforced by N rules)

- **Modules/packages:** `snake_case`
- **Classes:** `PascalCase`
- **Functions/methods:** `snake_case`
- **Constants:** `UPPER_SNAKE_CASE`
- **Private members:** prefix with underscore `_`

### Error Handling

- Use specific exception types, never bare `except:`.
- Prefer custom exception classes defined in a dedicated module.
- Always include meaningful error messages.
- Use `raise ... from err` to preserve exception chains.

### General Python Style

- Use f-strings for string formatting (enforced by UP rule).
- Use `pathlib.Path` over `os.path` for file path manipulation.
- Prefer dataclasses or Pydantic models for structured data.
- No unnecessary trailing whitespace (enforced by pre-commit).
- Files must end with a newline (enforced by pre-commit).

## Commit Convention

This project follows [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`

**Example:** `feat(tools): add file read tool implementation`

Use `cz commit` for interactive commit message generation. Non-compliant messages are rejected by the commit-msg hook.

## Branch Naming

| Type       | Format             | Example                |
|------------|--------------------|------------------------|
| Feature    | `feat/<desc>`      | `feat/multi-agent`     |
| Bug fix    | `fix/<desc>`       | `fix/token-expiry`     |
| Docs       | `docs/<desc>`      | `docs/api-reference`   |
| Refactor   | `refactor/<desc>`  | `refactor/cli-parser`  |

## CI Pipeline

GitHub Actions runs on every push/PR to `main` with Python 3.11 and 3.12:

1. `uv sync` — install dependencies
2. `uv run ruff check .` — lint
3. `uv run mypy .` — type check
4. `uv run pytest` — run tests

**All checks must pass before merging.**

## Key Design Decisions

- **Memory system:** Pure Markdown files (no database/vector store). Four types: `user`, `feedback`, `project`, `reference`. See `docs/memory/README.md`.
- **Tool system:** Unified tool class with a registry supporting API format conversion. Tools: FileRead, FileEdit, FileWrite, Bash, Glob, Grep. See `docs/tools/README.md`.
- **Security:** Sandbox via `bubblewrap`. Two global modes — Plan (read-only) and Build (read-write). See `docs/security/README.md`.
- **Context:** System prompt built from string arrays for dynamic injection and prompt caching. See `docs/context/README.md`.
- **Agent Loop:** Streaming with event state machine (message_start, content_block_start/delta/stop, message_delta/stop). See `docs/Agent-Loop/`.
