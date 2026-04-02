# Design
此文档记录整个项目记忆系统的设计

整个项目的记忆系统完全基于文件，不包含任何数据库，向量存储，只有Markdown, 所有memory文件均存储在~/.mini_cc/memory 目录下

## 短期记忆

### session-memory.md
会话级别的临时记忆，记录当前会话进行到了哪里，做了什么，遇到了什么问题

#### 文件内容
```markdown
# Session Title
A short and distinctive 5-10 word descriptive title_

# Current State
What is actively being worked on right now?_

# Task specification
What did the user ask to build?_

# Files and Functions
What are the important files?_

# Workflow
What bash commands are usually run and in what order?_

# Errors & Corrections
Errors encountered and how they were fixed._

# Codebase and System Documentation
What are important system components?_

# Learnings
What has worked well? What has not?_

# Key results
Exact output the user requested (table, answer, etc.)_

# Worklog
Step by step, what was attempted, done?_
```

**参数控制**
- MAX_SECTION_LENGTH: 控制文档中每个section的token限额
- MAX_TOTAL_SESSION_MEMORY_TOKENS: 文件总token上限

#### 触发机制
定义两个阈值:
- minimumTokensBetweenUpdate: 若当前上下文token数 - 上一次更新时的上下文token数 < 该阈值, 不触发短期记忆提取
- minimumToolCallsBetweenUpdates: 若自上次更新时，工具的调用次数 < 该阈值，不触发短期记忆提取

此外，如有 当前上下文token数 - 上一次更新时的上下文token数 > minimumTokensBetweenUpdate 且最后一轮没有工具调用, 触发短期记忆提取


## 中期记忆
        