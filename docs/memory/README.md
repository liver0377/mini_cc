# Design
此文档记录整个项目记忆系统的设计

整个项目的记忆系统完全基于文件，不包含任何数据库，向量存储，只有Markdown

- Memory.md是记忆的入口索引，每次对话都将其完整地加载到上下文中
- 包含四种记忆类型:
  - user
    存储用户偏好
  - feedback
    用户对agent行为的纠正与确认
  - project
    非代码可推导的项目上下文
  - reference
    外部系统指针

每种记忆类型都有`<when_to_save>`, `<how_to_use>`, `<body_structure>`类型约束, 遵循一下FrontMatter格式
```txt
---
name: {{memory name}}
description: {{one-line description — 用于未来判断相关性}}
type: {{user, feedback, project, reference}}
---

{{memory content — feedback/project 类型建议包含 **Why:** 和 **How to apply:** 行}}
```
