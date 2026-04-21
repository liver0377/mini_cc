# SWE-bench Verified 评测设计文档

## 概述

本目录包含 mini_cc 在 **SWE-bench Verified** 基准上的评测脚本与工具。评测目标为衡量 mini_cc 作为自动化代码修复 agent 的实际能力，重点关注三个核心指标：

1. **解决率（Resolve Rate）**：生成的 patch 能否通过原始 issue 的全部回归测试
2. **平均修复轮次（Avg Repair Turns）**：agent 解决一个问题所需的对话轮数
3. **工具调用效率（Tool Call Efficiency）**：解决问题的工具调用次数与成功率

## 评测流程

整个评测分为三个阶段，解耦执行：

```
阶段一                阶段二                  阶段三
eval_swebench.py  →  swebench_run_eval.sh  →  merge_report.py
(生成 patch)          (harness 判定)          (合并报告)
```

### 阶段一：生成 patch（`eval_swebench.py`）

**输入：** SWE-bench Verified 数据集（HuggingFace）

**过程：**
1. 从 `princeton-nlp/SWE-bench_Verified` 随机采样 N 个实例
2. 为每个实例创建独立的 git workdir（基于 `base_commit`）
3. 将 issue 描述作为 prompt 注入 mini_cc agent
4. agent 在 workdir 中自主修复问题（上限 30 轮）
5. 收集 `git diff HEAD` 作为 patch

**输出：**
- `predictions.jsonl` — SWE-bench 标准格式（instance_id + model_patch）
- `trajectory.json` — 每个 instance 的运行轨迹（轮次、耗时、工具调用详情）

**环境变量：**

| 变量 | 必需 | 说明 |
|------|------|------|
| `EVAL_MODEL` | 是 | 模型名称 |
| `EVAL_API_KEY` | 是 | API 密钥 |
| `EVAL_BASE_URL` | 否 | API 地址（默认 OpenAI） |

**命令行参数：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--n` | 10 | 采样实例数 |
| `--seed` | 42 | 随机种子 |
| `--timeout` | 300 | 单实例超时（秒） |
| `--max-turns` | 30 | 最大对话轮数 |
| `--output-dir` | results/swebench | 输出目录 |

### 阶段二：Harness 判定（`swebench_run_eval.sh`）

**前置：** 需要本机安装 Docker 和 `swebench` Python 包。

**过程：**
1. 读取阶段一输出的 `predictions.jsonl`
2. 调用 SWE-bench 官方 harness（`swebench.harness.run_evaluation`）
3. 为每个实例在 Docker 容器中运行原始测试用例
4. 判断 patch 是否通过所有 `FAIL_TO_PASS` 和 `PASS_TO_PASS` 测试

**输出：**
- `evaluation_results/{run_id}/` — 包含每个实例的 `report.json`
- 聚合报告（含 `resolved_ids`、`unresolved_ids` 等）

### 阶段三：合并报告（`merge_report.py`）

**输入：**
- `trajectory.json`（阶段一输出）
- harness 聚合结果（阶段二输出）

**过程：**
1. 读取 trajectory 中的轮次、耗时、工具调用详情
2. 读取 harness 结果中的 resolved 状态
3. 按 instance_id 关联，计算核心指标

**输出：**
- 控制台：Rich 表格格式的完整报告
- `final_report.json`：结构化 JSON 报告（含汇总 + 逐实例详情）

## 核心指标定义

### 1. 解决率（Resolve Rate）

```
resolve_rate = resolved_instances / total_instances × 100%
```

"resolved" 定义：SWE-bench harness 判定 patch 成功通过该实例的**全部**测试用例（包括 `FAIL_TO_PASS` 和 `PASS_TO_PASS`）。

这是最核心的指标，直接反映 agent 的实际代码修复能力。

### 2. 平均修复轮次（Avg Repair Turns）

```
avg_turns_all      = Σ(turns) / total_instances
avg_turns_resolved = Σ(turns | resolved) / resolved_instances
avg_turns_unresolved = Σ(turns | !resolved) / unresolved_instances
```

分别统计三组：全部、已解决、未解决。通过对比 resolved 与 unresolved 的平均轮次可以判断：
- 若 resolved 平均轮次明显更低 → agent 倾向于在能解决的问题上高效收敛
- 若两者接近 → 轮次不敏感，agent 无法有效区分可解决与不可解决的问题

### 3. 工具调用效率（Tool Call Efficiency）

分为两个维度：

**a) 调用频次**

```
avg_calls_all      = Σ(num_tool_calls) / total_instances
avg_calls_resolved = Σ(num_tool_calls | resolved) / resolved_instances
```

反映 agent 解决单个问题的平均资源消耗。

**b) 工具成功率**

按工具类型统计调用次数和成功次数：

```
tool_success_rate = success_calls / total_calls × 100%
```

反映 agent 使用各类工具的熟练程度。低成功率可能表明：
- prompt 中工具使用说明不够清晰
- 工具参数生成不够准确
- 某些场景下工具设计有缺陷

## 数据采集

### trajectory.json 结构

每个实例采集以下字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `instance_id` | str | SWE-bench 实例 ID |
| `status` | str | 运行状态：`ok` / `empty_patch` / `timeout` / `error` |
| `turns` | int | agent 对话轮数 |
| `elapsed_sec` | float | 总耗时（秒） |
| `patch_length` | int | 生成 patch 的字符长度 |
| `num_tool_calls` | int | 工具调用总次数 |
| `tool_events` | list | 每次工具调用的详情 |
| `resolved` | bool | harness 判定结果（仅 merge 后有） |

`tool_events` 中每个条目：

| 字段 | 类型 | 说明 |
|------|------|------|
| `tool` | str | 工具名称（如 `file_read`、`bash`） |
| `success` | bool | 调用是否成功 |
| `output_len` | int | 输出长度（字符数） |

### final_report.json 结构

```json
{
  "total_instances": 50,
  "resolved_instances": 15,
  "resolve_rate": 30.0,
  "avg_turns_all": 12.3,
  "avg_turns_resolved": 8.5,
  "avg_tool_calls_all": 25.6,
  "avg_tool_calls_resolved": 18.2,
  "avg_time_all": 120.5,
  "instances": [...]
}
```

## 使用方法

### 完整评测（三步）

```bash
# 阶段一：生成 patch
EVAL_MODEL=gpt-4o EVAL_API_KEY=sk-xxx \
  uv run python scripts/eval/eval_swebench.py --n 50 --output-dir results/swebench

# 阶段二：harness 判定（需要 Docker）
bash scripts/eval/swebench_run_eval.sh results/swebench/predictions.jsonl

# 阶段三：合并报告
uv run python scripts/eval/merge_report.py \
  --trajectory results/swebench/trajectory.json \
  --harness evaluation_results/mini_cc_eval/results.json \
  --output results/swebench/final_report.json
```

### 仅生成 patch（不跑 harness）

跳过阶段二和三，直接查看阶段一的控制台输出，可以获得：
- patch 生成率（`status == "ok"` 的比例）
- 平均轮次
- 工具调用统计

> 注意：此时没有"解决率"数据，`status == "ok"` 仅表示生成了非空 patch，不代表通过测试。

## 设计决策

### 为什么分两步而不是一步到位

1. **可复现性**：patch 生成和 harness 评测是独立过程，可以单独重跑
2. **成本控制**：LLM 调用是主要成本，与 harness 评测解耦后可以多次分析同一批 patch
3. **灵活性**：harness 需要 Docker 环境，分开执行降低环境要求
4. **调试友好**：出现问题可以精确定位是 agent 阶段还是评测阶段

### 为什么 tool_events 写入 trajectory

原始脚本在内存中采集了 tool_events 但未持久化。增强后保留完整工具调用详情，支持：
- 后续分析各工具的使用模式
- 识别高频失败工具
- 优化工具设计的数据基础

### instance 串行执行

当前设计为逐个实例串行执行。原因：
- SWE-bench 评测中每个实例需要独立的 git workdir 和 agent 状态
- LLM API 调用本身是瓶颈，并行化收益有限
- 串行执行便于实时观察进度和中断恢复
