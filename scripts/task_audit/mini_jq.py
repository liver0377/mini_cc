from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CASES_FILE = Path(__file__).parent / "cases" / "mini_jq_cases.json"

CATEGORIES_ORDER = [
    "identity",
    "field_access",
    "nested_field_access",
    "array_index",
    "pipe",
    "array_iterator",
    "error_contract",
]


def load_cases() -> list[dict[str, object]]:
    if not CASES_FILE.is_file():
        print(f"Cases file not found: {CASES_FILE}", file=sys.stderr)
        return []
    with CASES_FILE.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        print("Cases file must contain a JSON array", file=sys.stderr)
        return []
    return data


def find_mini_jq_binary() -> str | None:
    candidate = shutil.which("mini-jq")
    if candidate is not None:
        return candidate
    for path_str in ("./mini-jq", "./target/release/mini-jq", "./target/debug/mini-jq"):
        p = Path(path_str)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())
    return None


def run_jq(jq_bin: str, filter_expr: str, input_json: str, timeout: int = 5) -> tuple[str, str, int]:
    try:
        proc = subprocess.run(
            [jq_bin, filter_expr],
            input=input_json,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except OSError as exc:
        return "", str(exc), -1
    return proc.stdout.rstrip("\n"), proc.stderr.rstrip("\n"), proc.returncode


def compare_outputs(
    ref_out: str,
    ref_err: str,
    ref_rc: int,
    target_out: str,
    target_err: str,
    target_rc: int,
    case: dict[str, object],
) -> tuple[bool, str]:
    expected_exit = case.get("expected_exit")
    if isinstance(expected_exit, int):
        if expected_exit == 0 and target_rc != 0:
            return False, f"expected exit 0, got {target_rc}: {target_err}"
        if expected_exit != 0 and target_rc == 0:
            return False, f"expected non-zero exit, got 0 with output: {target_out}"
        if expected_exit != 0 and target_rc != 0:
            return True, ""
    if ref_rc != 0 and target_rc != 0:
        if ref_rc == target_rc:
            return True, ""
        return False, f"both errored with different exit codes: ref={ref_rc} target={target_rc}"
    if ref_rc == 0 and target_rc != 0:
        return False, f"ref succeeded but target failed with exit {target_rc}: {target_err}"
    if ref_rc != 0 and target_rc == 0:
        return False, f"ref failed but target succeeded with output: {target_out}"
    if ref_out != target_out:
        return False, f"output mismatch: ref={ref_out!r} target={target_out!r}"
    return True, ""


def build_coverage(
    cases: list[dict[str, object]],
    results: dict[str, bool],
) -> dict[str, bool | str]:
    category_state: dict[str, list[bool]] = {}
    for case in cases:
        cat = str(case.get("category", "unknown"))
        passed = results.get(str(case.get("id", "")), False)
        category_state.setdefault(cat, []).append(passed)
    coverage: dict[str, bool | str] = {}
    for cat in category_state:
        bools = category_state[cat]
        if all(bools):
            coverage[cat] = True
        elif any(bools):
            coverage[cat] = "partial"
        else:
            coverage[cat] = False
    return coverage


def find_blockers(
    cases: list[dict[str, object]],
    results: dict[str, bool],
    details: dict[str, str],
) -> list[str]:
    failed_categories: dict[str, list[str]] = {}
    for case in cases:
        case_id = str(case.get("id", ""))
        if not results.get(case_id, False):
            cat = str(case.get("category", "unknown"))
            failed_categories.setdefault(cat, []).append(case_id)
    blockers: list[str] = []
    for cat in CATEGORIES_ORDER:
        if cat in failed_categories:
            ids = failed_categories[cat]
            if len(ids) == 1:
                blockers.append(f"{cat}: case {ids[0]} failed ({details.get(ids[0], 'unknown reason')})")
            else:
                blockers.append(f"{cat}: {len(ids)} cases failed ({', '.join(ids[:3])})")
    return blockers


def find_regressions(
    results: dict[str, bool],
    prev_results: dict[str, bool] | None,
) -> list[str]:
    if prev_results is None:
        return []
    regressions: list[str] = []
    for case_id, was_pass in prev_results.items():
        if was_pass and not results.get(case_id, False):
            regressions.append(f"{case_id}: was passing, now failing")
    return regressions


def recommend_next_focus(
    coverage: dict[str, bool | str],
    blockers: list[str],
) -> str | None:
    priority = ["identity", "field_access", "nested_field_access", "array_index", "pipe", "array_iterator"]
    for cat in priority:
        state = coverage.get(cat)
        if state is False:
            return f"implement_{cat}"
        if state == "partial":
            return f"fix_{cat}_edge_cases"
    if blockers:
        return "resolve_remaining_blockers"
    return "all_semantic_cases_passed"


def main() -> None:
    jq_bin = shutil.which("jq")
    if jq_bin is None:
        emit_error_artifact("jq not found on PATH; cannot run semantic audit")
        sys.exit(1)

    mini_jq_bin = find_mini_jq_binary()
    if mini_jq_bin is None:
        emit_error_artifact("mini-jq binary not found on PATH or in common locations")
        sys.exit(1)

    cases = load_cases()
    if not cases:
        emit_error_artifact(f"no test cases loaded from {CASES_FILE}")
        sys.exit(1)

    prev_path = _resolve_prev_path()
    prev_results: dict[str, bool] | None = None
    if prev_path is not None and prev_path.is_file():
        try:
            prev_data = json.loads(prev_path.read_text(encoding="utf-8"))
            prev_results = prev_data.get("case_results")
        except (OSError, json.JSONDecodeError):
            pass

    results: dict[str, bool] = {}
    details: dict[str, str] = {}
    passed_count = 0
    failed_count = 0
    improvements: list[str] = []

    for case in cases:
        case_id = str(case.get("id", ""))
        filter_expr = str(case.get("filter", "."))
        input_json = str(case.get("input", "null"))

        ref_out, ref_err, ref_rc = run_jq(jq_bin, filter_expr, input_json)
        tgt_out, tgt_err, tgt_rc = run_jq(mini_jq_bin, filter_expr, input_json)

        ok, reason = compare_outputs(ref_out, ref_err, ref_rc, tgt_out, tgt_err, tgt_rc, case)
        results[case_id] = ok
        if ok:
            passed_count += 1
        else:
            failed_count += 1
            details[case_id] = reason
        if prev_results is not None:
            was_pass = prev_results.get(case_id)
            if not was_pass and ok:
                improvements.append(f"{case_id}: now passing (was failing)")

    coverage = build_coverage(cases, results)
    blockers = find_blockers(cases, results, details)
    regressions = find_regressions(results, prev_results)
    next_focus = recommend_next_focus(coverage, blockers)

    score_total = passed_count * 10 - failed_count * 5

    artifact = {
        "profile": "mini_jq",
        "summary": {
            "cases_total": len(cases),
            "cases_passed": passed_count,
            "cases_failed": failed_count,
        },
        "score_total": score_total,
        "coverage": coverage,
        "blockers": blockers[:5],
        "regressions": regressions[:5],
        "improvements": improvements[:5],
        "recommended_next_focus": next_focus,
        "case_results": results,
    }

    json.dump(artifact, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")

    if failed_count > 0:
        sys.exit(0)


def _resolve_prev_path() -> Path | None:
    env_path = os.environ.get("MINI_JQ_PREVIOUS_AUDIT")
    if env_path:
        return Path(env_path)
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    candidate = Path(__file__).parent.parent.parent / ".mini_cc" / "latest_jq_audit.json"
    if candidate.is_file():
        return candidate
    return None


def emit_error_artifact(message: str) -> None:
    artifact = {
        "profile": "mini_jq",
        "summary": message,
        "blockers": [message],
        "case_results": {},
    }
    json.dump(artifact, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
