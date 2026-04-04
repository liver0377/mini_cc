# Design

此文档记录中期记忆（跨会话持久化）的设计。详细设计见 [design.md](design.md)。

整个记忆系统完全基于文件，不包含任何数据库、向量存储，只有 Markdown。所有记忆文件存储在 `~/.mini_cc/projects/{project_id}/memory/` 目录下。

> 上下文压缩（会话级短期摘要）是独立子系统，参见 `docs/compression/`。

## 中期记忆
