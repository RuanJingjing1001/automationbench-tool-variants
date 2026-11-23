# SPDX-License-Identifier: MIT

"""Compare native, cost-priority and latency-priority tool runs."""

import argparse
import asyncio
from collections import Counter
from itertools import combinations
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from automationbench.domains import get_combined_dataset
from automationbench.scripts.eval import _task_name, run_evaluation

PILOT_TASKS = (
    "simple.email_sf_contact_phone_update",
    "simple.email_sf_contact_city_update",
    "sales.create_important_draft",
    "operations.trello_basecamp_compliance",
    "operations.monday_email_update",
)
PREFERENCES = ("cost", "speed")
DEFAULT_MAX_TASK_TOKENS = 400_000
PILOT50_PATH = (
    Path(__file__).resolve().parents[2] / "benchmarks" / "holo4-effective-calls-gt10-pilot50.json"
)


def load_pilot50(limit: int | None = None) -> tuple[str, ...]:
    manifest = json.loads(PILOT50_PATH.read_text())
    tasks = tuple(row["task"] for row in manifest["tasks"])
    if len(tasks) != 50 or len(set(tasks)) != 50 or manifest["sample_size"] != 50:
        raise ValueError("pilot50 manifest must contain 50 unique task IDs")
    if limit is not None and not 1 <= limit <= len(tasks):
        raise ValueError("pilot50 limit must be between 1 and 50")
    return tasks[:limit]


def arm_summary(
    task: dict | None, aborted: bool, budget: int | None, *, native: bool = False
) -> dict:
    if task is None:
        return {"status": "missing"}

    report = task.get("tool_variants")
    if task.get("stop_condition") == "token_budget_exceeded":
        status = "token_budget_exceeded"
    elif (report is None and not native) or aborted or task.get("errors"):
        status = "incomplete"
    else:
        status = "passed" if task["passed"] else "failed"
    result = {"status": status, "score": task.get("score")}
    if "input_tokens" in task and "output_tokens" in task:
        result["model_tokens"] = task["input_tokens"] + task["output_tokens"]
    if report is not None:
        cost = report["total_cost_units"]
        attempts = report["attempts"]
        versions = Counter(attempt["tool"].rsplit("_", 1)[-1] for attempt in attempts)
        result.update(
            cost_units=cost,
            simulated_latency_ms=report["total_simulated_latency_ms"],
            injected_timeouts=report["injected_timeouts"],
            within_budget=None if budget is None else cost <= budget,
            attempts=attempts,
            variant_attempt_counts=dict(versions),
        )
    return result


def compare_runs(tasks: tuple[str, ...], runs: dict, budget: int | None) -> dict:
    arms = ("native", *PREFERENCES) if "native" in runs else PREFERENCES
    by_arm = {
        arm: {task["name"]: task for task in runs.get(arm, {}).get("tasks", [])} for arm in arms
    }
    aborted_by_arm = {
        arm: set(runs.get(arm, {}).get("summary", {}).get("aborted_tasks", [])) for arm in arms
    }
    pairs = []
    for name in tasks:
        pair = {"task": name}
        for arm in arms:
            pair[arm] = arm_summary(
                by_arm[arm].get(name), name in aborted_by_arm[arm], budget, native=arm == "native"
            )
        scores = {arm: pair[arm].get("score") for arm in arms}
        pair["score_deltas"] = {
            f"{right}_minus_{left}": scores[right] - scores[left]
            for left, right in combinations(arms, 2)
            if scores[left] is not None and scores[right] is not None
        }
        cost, speed = pair["cost"], pair["speed"]
        if (
            "attempts" in cost
            and "attempts" in speed
            and all(pair[arm]["status"] != "token_budget_exceeded" for arm in PREFERENCES)
        ):
            tools_by_action = []
            for result in (cost, speed):
                grouped = {}
                for attempt in result["attempts"]:
                    grouped.setdefault(attempt["action"], []).append(attempt["tool"])
                tools_by_action.append(grouped)
            cost_tools, speed_tools = tools_by_action
            shared = cost_tools.keys() & speed_tools.keys()
            pair.update(
                cost_saved=speed["cost_units"] - cost["cost_units"],
                latency_saved_ms=cost["simulated_latency_ms"] - speed["simulated_latency_ms"],
                tool_switches=[
                    {"action": action, "cost": cost_tools[action], "speed": speed_tools[action]}
                    for action in sorted(shared)
                    if set(cost_tools[action]) != set(speed_tools[action])
                ],
                cost_only_actions=sorted(cost_tools.keys() - speed_tools.keys()),
                speed_only_actions=sorted(speed_tools.keys() - cost_tools.keys()),
            )
        pairs.append(pair)

    by_arm_metrics = {}
    for arm in arms:
        scores = [pair[arm]["score"] for pair in pairs if pair[arm].get("score") is not None]
        scored = len(scores)
        passed = sum(score == 1.0 for score in scores)
        by_arm_metrics[arm] = {
            "scored_tasks": scored,
            "avg_score": sum(scores) / scored if scored else None,
            "strict_pass_count": passed,
            "strict_pass_rate": passed / scored if scored else None,
        }
    return {
        "tasks": pairs,
        "summary": {
            "pairs": len(pairs),
            "by_arm": by_arm_metrics,
            "both_passed": sum(
                pair["cost"]["status"] == pair["speed"]["status"] == "passed" for pair in pairs
            ),
            "with_tool_switch": sum(bool(pair.get("tool_switches")) for pair in pairs),
            "with_incomplete_arm": sum(
                any(pair[arm]["status"] in ("missing", "incomplete") for arm in arms)
                for pair in pairs
            ),
            "token_budget_exceeded_by_arm": {
                arm: sum(pair[arm]["status"] == "token_budget_exceeded" for pair in pairs)
                for arm in arms
            },
        },
    }


async def run_pair(
    args,
    tasks: tuple[str, ...],
    domains: list[str],
    *,
    include_native: bool = False,
    max_task_tokens: int | None = None,
) -> dict:
    args.output_dir.mkdir(parents=True, exist_ok=False)
    runs = {}
    errors = {}
    scenario = "budget-speed" if args.budget is not None else "cost-speed"
    arms = ("native", *PREFERENCES) if include_native else PREFERENCES
    for arm in arms:
        native = arm == "native"
        raw_path = args.output_dir / f"{arm}.json"
        print(f"Running {arm} arm", flush=True)
        try:
            async with asyncio.timeout(args.arm_timeout):
                await run_evaluation(
                    model=args.model,
                    domains=domains,
                    base_url=args.base_url,
                    api=args.api,
                    tasks=",".join(tasks),
                    toolset="limited_zapier" if native else "variants",
                    variant_scenario=scenario,
                    variant_seed=args.variant_seed,
                    tool_preference="neutral" if native else arm,
                    tool_budget=None if native else args.budget,
                    max_turns=args.max_steps,
                    max_concurrent=args.max_concurrent,
                    max_task_tokens=max_task_tokens,
                    export_json=str(raw_path),
                )
            runs[arm] = json.loads(raw_path.read_text())
        except Exception as exc:
            # Preserve the other arm and report the failure without copying credentials or provider payloads.
            errors[arm] = type(exc).__name__
            runs[arm] = {}
            print(f"{arm} evaluation did not finish ({errors[arm]})", flush=True)

    report = compare_runs(tasks, runs, args.budget)
    report.update(model=args.model, scenario=scenario, budget=args.budget, run_errors=errors)
    if max_task_tokens is not None:
        report["max_task_tokens"] = max_task_tokens
    (args.output_dir / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def print_report(report: dict) -> None:
    console = Console()
    summary = report["summary"]
    metrics_table = Table(
        "Arm", "Scored / scheduled", "Avg. task score", "Strict pass rate", "Token cap hits"
    )
    for arm, metrics in summary["by_arm"].items():
        avg_score = metrics["avg_score"]
        pass_rate = metrics["strict_pass_rate"]
        metrics_table.add_row(
            arm.title(),
            f"{metrics['scored_tasks']}/{summary['pairs']}",
            f"{avg_score:.1%}" if avg_score is not None else "–",
            f"{pass_rate:.1%} ({metrics['strict_pass_count']}/{metrics['scored_tasks']})"
            if pass_rate is not None
            else "–",
            str(summary["token_budget_exceeded_by_arm"][arm]),
        )
    console.print(metrics_table)

    table = Table(
        caption="Score Δ = speed − cost in percentage points.",
    )
    table.add_column("Task", overflow="fold", ratio=2)
    for arm in summary["by_arm"]:
        table.add_column(arm.title(), ratio=1)
    for heading in ("Score Δ", "Cost saved", "ms saved", "Switches"):
        table.add_column(heading, justify="right")
    for pair in report["tasks"]:
        arms = []
        for arm in summary["by_arm"]:
            result = pair[arm]
            status = result["status"]
            score = result.get("score")
            label = f"{score:.1%}" if score is not None else "–"
            if status not in ("passed", "failed"):
                label += "\n" + ("token cap" if status == "token_budget_exceeded" else status)
            if "cost_units" in result:
                label += f"\n{result['cost_units']}u/{result['simulated_latency_ms']}ms"
            if result.get("within_budget") is False:
                label += "\nover budget"
            arms.append(label)
        delta = pair["score_deltas"].get("speed_minus_cost")
        table.add_row(
            pair["task"],
            *arms,
            f"{delta * 100:+.1f}" if delta is not None else "–",
            str(pair.get("cost_saved", "–")),
            str(pair.get("latency_saved_ms", "–")),
            str(len(pair.get("tool_switches", []))),
        )
    console.print(table)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL"))
    parser.add_argument(
        "--api",
        default="auto",
        choices=("auto", "anthropic", "chat_completions", "responses", "gemini_interactions"),
    )
    task_selection = parser.add_mutually_exclusive_group()
    task_selection.add_argument("--tasks", help="Comma-separated task IDs")
    task_selection.add_argument(
        "--pilot50", action="store_true", help="Use the committed 50-task sample"
    )
    parser.add_argument("--limit", type=int, help="Run only the first N tasks in --pilot50")
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="New directory for the reports"
    )
    parser.add_argument(
        "--arm-timeout", type=int, help="Seconds allowed per arm (pilot50: 3600, otherwise: 300)"
    )
    parser.add_argument(
        "--max-steps", type=int, help="Model turns per task (pilot50: 64, otherwise: 16)"
    )
    parser.add_argument("--max-concurrent", type=int, default=2)
    parser.add_argument(
        "--max-task-tokens",
        type=int,
        default=DEFAULT_MAX_TASK_TOKENS,
        help="Cumulative model tokens per task in each arm (default: 400000)",
    )
    parser.add_argument("--variant-seed", type=int, default=0)
    parser.add_argument(
        "--budget", type=int, help="Tool cost ceiling stated in the variant prompts"
    )
    args = parser.parse_args()
    if args.limit is not None and not args.pilot50:
        parser.error("--limit requires --pilot50")
    if args.arm_timeout is None:
        args.arm_timeout = 3600 if args.pilot50 else 300
    if args.max_steps is None:
        args.max_steps = 64 if args.pilot50 else 16
    if (
        args.arm_timeout < 1
        or args.max_steps < 1
        or args.max_concurrent < 1
        or args.max_task_tokens < 1
        or (args.budget is not None and args.budget < 1)
    ):
        parser.error("timeout, step limit, concurrency and token budgets must be positive")
    if args.output_dir.exists():
        parser.error("--output-dir must be a new directory")
    if args.pilot50:
        try:
            tasks = load_pilot50(args.limit)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(f"cannot load pilot50 tasks: {exc}")
    elif args.tasks is not None:
        tasks = tuple(dict.fromkeys(name.strip() for name in args.tasks.split(",") if name.strip()))
    else:
        tasks = PILOT_TASKS
    if not tasks:
        parser.error("choose at least one task")
    domains = list(dict.fromkeys(name.split(".", 1)[0] for name in tasks))
    try:
        available = {_task_name(row) for row in get_combined_dataset(domains)}
    except ValueError as exc:
        parser.error(str(exc))
    if unknown := set(tasks) - available:
        parser.error(f"unknown task IDs {', '.join(sorted(unknown))}")
    report = asyncio.run(
        run_pair(args, tasks, domains, include_native=True, max_task_tokens=args.max_task_tokens)
    )
    print_report(report)
    print(f"Comparison saved to {args.output_dir / 'comparison.json'}")
    if report["summary"]["with_incomplete_arm"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
