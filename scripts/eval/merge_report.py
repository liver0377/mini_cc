from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()


def _load_json(path: Path) -> dict | list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_harness_results(harness_path: Path) -> dict[str, bool]:
    data = _load_json(harness_path)

    if isinstance(data, list):
        resolved: dict[str, bool] = {}
        for entry in data:
            iid = entry.get("instance_id", "")
            resolved[iid] = entry.get("resolved", False)
        return resolved

    resolved_ids = set(data.get("resolved_ids", []))
    all_ids = (
        data.get("submitted_ids", [])
        or data.get("completed_ids", [])
        or data.get("resolved_ids", []) + data.get("unresolved_ids", [])
    )
    result: dict[str, bool] = {}
    for iid in all_ids:
        result[iid] = iid in resolved_ids
    return result


def _merge(trajectory: list[dict], harness: dict[str, bool]) -> list[dict]:
    merged = []
    for entry in trajectory:
        iid = entry["instance_id"]
        entry["resolved"] = harness.get(iid, False)
        merged.append(entry)
    return merged


def _print_report(merged: list[dict]) -> None:
    total = len(merged)
    if total == 0:
        console.print("[yellow]No results to report.[/]")
        return

    resolved_list = [r for r in merged if r["resolved"]]
    unresolved_list = [r for r in merged if not r["resolved"]]
    with_patch = [r for r in merged if r["status"] == "ok"]
    n_resolved = len(resolved_list)
    n_with_patch = len(with_patch)

    console.print()
    table = Table(title="SWE-bench Verified - Final Evaluation Report")
    table.add_column("Instance ID", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Resolved", justify="center")
    table.add_column("Turns", justify="right")
    table.add_column("Time(s)", justify="right")
    table.add_column("Patch Size", justify="right")
    table.add_column("Tool Calls", justify="right")

    for r in merged:
        status = r["status"]
        if status == "ok":
            status_str = "[green]OK[/]"
        elif status == "empty_patch":
            status_str = "[yellow]EMPTY[/]"
        elif status == "timeout":
            status_str = "[red]TIMEOUT[/]"
        else:
            status_str = f"[red]{status}[/]"

        resolved_str = "[green]YES[/]" if r["resolved"] else "[red]NO[/]"

        table.add_row(
            r["instance_id"],
            status_str,
            resolved_str,
            str(r["turns"]),
            str(r["elapsed_sec"]),
            str(r["patch_length"]),
            str(r["num_tool_calls"]),
        )

    console.print(table)

    resolve_rate = n_resolved / total * 100
    patch_rate = n_with_patch / total * 100

    avg_turns_all = sum(r["turns"] for r in merged) / total
    avg_turns_resolved = sum(r["turns"] for r in resolved_list) / n_resolved if n_resolved else 0
    avg_turns_unresolved = sum(r["turns"] for r in unresolved_list) / len(unresolved_list) if unresolved_list else 0

    avg_tools_all = sum(r["num_tool_calls"] for r in merged) / total
    avg_tools_resolved = sum(r["num_tool_calls"] for r in resolved_list) / n_resolved if n_resolved else 0
    avg_tools_unresolved = (
        sum(r["num_tool_calls"] for r in unresolved_list) / len(unresolved_list) if unresolved_list else 0
    )

    avg_time_all = sum(r["elapsed_sec"] for r in merged) / total
    avg_time_resolved = sum(r["elapsed_sec"] for r in resolved_list) / n_resolved if n_resolved else 0

    console.print()
    console.print("[bold]===== Core Metrics =====[/]")
    console.print(f"  Resolve Rate:           {n_resolved}/{total} ({resolve_rate:.1f}%)")
    console.print(f"  Patch Generated:        {n_with_patch}/{total} ({patch_rate:.1f}%)")
    console.print()

    console.print("[bold]===== Avg Repair Turns =====[/]")
    console.print(f"  All instances:          {avg_turns_all:.1f}")
    console.print(f"  Resolved:               {avg_turns_resolved:.1f}")
    console.print(f"  Unresolved:             {avg_turns_unresolved:.1f}")
    console.print()

    console.print("[bold]===== Tool Call Efficiency =====[/]")
    console.print(f"  Avg calls (all):        {avg_tools_all:.1f}")
    console.print(f"  Avg calls (resolved):   {avg_tools_resolved:.1f}")
    console.print(f"  Avg calls (unresolved): {avg_tools_unresolved:.1f}")
    console.print(f"  Avg time (all):         {avg_time_all:.1f}s")
    console.print(f"  Avg time (resolved):    {avg_time_resolved:.1f}s")

    tool_stats: dict[str, dict[str, int]] = {}
    for r in merged:
        for ev in r.get("tool_events", []):
            name = ev["tool"]
            if name not in tool_stats:
                tool_stats[name] = {"total": 0, "success": 0}
            tool_stats[name]["total"] += 1
            if ev["success"]:
                tool_stats[name]["success"] += 1

    if tool_stats:
        console.print()
        tool_table = Table(title="Tool Call Breakdown")
        tool_table.add_column("Tool", style="cyan")
        tool_table.add_column("Calls", justify="right")
        tool_table.add_column("Success", justify="right")
        tool_table.add_column("Rate", justify="right")

        for name in sorted(tool_stats, key=lambda n: tool_stats[n]["total"], reverse=True):
            s = tool_stats[name]
            rate = s["success"] / s["total"] * 100
            tool_table.add_row(name, str(s["total"]), str(s["success"]), f"{rate:.1f}%")

        console.print(tool_table)


def _write_final_report(merged: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = len(merged)
    resolved_list = [r for r in merged if r["resolved"]]

    summary = {
        "total_instances": total,
        "resolved_instances": len(resolved_list),
        "resolve_rate": len(resolved_list) / total * 100 if total else 0,
        "avg_turns_all": sum(r["turns"] for r in merged) / total if total else 0,
        "avg_turns_resolved": (sum(r["turns"] for r in resolved_list) / len(resolved_list) if resolved_list else 0),
        "avg_tool_calls_all": sum(r["num_tool_calls"] for r in merged) / total if total else 0,
        "avg_tool_calls_resolved": (
            sum(r["num_tool_calls"] for r in resolved_list) / len(resolved_list) if resolved_list else 0
        ),
        "avg_time_all": sum(r["elapsed_sec"] for r in merged) / total if total else 0,
        "instances": merged,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge SWE-bench trajectory + harness results into final report")
    parser.add_argument("--trajectory", type=str, required=True, help="Path to trajectory.json")
    parser.add_argument("--harness", type=str, required=True, help="Path to harness results.json")
    parser.add_argument("--output", type=str, default="results/swebench/final_report.json", help="Output path")
    args = parser.parse_args()

    traj_path = Path(args.trajectory)
    harness_path = Path(args.harness)
    output_path = Path(args.output)

    if not traj_path.is_file():
        console.print(f"[red]Trajectory not found: {traj_path}[/]")
        sys.exit(1)
    if not harness_path.is_file():
        console.print(f"[red]Harness results not found: {harness_path}[/]")
        sys.exit(1)

    console.print(f"[bold]Loading trajectory:[/]  {traj_path}")
    console.print(f"[bold]Loading harness:[/]     {harness_path}")

    trajectory: list[dict] = _load_json(traj_path)  # type: ignore[assignment]
    harness = _load_harness_results(harness_path)

    console.print(f"  Trajectory entries: {len(trajectory)}")
    console.print(f"  Harness entries:    {len(harness)}")

    merged = _merge(trajectory, harness)
    _print_report(merged)
    _write_final_report(merged, output_path)

    console.print(f"\n[bold]Final report saved to:[/] {output_path}")


if __name__ == "__main__":
    main()
