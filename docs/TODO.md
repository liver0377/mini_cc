# TODO: 子 Agent 文件冲突防止机制

## 问题

当前读 Agent（后台异步）和写 Agent（同步阻塞）可以同时运行，存在数据一致性风险：

```
t0: 主 Agent 启动只读 Agent A（后台，在 worktree 读）
t1: 主 Agent 启动写 Agent B（阻塞，在主工作区写）
    → A 读的是旧版本，B 正在修改
t2: B 完成，A 继续
t3: A 返回过时的结果
```

## 方案：文件级读写锁

在工具执行时按文件粒度加锁，读共享、写互斥。

### 1. 新增 `AsyncRWLock`（每文件一个锁）

```python
class AsyncRWLock:
    def __init__(self) -> None:
        self._readers = 0
        self._writer = False
        self._read_ready = asyncio.Event()
        self._write_ready = asyncio.Event()
        self._read_ready.set()
        self._write_ready.set()

    async def acquire_read(self) -> None:
        while self._writer:
            await self._write_ready.wait()
        self._readers += 1

    def release_read(self) -> None:
        self._readers -= 1
        if self._readers == 0:
            self._write_ready.set()

    async def acquire_write(self) -> None:
        while self._writer or self._readers > 0:
            await self._read_ready.wait()
        self._writer = True
        self._write_ready.clear()

    def release_write(self) -> None:
        self._writer = False
        self._read_ready.set()
        self._write_ready.set()
```

### 2. 新增 `FileLockService`（管理所有文件锁）

```python
class FileLockService:
    def __init__(self) -> None:
        self._locks: dict[str, AsyncRWLock] = {}

    def _get_lock(self, file_path: str) -> AsyncRWLock:
        path = str(Path(file_path).resolve())
        if path not in self._locks:
            self._locks[path] = AsyncRWLock()
        return self._locks[path]

    async def acquire_read(self, file_path: str) -> None:
        await self._get_lock(file_path).acquire_read()

    def release_read(self, file_path: str) -> None:
        self._get_lock(file_path).release_read()

    async def acquire_write(self, file_path: str) -> None:
        await self._get_lock(file_path).acquire_write()

    def release_write(self, file_path: str) -> None:
        self._get_lock(file_path).release_write()
```

### 3. 注入 `StreamingToolExecutor`

```python
class StreamingToolExecutor:
    def __init__(self, ..., file_lock: FileLockService):
        self._file_lock = file_lock

    async def _execute_tool(self, tc, tool, kwargs):
        file_path = kwargs.get("file_path")

        if tool.name in ("file_edit", "file_write"):
            await self._file_lock.acquire_write(file_path)
            try:
                result = await tool.async_execute(**kwargs)
            finally:
                self._file_lock.release_write(file_path)

        elif tool.name == "file_read":
            await self._file_lock.acquire_read(file_path)
            try:
                result = await tool.async_execute(**kwargs)
            finally:
                self._file_lock.release_read(file_path)
        else:
            result = await tool.async_execute(**kwargs)

        return result
```

### 4. 在 `AgentManager` 中创建并共享 `FileLockService`

```python
class AgentManager:
    def __init__(self, ...):
        self._file_lock = FileLockService()

    def create_agent(self, ...):
        file_lock = self._file_lock if not config.is_readonly else None
        executor = StreamingToolExecutor(..., file_lock=file_lock)
```

主 Agent 和所有子 Agent 共享同一个 `FileLockService` 实例。

### 效果

```
场景1：无冲突 → 并行
  读 Agent A 读 frontend/app.py  → 获取读锁 ✓
  写 Agent B 改 backend/auth.py  → 获取写锁 ✓（不同文件，无冲突）

场景2：同文件冲突 → 串行
  读 Agent A 读 backend/auth.py  → 获取读锁 ✓
  写 Agent B 改 backend/auth.py  → 等待 A 释放读锁 ...
  A 完成 → B 获取写锁 ✓

场景3：多读者 → 并行
  读 Agent A 读 auth.py → 读锁 ✓
  读 Agent B 读 auth.py → 读锁 ✓（多个读者兼容）
```

### 死锁风险与解决

```
Agent A: 读 auth.py → 读 session.py → ...
Agent B: 写 session.py → 写 auth.py → ...
→ A 持有 auth.py 读锁等 session.py，B 持有 session.py 写锁等 auth.py
```

**解决**：写锁加超时兜底，超时后返回 `ToolResult(error="文件被其他 Agent 锁定", success=False)`，LLM 自行决定重试或跳过。

```python
async def acquire_write(self, file_path: str, timeout: float = 30.0) -> bool:
    try:
        await asyncio.wait_for(
            self._get_lock(file_path).acquire_write(),
            timeout=timeout,
        )
        return True
    except asyncio.TimeoutError:
        return False
```

## 参考：Claude Code 的做法

Claude Code 不使用文件锁，而是：

1. **物理隔离**：subagent 可设置 `isolation: worktree`，在独立 worktree 中运行
2. **工具权限隔离**：只读 subagent 只能使用 Read/Grep/Glob，无 Edit/Write
3. **约定优于机制**：文档中告知用户"不要让两个 teammate 改同一个文件"

---

# TODO: 自动项目扫描 + 智能拆分

## 问题

当前主 Agent 在分析代码仓库时，任务拆分完全依赖 LLM 的"临场发挥"，存在以下痛点：

1. **拆分质量不稳定** — 主 Agent 对项目结构一无所知就要决定怎么拆，容易拆得不均匀（一个 agent 负责超大目录，另一个只有几个文件）
2. **没有数据驱动的拆分依据** — LLM 凭空猜测目录大小，可能导致某些子 Agent 工作量过大而超时
3. **缺少项目全貌** — 子 Agent 的 prompt 中缺乏项目整体结构信息，无法高效定位关键文件

## 方案：新增 `ProjectScan` 工具，数据驱动拆分决策

### 核心思路

在主 Agent 创建子 Agent 之前，先调用 `project_scan` 工具获取项目结构地图（文件数、行数、目录分组），然后基于数据做拆分决策，而非凭空猜测。

### 流程变更

```
当前流程：
  用户: "分析这个项目" → 主 Agent 凭空决定拆分 → 创建 3-5 个子 Agent

改进流程：
  用户: "分析这个项目"
    → 主 Agent 调用 project_scan 工具
    → 获取结构化的项目地图（目录/文件数/行数/建议分组）
    → 基于数据决定拆分策略（行数均衡、按职责分组）
    → 创建 3-5 个子 Agent，每个子 Agent 的 prompt 包含精确的目录范围和文件清单
```

### 涉及改动

#### 1. 新增 `ProjectScan` 工具

- 文件：`src/mini_cc/tools/project_scanner.py`
- 继承 `BaseTool`，工具名称 `project_scan`
- 输入参数：
  - `path`（默认 `"."`）— 扫描根目录
  - `depth`（默认 `3`）— 目录扫描深度
  - `group_threshold`（默认 `4`）— 文件数低于此值的目录合并到相邻组
- 扫描策略：
  - git repo 下优先使用 `git ls-files` 获取文件列表，自动遵守 .gitignore
  - 非 git repo 回退到 `pathlib.Path` 遍历，硬编码排除 `__pycache__`、`.git`、`node_modules` 等
- 输出内容：
  - 按 `src/` 下的顶级子目录分组，展示每组文件数和行数
  - 自动生成建议拆分方案（3~5 组，按行数尽量均匀分配）
  - 标注每组的主要文件名和职责标签（入口、核心逻辑、配置/测试等）

#### 2. 注册到主 Agent 工具集

- 文件：`src/mini_cc/tools/__init__.py`
- 在 `create_default_registry()` 中注册 `ProjectScan`
- **不**加入 `create_readonly_registry()` — 子 Agent 不需要扫描项目，只有主 Agent 需要

#### 3. 更新系统 prompt 规则

- 文件：`src/mini_cc/context/prompts/rules.md`
- 修改代码仓库分析相关规则，增加 `project_scan` 前置步骤要求
- 规则变为：分析代码库时必须先调用 `project_scan`，然后按扫描结果中的建议拆分方案创建子 Agent

#### 4. 更新工具使用指南

- 文件：`src/mini_cc/context/prompts/tool_guide.md`
- 在"代码分析拆分策略"部分加入 `project_scan` 的使用指引
- 更新示例，展示基于扫描数据拆分的过程（替换当前凭空拆分的示例）

#### 5. 新增测试

- 文件：`tests/tools/test_project_scanner.py`
- 测试 git repo 场景的扫描输出格式和准确性
- 测试非 git repo 的回退逻辑
- 测试分组逻辑（小目录合并、行数均衡）
- 测试空目录和边界场景

### 设计取舍

1. **为什么是工具而非自动注入？** — 作为工具，主 Agent 可以灵活决定是否扫描。简单问题（如"这个函数做什么"）不需要扫描。复杂分析时 LLM 按规则触发，兼顾灵活性。
2. **为什么用 `git ls-files`？** — 自动遵守 .gitignore，避免扫描 `node_modules`、`__pycache__` 等，且本项目已假设运行在 git repo 中。
3. **为什么不在子 Agent 里自动注入扫描结果？** — 保持关注点分离。主 Agent 拿到扫描结果后，用它来写更精确的子 Agent prompt，比自动注入更灵活且不增加子 Agent 的上下文负担。
4. **建议方案是强制的还是参考性的？** — 参考性的。主 Agent 可以根据用户的具体意图调整（如用户只关心 UI 层，可以将 tui/ 单独拆为一组而非合并）。

### 涉及文件汇总

| 操作 | 文件 |
|------|------|
| 新增 | `src/mini_cc/tools/project_scanner.py` |
| 修改 | `src/mini_cc/tools/__init__.py` |
| 修改 | `src/mini_cc/context/prompts/rules.md` |
| 修改 | `src/mini_cc/context/prompts/tool_guide.md` |
| 新增 | `tests/tools/test_project_scanner.py` |

---

# TODO: AST 调用链追踪工具 (`trace_calls`)

## 问题

当前 Agent 分析代码调用链的方式是"看到调用 → grep 找定义 → file_read 读代码"，存在以下痛点：

1. **容易断链** — Agent 可能在某一步停下来，不再继续追踪下游调用
2. **上下文浪费** — 每次跳转都要 grep + read，消耗大量 token
3. **缺乏全局视图** — Agent 只能看到局部调用，无法一次性理解从入口到终点的完整调用路径
4. **重复工作** — 多个子 Agent 可能各自追踪同一条调用链，浪费并行资源

## 方案：新增 `trace_calls` 工具，AST 静态分析追踪调用链

### 核心思路

提供一个 `trace_calls` 工具，Agent 只需指定入口函数，工具自动用 Python `ast` 模块追踪 N 层调用链，一次性返回完整的调用树，无需 Agent 逐层手动 grep + file_read。

### 使用场景

- Agent 分析某个函数的完整执行流程
- 子 Agent 理解自己负责模块的内部调用关系
- 重构前评估修改影响范围
- 排查 bug 时快速定位问题可能经过的代码路径

### 输入参数

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `entry` | 是 | — | 入口符号，格式 `ClassName.method_name` 或 `function_name` |
| `entry_file` | 是 | — | 入口符号所在文件路径（消歧义） |
| `depth` | 否 | 3 | 追踪深度，最大 5 |
| `max_nodes` | 否 | 50 | 返回最大节点数（防止调用树爆炸） |

### AST 分析策略

纯 `ast` 模块实现，不依赖外部库，分四步：

1. **定位入口函数** — 在 `entry_file` 中用 `ast` 解析找到对应的 `FunctionDef` / `AsyncFunctionDef` 节点
2. **提取调用** — 遍历函数体中所有 `ast.Call` 节点，提取被调函数名
3. **解析目标** — 根据当前文件的 import 语句，将调用名映射到定义文件和行号：
   - `self.xxx()` → 当前类的同名方法
   - `module.func()` → 根据 import 解析模块文件，再查找 `func` 定义
   - `func()` → 先查当前模块顶层定义，再查 import 导入的符号
4. **递归追踪** — 对每个解析到的目标，重复 1-3，直到达到 depth 上限或 max_nodes 上限
5. **去重** — 用 `visited` 集合（文件路径 + 符号名）防止循环追踪

### 输出格式

树状文本 + 扁平化摘要表：

```
## 调用链: QueryEngine.submit_message (depth=3)

engine.py:59  submit_message(prompt, state)
├── engine.py:87  _query_loop(state)
│   ├── engine.py:67  _drain_completions()
│   ├── engine.py:77  _drain_agent_events()
│   ├── compressor.py:42  compress_messages(...)
│   ├── models.py:128  collect_tool_calls(events)
│   └── executor.py:25  StreamingToolExecutor.run(tool_calls)
│       ├── executor.py:65  _execute_tool(tc, tool, kwargs)
│       └── (base.py:34)  BaseTool.async_execute  [动态分发，无法继续追踪]
└── engine.py:227  _should_wait_for_agents()

## 摘要
- 涉及文件: engine.py, compressor.py, models.py, executor.py
- 总调用节点: 12
- 无法解析的调用: 3 (动态分发/回调)
- 建议重点阅读: engine.py:87 (_query_loop 是核心循环)
```

### 静态分析的局限性

以下场景 AST 无法解析，输出中标注 `[动态分发，无法继续追踪]`：

- Protocol / ABC 的多态调用（运行时才能确定具体实现）
- 回调函数 / `Callable` 参数（函数作为参数传递）
- 字符串形式的动态 import（`importlib.import_module(name)`）
- `getattr(obj, method_name)()` 模式

这些场景 Agent 需要通过 `file_read` 手动继续追踪。工具在摘要中会统计无法解析的调用数量，帮助 Agent 评估结果的完整性。

### 注册范围

- 加入 `create_default_registry()` — 主 Agent 和写 Agent 可用
- 加入 `create_readonly_registry()` — 只读 Agent 也可用（分析调用链是只读操作）

### 涉及改动

#### 1. 新增 `TraceCalls` 工具

- 文件：`src/mini_cc/tools/trace_calls.py`
- 继承 `BaseTool`，工具名称 `trace_calls`
- 实现上述 AST 分析逻辑

#### 2. 注册到工具集

- 文件：`src/mini_cc/tools/__init__.py`
- 在 `create_default_registry()` 和 `create_readonly_registry()` 中均注册

#### 3. 更新工具使用指南

- 文件：`src/mini_cc/context/prompts/tool_guide.md`
- 在搜索类工具指引中加入 `trace_calls` 的使用说明和适用场景
- 文件：`src/mini_cc/context/prompts/tool_guide_sub.md`
- 同步更新子 Agent 版本的工具指南

#### 4. 新增测试

- 文件：`tests/tools/test_trace_calls.py`
- 测试用例覆盖：
  - 简单直线调用链（A → B → C）
  - 分支调用链（A → B, C, D）
  - self 方法调用解析
  - 跨模块 import 解析
  - depth 和 max_nodes 上限
  - 循环调用去重
  - 无法解析的动态调用标注
  - 入口函数不存在时的错误处理

### 设计取舍

1. **为什么用 AST 而不是运行时追踪？** — AST 是静态分析，不需要实际执行代码，对项目零侵入。运行时追踪需要覆盖测试用例，很多分支可能跑不到。
2. **为什么限制 depth 最大 5？** — 防止调用树指数爆炸。深度 5 通常足够覆盖从入口到叶子节点的关键路径。Agent 可以对感兴趣的子节点再次调用 `trace_calls` 进行深入追踪。
3. **为什么不用 LSP（Language Server Protocol）？** — LSP 需要启动语言服务器进程，增加系统复杂度和启动时间。AST 解析足够覆盖 80% 的场景，且对项目零依赖。
4. **为什么不预构建全项目调用图？** — 全项目调用图构建成本高、更新困难。按需追踪更轻量，Agent 只分析自己关心的入口。

### 涉及文件汇总

| 操作 | 文件 |
|------|------|
| 新增 | `src/mini_cc/tools/trace_calls.py` |
| 修改 | `src/mini_cc/tools/__init__.py` |
| 修改 | `src/mini_cc/context/prompts/tool_guide.md` |
| 修改 | `src/mini_cc/context/prompts/tool_guide_sub.md` |
| 新增 | `tests/tools/test_trace_calls.py` |

---

# TODO: Verification Agent — 编辑后自动验证

## 问题

当前 `file_edit` / `file_write` 执行后没有任何验证机制。编辑后的文件可能存在语法错误、类型错误、逻辑错误，但 Agent 一无所知，直到后续步骤出错才发现。

现有验证链的薄弱环节：

| 阶段 | 当前行为 | 缺失 |
|------|---------|------|
| 编辑前 | `SnapshotService` 备份原始文件 | 无 |
| 编辑中 | 精确字符串替换，检查匹配唯一性 | 无语义检查 |
| 编辑后 | 返回"文件编辑成功" | **完全没有验证** — 不检查语法、不跑 lint、不跑测试 |

## 方案：两层验证 + 专用 Verification Agent

### 整体架构

```
写 Agent 执行 file_edit("src/foo.py", ...)
  │
  ▼ 第一层：即时语法检查（零成本，内置在 file_edit/file_write 中）
  │   ast.parse() 验证被修改文件
  │   ├── 失败 → 回滚文件 + 返回 ToolResult(success=False)
  │   └── 通过 ↓
  ▼ 第二层：Verification Agent（LLM 驱动，根据编辑规模分级触发）
  │   收到 diff + 编辑意图 + 项目验证命令
  │   → 多维度验证 → 返回结构化验证报告
  │   ├── 报告附加到 ToolResult.output
  │   └── 写 Agent 看到报告后自行决定是否修复
  ▼ 写 Agent 继续下一步
```

### 第一层：即时语法检查（每次编辑必跑）

- 在 `file_edit.py` 和 `file_write.py` 的 `execute()` 方法中，写入文件后对 `.py` 文件执行 `ast.parse()`
- 语法检查失败时，用 `SnapshotService.restore()` 回滚文件，返回 `ToolResult(success=False, error="语法错误: ...")`
- 通过则继续进入第二层

这一层成本极低（纯 CPU，无 LLM 调用），拦截最明显的语法错误。

### 第二层：Verification Agent（LLM 驱动，分级触发）

#### 创建与调度方式

不是通过 `AgentTool` 由 LLM 手动创建，而是系统自动创建。`StreamingToolExecutor` 在 `file_edit`/`file_write` 执行成功且语法检查通过后，自动触发验证流程。

Verification Agent 是一种特殊的**同步阻塞 readonly SubAgent** — 写 Agent 必须等待验证结果才能继续下一步操作。

#### 分级验证策略

根据编辑规模自动选择验证级别，控制成本：

| 编辑规模 | 验证级别 | 行为 |
|----------|---------|------|
| 改动 ≤ 5 行 | 快速验证 | 只跑 ast + ruff + mypy（不启动 LLM Agent，纯脚本） |
| 改动 6-30 行 | 标准验证 | 启动 Verification Agent，语法 + lint + 类型 + 语义审查 |
| 改动 > 30 行 | 深度验证 | 启动 Verification Agent，全量验证含影响分析 |

#### Verification Agent 收到的信息

自动构造验证 prompt，包含以下内容：

1. **修改的文件路径**
2. **变更 diff 摘要**（old_string vs new_string 的对比，标注行号范围）
3. **编辑意图**（从写 Agent 当前 turn 的 TextDelta 和 ToolCallStart 事件中提取 — Agent 在调用 file_edit 之前通常会描述修改意图）
4. **项目验证命令**（从 AGENTS.md 的 "Build, Lint, and Test Commands" 段解析）

#### Verification Agent 的工具权限

使用 `create_readonly_registry()`（file_read, glob, grep, bash），不能修改任何文件。

#### Verification Agent 的验证步骤

prompt 引导按优先级自主执行：

1. **语法检查** — `bash: python -c "import ast; ast.parse(open('...').read())"` 或 `bash: ruff check <file>`
2. **Lint** — `bash: uv run ruff check <file>`（只检查被修改的单个文件）
3. **类型检查** — `bash: uv run mypy <file>`（如适用）
4. **语义审查** — `file_read` 读取修改后的文件上下文，判断改动是否实现了声称的意图、是否有遗漏
5. **影响分析**（仅深度验证）— `grep` 搜索调用方，判断是否需要同步修改

#### Verification Agent 的输出格式

结构化的验证报告，直接附加到 `ToolResult.output`：

```
## 验证报告: src/mini_cc/tools/file_edit.py

**结论**: ⚠️ 发现问题

### 检查项
| 检查 | 结果 |
|------|------|
| 语法检查 | ✅ 通过 |
| ruff lint | ✅ 通过 |
| mypy 类型 | ❌ 失败: "error: Incompatible return type" |
| 语义审查 | ⚠️ 警告: execute() 新增的 ast.parse 没有处理 SyntaxError 以外的异常 |

### 发现的问题
1. file_edit.py:42 — 缺少 OSError 的 except 分支，可能导致验证步骤本身抛异常
2. file_edit.py:48 — 返回类型与 ToolResult 的 success 字段不一致

### 建议
- 在 ast.parse 外层增加通用异常处理
- 补充对应的单元测试
```

#### 验证结果处理

Verification Agent **只报告问题，不自动修复**。验证报告附加到 `ToolResult.output` 中，写 Agent 在下一步立刻看到完整报告，自行决定是否修复。这样做的理由：

- Verification Agent 的判断也可能出错（误报），自动修复会引入风险
- 职责清晰：验证者只验证，修改者只修改
- 写 Agent 拥有完整的上下文（知道用户原始意图），能做出更好的修复决策

### 涉及改动

#### 1. 新增 Verification Agent 定义

- 文件：`src/mini_cc/agent/verify_agent.py`
- 定义 `VerifyAgent` 类，封装验证逻辑：
  - diff 摘要生成（对比编辑前后的文件内容）
  - 编辑意图提取（从写 Agent 当前 turn 的事件流中提取文本描述）
  - 验证 prompt 构造
  - 验证报告解析与格式化

#### 2. 扩展 AgentManager

- 文件：`src/mini_cc/agent/manager.py`
- 新增 `create_verify_agent()` 方法：
  - 使用 `create_readonly_registry()` 工具集
  - 使用专用的系统 prompt（`build_for_verify_agent()`）
  - 不创建 worktree（验证在主工作区进行，只读）
  - 同步阻塞执行

#### 3. 扩展 StreamingToolExecutor

- 文件：`src/mini_cc/tool_executor/executor.py`
- `_execute_tool()` 方法中，对 `file_edit`/`file_write` 的成功结果触发验证流程：
  - 调用 `ast.parse` 即时语法检查
  - 根据改动行数决定验证级别
  - 小改动：纯脚本验证，不启动 LLM
  - 中大改动：创建 Verification Agent 同步执行，将报告附加到 `ToolResult.output`

#### 4. 增强文件编辑工具

- 文件：`src/mini_cc/tools/file_edit.py`
  - execute() 中增加 ast.parse 语法检查
  - 语法错误时调用 SnapshotService 回滚并返回失败
- 文件：`src/mini_cc/tools/file_write.py`
  - 同上

#### 5. 扩展 SnapshotService

- 文件：`src/mini_cc/agent/snapshot.py`
- 新增 `restore_single(file_path)` 方法，支持回滚单个文件（当前只有 `restore_all()`）

#### 6. 新增验证专用系统 prompt

- 文件：`src/mini_cc/context/prompts/verify_guide.md`
- Verification Agent 专用系统 prompt，包含：
  - 身份声明（你是代码验证专家）
  - 验证策略和优先级
  - 输出格式规范

#### 7. 扩展系统 prompt 构建器

- 文件：`src/mini_cc/context/system_prompt.py`
- 新增 `build_for_verify_agent()` 方法，使用 `verify_guide.md` 替代 `intro_sub.md` / `rules_sub.md`

#### 8. 解析 AGENTS.md 验证命令

- 文件：`src/mini_cc/context/system_prompt.py` 或新增 `src/mini_cc/context/agents_md.py`
- 从 AGENTS.md 中解析 "Build, Lint, and Test Commands" 段，提取可用的 lint/typecheck/test 命令
- 注入到 Verification Agent 的验证 prompt 中

#### 9. 新增测试

- 文件：`tests/agent/test_verify_agent.py`
- 测试用例覆盖：
  - diff 摘要生成准确性
  - 编辑意图提取
  - 分级验证级别判定（≤5 行 / 6-30 行 / >30 行）
  - 语法错误自动回滚
  - Verification Agent 验证报告格式
  - 小改动纯脚本验证（无 LLM 调用）
  - AGENTS.md 验证命令解析

### 设计取舍

1. **为什么自动触发而不是 Agent 主动调用？** — 代码质量不应该依赖 LLM "记得"去验证。自动触发保证每次编辑都被检查，是质量保障而非可选功能。
2. **为什么分级验证？** — 成本优化。小改动（修个变量名、加一行 import）用脚本验证足够，不值得启动 LLM Agent。只有涉及逻辑变更的中大型编辑才值得 LLM 推理验证。
3. **为什么验证失败不自动回滚？** — Verification Agent 的判断也可能误报。让写 Agent 看到报告后自行决定，比自动回滚更安全。唯一的例外是 ast.parse 语法错误，这是确定性错误，直接回滚。
4. **为什么用只读工具集而非 trace_calls？** — 避免验证步骤本身变得太重。影响分析用 grep 搜索调用方足够了，trace_calls 适合深度分析场景，放在每次编辑后验证成本太高。
5. **Verification Agent 的编辑意图从哪来？** — 从写 Agent 当前 turn 的事件流中提取。Agent 通常在调用 file_edit 之前会输出文本描述修改意图（如"我现在要修改 execute 方法增加语法检查"），这些 TextDelta 事件被收集后注入验证 prompt。

### 涉及文件汇总

| 操作 | 文件 |
|------|------|
| 新增 | `src/mini_cc/agent/verify_agent.py` |
| 修改 | `src/mini_cc/agent/manager.py` |
| 修改 | `src/mini_cc/tool_executor/executor.py` |
| 修改 | `src/mini_cc/tools/file_edit.py` |
| 修改 | `src/mini_cc/tools/file_write.py` |
| 修改 | `src/mini_cc/agent/snapshot.py` |
| 新增 | `src/mini_cc/context/prompts/verify_guide.md` |
| 修改 | `src/mini_cc/context/system_prompt.py` |
| 新增 | `tests/agent/test_verify_agent.py` |
