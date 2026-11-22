# SPDX-License-Identifier: MIT

"""Run matched cost/speed evaluations and compare the resulting tool trajectories."""

import argparse
import asyncio
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


def arm_summary(task: dict | None, aborted: bool, budget: int | None) -> dict:
    if task is None:
        return {"status": "missing"}

    report = task.get("tool_variants")
    if report is None or aborted or task.get("errors"):
        status = "incomplete"
    else:
        status = "passed" if task["passed"] else "failed"
    result = {"status": status, "score": task["score"]}
    if report is not None:
        cost = report["total_cost_units"]
        result.update(
            cost_units=cost,
            simulated_latency_ms=report["total_simulated_latency_ms"],
            injected_timeouts=report["injected_timeouts"],
            within_budget=None if budget is None else cost <= budget,
            attempts=report["attempts"],
        )
    return result


def compare_runs(tasks: tuple[str, ...], runs: dict, budget: int | None) -> dict:
    by_arm = {
        arm: {task["name"]: task for task in run.get("tasks", [])} for arm, run in runs.items()
    }
    aborted_by_arm = {
        arm: set(run.get("summary", {}).get("aborted_tasks", [])) for arm, run in runs.items()
    }
    pairs = []
    for name in tasks:
        cost, speed = (
            arm_summary(by_arm[arm].get(name), name in aborted_by_arm[arm], budget)
            for arm in PREFERENCES
        )
        pair = {"task": name, "cost": cost, "speed": speed}
        if "attempts" in cost and "attempts" in speed:
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
    return {
        "tasks": pairs,
        "summary": {
            "pairs": len(pairs),
            "both_passed": sum(
                pair["cost"]["status"] == pair["speed"]["status"] == "passed" for pair in pairs
            ),
            "with_tool_switch": sum(bool(pair.get("tool_switches")) for pair in pairs),
            "with_incomplete_arm": sum(
                any(pair[arm]["status"] in ("missing", "incomplete") for arm in PREFERENCES)
                for pair in pairs
            ),
        },
    }


async def run_pair(args, tasks: tuple[str, ...], domains: list[str]) -> dict:
    args.output_dir.mkdir(parents=True, exist_ok=False)
    runs = {}
    errors = {}
    scenario = "budget-speed" if args.budget is not None else "cost-speed"
    for arm in PREFERENCES:
        raw_path = args.output_dir / f"{arm}.json"
        print(f"Running {arm} preference", flush=True)
        try:
            async with asyncio.timeout(args.arm_timeout):
                await run_evaluation(
                    model=args.model,
                    domains=domains,
                    base_url=args.base_url,
                    api=args.api,
                    tasks=",".join(tasks),
                    toolset="variants",
                    variant_scenario=scenario,
                    variant_seed=args.variant_seed,
                    tool_preference=arm,
                    tool_budget=args.budget,
                    max_turns=args.max_steps,
                    max_concurrent=args.max_concurrent,
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
    (args.output_dir / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def print_report(report: dict) -> None:
    table = Table(
        "Task", "Cost arm", "Speed arm", "Cost saved", "Time saved (ms)", "Actions switched"
    )
    for pair in report["tasks"]:
        arms = []
        for arm in PREFERENCES:
            result = pair[arm]
            label = result["status"]
            if "cost_units" in result:
                label += f" · {result['cost_units']}u / {result['simulated_latency_ms']}ms"
            if result.get("within_budget") is False:
                label += " · over budget"
            arms.append(label)
        table.add_row(
            pair["task"],
            *arms,
            str(pair.get("cost_saved", "–")),
            str(pair.get("latency_saved_ms", "–")),
            str(len(pair.get("tool_switches", []))),
        )
    Console().print(table)


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
    parser.add_argument("--tasks", default=",".join(PILOT_TASKS), help="Comma-separated task IDs")
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="New directory for the reports"
    )
    parser.add_argument("--arm-timeout", type=int, default=300, help="Seconds allowed per arm")
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument("--max-concurrent", type=int, default=2)
    parser.add_argument("--variant-seed", type=int, default=0)
    parser.add_argument("--budget", type=int, help="Tool cost ceiling stated in both prompts")
    args = parser.parse_args()
    if (
        args.arm_timeout < 1
        or args.max_steps < 1
        or args.max_concurrent < 1
        or (args.budget is not None and args.budget < 1)
    ):
        parser.error("timeout, step limit, concurrency and optional budget must be positive")
    if args.output_dir.exists():
        parser.error("--output-dir must be a new directory")
    tasks = tuple(dict.fromkeys(name.strip() for name in args.tasks.split(",") if name.strip()))
    if not tasks:
        parser.error("choose at least one task")
    domains = list(dict.fromkeys(name.split(".", 1)[0] for name in tasks))
    try:
        available = {_task_name(row) for row in get_combined_dataset(domains)}
    except ValueError as exc:
        parser.error(str(exc))
    if unknown := set(tasks) - available:
        parser.error(f"unknown task IDs {', '.join(sorted(unknown))}")
    report = asyncio.run(run_pair(args, tasks, domains))
    print_report(report)
    print(f"Comparison saved to {args.output_dir / 'comparison.json'}")
    if report["summary"]["with_incomplete_arm"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
