### Desigin
此文档记录本项目的安全系统设计

#### Sandbox
sandbox用于限制code agent的能力范围，code agent应该:
- 只能够看到项目根目录下的文件
- 只能修改项目根目录下的文件
- 禁止sudo
- 禁止执行rm -f等危险命令
- 不能无限输出，吃满cpu等系统资源


#### Plan & Build
Plan和Build是code agent的两个全局模式
在Plan Mode下，code agent只能做只读操作, 根据用户的需求，输出执行计划
在Build模式之下，code agent可以执行写操作，修改整个代码仓库
