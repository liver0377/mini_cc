# Design
此文档负责设计整个系统的上下文管理

## System Prompt
包含以下几个部分:
- System Rules
- Doing Tasks
- Using Tools
- Memory

采用string数组来实现动态注入以及提示词缓存
