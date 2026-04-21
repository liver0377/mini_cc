from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env")

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from mini_cc.context.system_prompt import SystemPromptBuilder, collect_env_info  # noqa: E402
from mini_cc.context.tool_use import ToolUseContext  # noqa: E402
from mini_cc.features.memory.extractor import MemoryExtractor  # noqa: E402
from mini_cc.models import (  # noqa: E402
    AgentCompletionEvent,
    Event,
    Message,
    QueryState,
    Role,
    TextDelta,
    ToolResultEvent,
)
from mini_cc.providers.openai import OpenAIProvider  # noqa: E402
from mini_cc.runtime.agents import AgentManager  # noqa: E402
from mini_cc.runtime.execution import StreamingToolExecutor  # noqa: E402
from mini_cc.runtime.query import QueryEngine  # noqa: E402
from mini_cc.task.service import TaskService  # noqa: E402
from mini_cc.tools import create_default_registry  # noqa: E402
from mini_cc.tools.agent_tool import AgentTool  # noqa: E402

console = Console()


def _load_instances(n: int, seed: int) -> list[dict]:
    from datasets import load_dataset

    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    instances = list(ds)
    random.seed(seed)
    return random.sample(instances, min(n, len(instances)))


def _ensure_bare_clone(repo: str, cache_dir: Path) -> Path:
    owner, name = repo.split("/")
    clone_dir = cache_dir / f"{owner}__{name}.git"
    if clone_dir.is_dir():
        console.print(f"  [dim]Cache hit: {repo}[/]")
        return clone_dir
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo}.git"
    console.print(f"  [cyan]Cloning {repo} (bare)...[/]")
    subprocess.run(
        ["git", "clone", "--bare", url, str(clone_dir)],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return clone_dir


def _checkout_workdir(bare_path: Path, base_commit: str, work_base: Path, instance_id: str) -> Path:
    workdir = work_base / instance_id
    if workdir.is_dir():
        shutil.rmtree(workdir)
    subprocess.run(
        ["git", "clone", str(bare_path), str(workdir)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    subprocess.run(
        ["git", "checkout", base_commit],
        cwd=str(workdir),
        check=True,
        capture_output=True,
        timeout=30,
    )
    return workdir


def _build_engine(workdir: Path, config_api_key: str, config_base_url: str, config_model: str):
    provider = OpenAIProvider(model=config_model, base_url=config_base_url, api_key=config_api_key)

    completion_queue: asyncio.Queue[AgentCompletionEvent] = asyncio.Queue()
    agent_event_queue: asyncio.Queue[Event] = asyncio.Queue()

    registry = create_default_registry()
    executor = StreamingToolExecutor(registry)
    interrupt_flag = threading.Event()

    turn_limit_holder: dict = {"max": 30, "engine_ref": None}

    def _is_interrupted() -> bool:
        if interrupt_flag.is_set():
            return True
        eng = turn_limit_holder["engine_ref"]
        if eng is not None and eng.state is not None and eng.state.turn_count >= turn_limit_holder["max"]:
            return True
        return False

    tool_use_ctx = ToolUseContext(
        get_schemas=registry.to_api_format,
        execute=executor.run,
        is_interrupted=_is_interrupted,
    )

    memory_extractor = MemoryExtractor(stream_fn=provider.stream, cwd=str(workdir))

    async def _post_turn_hook(state: QueryState) -> None:
        if memory_extractor.should_extract(state):
            memory_extractor.fire_and_forget(state)

    engine = QueryEngine(
        stream_fn=provider.stream,
        tool_use_ctx=tool_use_ctx,
        completion_queue=completion_queue,
        agent_event_queue=agent_event_queue,
        post_turn_hook=_post_turn_hook,
        model=config_model,
    )
    turn_limit_holder["engine_ref"] = engine

    session_id = secrets.token_hex(4)
    task_service = TaskService(task_list_id=session_id)
    env_info = collect_env_info(config_model, cwd=workdir)
    prompt_builder = SystemPromptBuilder()

    agent_manager = AgentManager(
        project_root=workdir,
        stream_fn=provider.stream,
        task_service=task_service,
        completion_queue=completion_queue,
        agent_event_queue=agent_event_queue,
        prompt_builder=prompt_builder,
        env_info=env_info,
    )

    agent_tool = AgentTool(
        manager=agent_manager,
        get_parent_state=lambda: engine.state if engine.state else QueryState(),
        event_queue=agent_event_queue,
        get_mode=lambda: "build",
    )
    registry.register(agent_tool)

    system_prompt = prompt_builder.build(env_info, mode="build")
    state = QueryState(messages=[])
    state.messages.append(Message(role=Role.SYSTEM, content=system_prompt))

    return engine, state, turn_limit_holder


def _build_problem_prompt(instance: dict) -> str:
    parts = [
        "## Issue\n",
        instance["problem_statement"],
    ]
    hints = instance.get("hints_text", "").strip()
    if hints:
        parts.extend(["\n\n## Hints from the original PR\n", hints])
    parts.append(
        "\n\n## Instructions\n"
        "Please fix the issue described above. Make minimal, targeted changes.\n"
        "Do NOT modify or add test files.\n"
        "After making changes, verify your fix is correct by running relevant tests or checking the code."
    )
    return "\n".join(parts)


def _collect_patch(workdir: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout


async def _run_instance(
    instance: dict,
    workdir: Path,
    config_api_key: str,
    config_base_url: str,
    config_model: str,
    max_turns: int,
) -> dict:
    instance_id = instance["instance_id"]
    engine, state, turn_limit_holder = _build_engine(workdir, config_api_key, config_base_url, config_model)
    turn_limit_holder["max"] = max_turns

    problem_prompt = _build_problem_prompt(instance)

    tool_events: list[dict] = []
    t0 = time.monotonic()
    status = "ok"

    try:
        async for event in engine.submit_message(problem_prompt, state):
            if isinstance(event, TextDelta):
                pass
            elif isinstance(event, ToolResultEvent):
                tool_events.append(
                    {
                        "tool": event.name,
                        "success": event.success,
                        "output_len": len(event.output),
                    }
                )
    except TimeoutError:
        status = "timeout"
    except Exception as exc:
        status = f"error: {exc!r}"
    except KeyboardInterrupt:
        status = "interrupted"
        raise

    elapsed = time.monotonic() - t0
    turns = engine.state.turn_count if engine.state else 0
    patch = _collect_patch(workdir)

    if not patch.strip():
        status = "empty_patch"

    return {
        "instance_id": instance_id,
        "status": status,
        "turns": turns,
        "elapsed_sec": round(elapsed, 1),
        "patch_length": len(patch),
        "num_tool_calls": len(tool_events),
        "tool_events": tool_events,
        "model_patch": patch,
    }


def _write_predictions(results: list[dict], model_name: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for r in results:
            entry = {
                "instance_id": r["instance_id"],
                "model_name_or_path": model_name,
                "model_patch": r["model_patch"],
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_trajectory(results: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = []
    for r in results:
        entry = {k: v for k, v in r.items() if k != "model_patch"}
        serializable.append(entry)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def _print_report(results: list[dict]) -> None:
    table = Table(title="SWE-bench Verified - Instance Report")
    table.add_column("Instance ID", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Turns", justify="right")
    table.add_column("Time(s)", justify="right")
    table.add_column("Patch Size", justify="right")
    table.add_column("Tool Calls", justify="right")

    for r in results:
        status = r["status"]
        if status == "ok":
            status_str = "[green]OK[/]"
        elif status == "empty_patch":
            status_str = "[yellow]EMPTY[/]"
        elif status == "timeout":
            status_str = "[red]TIMEOUT[/]"
        else:
            status_str = f"[red]{status}[/]"

        table.add_row(
            r["instance_id"],
            status_str,
            str(r["turns"]),
            str(r["elapsed_sec"]),
            str(r["patch_length"]),
            str(r["num_tool_calls"]),
        )

    console.print(table)

    total = len(results)
    if total == 0:
        return

    with_patch = [r for r in results if r["status"] == "ok"]
    without_patch = [r for r in results if r["status"] != "ok"]
    n_with_patch = len(with_patch)

    avg_turns_all = sum(r["turns"] for r in results) / total
    avg_turns_ok = sum(r["turns"] for r in with_patch) / n_with_patch if n_with_patch else 0
    avg_turns_fail = sum(r["turns"] for r in without_patch) / len(without_patch) if without_patch else 0

    avg_time = sum(r["elapsed_sec"] for r in results) / total
    avg_tools_all = sum(r["num_tool_calls"] for r in results) / total
    avg_tools_ok = sum(r["num_tool_calls"] for r in with_patch) / n_with_patch if n_with_patch else 0

    console.print("\n[bold]Summary:[/]")
    console.print(f"  Total:              {total}")
    console.print(f"  With patch:         {n_with_patch} ({n_with_patch / total * 100:.1f}%)")
    console.print(f"  Avg turns (all):    {avg_turns_all:.1f}")
    console.print(f"  Avg turns (patch):  {avg_turns_ok:.1f}")
    console.print(f"  Avg turns (fail):   {avg_turns_fail:.1f}")
    console.print(f"  Avg time:           {avg_time:.1f}s")
    console.print(f"  Avg tool calls:     {avg_tools_all:.1f} (all) / {avg_tools_ok:.1f} (patch)")

    tool_stats: dict[str, dict[str, int]] = {}
    for r in results:
        for ev in r.get("tool_events", []):
            name = ev["tool"]
            if name not in tool_stats:
                tool_stats[name] = {"total": 0, "success": 0}
            tool_stats[name]["total"] += 1
            if ev["success"]:
                tool_stats[name]["success"] += 1

    if tool_stats:
        tool_table = Table(title="Tool Call Statistics")
        tool_table.add_column("Tool", style="cyan")
        tool_table.add_column("Calls", justify="right")
        tool_table.add_column("Success", justify="right")
        tool_table.add_column("Rate", justify="right")

        for name in sorted(tool_stats):
            s = tool_stats[name]
            rate = s["success"] / s["total"] * 100
            tool_table.add_row(name, str(s["total"]), str(s["success"]), f"{rate:.1f}%")

        console.print(tool_table)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run mini_cc on SWE-bench Verified")
    parser.add_argument("--n", type=int, default=10, help="Number of instances to sample")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for sampling")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout per instance (seconds)")
    parser.add_argument("--max-turns", type=int, default=30, help="Max agent turns per instance")
    parser.add_argument("--output-dir", type=str, default="results/swebench", help="Output directory")
    parser.add_argument("--cache-dir", type=str, default="~/.cache/swebench", help="Repo cache directory")
    args = parser.parse_args()

    eval_api_key = os.environ.get("EVAL_API_KEY", "")
    eval_base_url = os.environ.get("EVAL_BASE_URL", "https://api.openai.com/v1")
    eval_model = os.environ.get("EVAL_MODEL", "")
    if not eval_model:
        console.print("[bold red]Error: EVAL_MODEL environment variable is required[/]")
        sys.exit(1)
    if not eval_api_key:
        console.print("[bold red]Error: EVAL_API_KEY environment variable is required[/]")
        sys.exit(1)

    cache_dir = Path(args.cache_dir).expanduser()
    output_dir = Path(args.output_dir)
    work_base = Path(f"/tmp/swebench_eval_{os.getpid()}")

    console.print("[bold]SWE-bench Verified Evaluation[/]")
    console.print(f"  Model:     {eval_model}")
    console.print(f"  N:         {args.n}")
    console.print(f"  Seed:      {args.seed}")
    console.print(f"  Timeout:   {args.timeout}s")
    console.print(f"  Max turns: {args.max_turns}")
    console.print()

    console.print("[bold]Loading dataset...[/]")
    instances = _load_instances(args.n, args.seed)
    console.print(f"  Sampled {len(instances)} instances")

    unique_repos = sorted({inst["repo"] for inst in instances})
    console.print(f"\n[bold]Caching repos ({len(unique_repos)} unique)...[/]")
    repo_cache: dict[str, Path] = {}
    for repo in unique_repos:
        repo_cache[repo] = _ensure_bare_clone(repo, cache_dir)

    results: list[dict] = []

    for i, inst in enumerate(instances):
        instance_id = inst["instance_id"]
        console.print(f"\n[bold cyan][{i + 1}/{len(instances)}] {instance_id}[/] ({inst['repo']})")

        bare_path = repo_cache[inst["repo"]]
        workdir = _checkout_workdir(bare_path, inst["base_commit"], work_base, instance_id)
        console.print(f"  Workdir: {workdir}")

        try:
            result = asyncio.run(
                asyncio.wait_for(
                    _run_instance(
                        instance=inst,
                        workdir=workdir,
                        config_api_key=eval_api_key,
                        config_base_url=eval_base_url,
                        config_model=eval_model,
                        max_turns=args.max_turns,
                    ),
                    timeout=args.timeout + 30,
                )
            )
        except TimeoutError:
            result = {
                "instance_id": instance_id,
                "status": "timeout",
                "turns": 0,
                "elapsed_sec": args.timeout,
                "patch_length": 0,
                "num_tool_calls": 0,
                "tool_events": [],
                "model_patch": "",
            }
        except KeyboardInterrupt:
            console.print("\n[bold red]Interrupted by user[/]")
            break
        finally:
            if workdir.is_dir():
                shutil.rmtree(workdir, ignore_errors=True)

        results.append(result)
        console.print(
            f"  -> {result['status']} | {result['turns']} turns | "
            f"{result['elapsed_sec']}s | patch={result['patch_length']} chars"
        )

    if work_base.is_dir():
        shutil.rmtree(work_base, ignore_errors=True)

    predictions_path = output_dir / "predictions.jsonl"
    trajectory_path = output_dir / "trajectory.json"
    _write_predictions(results, eval_model, predictions_path)
    _write_trajectory(results, trajectory_path)

    console.print("\n[bold]Outputs:[/]")
    console.print(f"  Predictions: {predictions_path}")
    console.print(f"  Trajectory:  {trajectory_path}")

    _print_report(results)

    console.print("\n[dim]Next: run harness evaluation to get resolve rate[/]")
    console.print(f"[dim]  bash scripts/eval/swebench_run_eval.sh {predictions_path}[/]")
    console.print("[dim]Then merge results:[/]")
    console.print(
        f"[dim]  uv run python scripts/eval/merge_report.py "
        f"--trajectory {trajectory_path} "
        f"--harness evaluation_results/mini_cc_eval/results.json "
        f"--output {output_dir / 'final_report.json'}[/]"
    )


if __name__ == "__main__":
    main()
