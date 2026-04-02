# 贡献指南

感谢你对 Mini Claude Code 的关注！本文档将帮助你了解如何参与项目开发。

## 开发环境搭建

### 前置要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

### 安装步骤

```bash
# 克隆仓库
git clone https://github.com/liver0377/mini_cc.git
cd mini_cc

# 安装依赖（包括开发依赖）
uv sync

# 安装 Git hooks
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg
```

## 开发流程

### 分支规范

| 分支类型 | 命名格式 | 示例 |
| --- | --- | --- |
| 功能 | `feat/<描述>` | `feat/multi-agent` |
| 修复 | `fix/<描述>` | `fix/token-expiry` |
| 文档 | `docs/<描述>` | `docs/api-reference` |
| 重构 | `refactor/<描述>` | `refactor/cli-parser` |

### 工作流程

1. 基于 `main` 创建功能分支
2. 开发并提交代码
3. 确保通过所有本地检查
4. 推送分支并发起 Pull Request

```bash
git checkout -b feat/your-feature
# 开发...
git add <相关文件>
cz commit
git push origin feat/your-feature
```

## 提交规范

本项目遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <description>
```

### Type 列表

| Type | 用途 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `docs` | 文档变更 |
| `style` | 代码格式（不影响逻辑） |
| `refactor` | 重构（不是新功能也不是修复） |
| `test` | 添加或修改测试 |
| `chore` | 构建过程或辅助工具变动 |
| `ci` | CI 配置变更 |

### 示例

```bash
# 方式一：手动编写
git commit -m "feat(auth): add user login API"

# 方式二：使用 commitizen 交互式引导（推荐）
cz commit
```

不符合规范的提交会被 `commit-msg` hook 自动拒绝。

## 代码质量

提交前请确保通过以下检查：

```bash
uv run ruff check .      # Lint 检查
uv run ruff format .     # 代码格式化
uv run mypy .            # 类型检查
uv run pytest            # 运行测试
```

安装了 `pre-commit` hooks 后，以上部分检查会在 `git commit` 时自动执行。

## Pull Request 规范

- PR 标题遵循 Conventional Commits 格式（如 `feat: add multi-agent support`）
- 描述清楚改动的动机和内容
- 关联相关的 Issue（如 `Closes #12`）
- 确保通过所有 CI 检查
- 如有必要，补充必要的测试和文档

## 问题反馈

如果你发现了 Bug 或有功能建议，请通过 [GitHub Issues](https://github.com/liver0377/mini_cc/issues) 提交。
