# SPDX-License-Identifier: MIT

"""Screen benchmark tasks and evaluate budgeted tool selection against successful traces."""

import argparse
import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

from automationbench.scripts.paired_eval import PREFERENCES, run_pair
from automationbench.tool_variants import profiles_for_action

SCENARIO = "budget-speed"


def fixed_trace_optima(actions: tuple[str, ...]) -> dict[int, tuple[int, tuple[str, ...]]]:
    """Find the fastest tool assignment at each exact spend on a fixed action trace."""
    states = {0: (0, ())}
    for action in actions:
        next_states = {}
        for cost, (latency, tools) in states.items():
            for profile in profiles_for_action(action, SCENARIO):
                new_cost = cost + profile.cost_units
                new_latency = latency + profile.latency_ms
                candidate = new_latency, (*tools, f"{action}_{profile.suffix}")
                if new_cost not in next_states or candidate < next_states[new_cost]:
                    next_states[new_cost] = candidate
        states = next_states
    return states


def best_under_budget(optima: dict[int, tuple[int, tuple[str, ...]]], budget: int) -> dict | None:
    candidates = [
        (latency, cost, tools) for cost, (latency, tools) in optima.items() if cost <= budget
    ]
    if not candidates:
        return None
    latency, cost, tools = min(candidates)
    return {"cost_units": cost, "simulated_latency_ms": latency, "tools": tools}


def build_plan(references: list[dict], min_actions: int = 4) -> dict:
    names = set()
    accepted = defaultdict(set)
    for reference in references:
        aborted = set(reference.get("summary", {}).get("aborted_tasks", []))
        for task in reference["tasks"]:
            name = task["name"]
            names.add(name)
            attempts = (task.get("tool_variants") or {}).get("attempts") or []
            if (
                task.get("passed")
                and name not in aborted
                and not task.get("errors")
                and attempts
                and all(attempt.get("status") == "returned" for attempt in attempts)
            ):
                accepted[name].add(tuple(attempt["action"] for attempt in attempts))

    plan = {}
    for name in sorted(names):
        if not accepted[name]:
            plan[name] = {"tier": "pending", "reason": "no_successful_reference"}
            continue
        actions = min(accepted[name], key=lambda trace: (len(trace), -len(set(trace)), trace))
        savings = set()
        for action in actions:
            economy, fast = profiles_for_action(action, SCENARIO)
            savings.add(economy.latency_ms - fast.latency_ms)

        optima = fixed_trace_optima(actions)
        budget_points = []
        best_latency = optima[min(optima)][0]
        for cost in sorted(optima)[1:-1]:
            latency = optima[cost][0]
            if latency < best_latency:
                budget_points.append(cost)
                best_latency = latency
        if len(budget_points) > 3:
            budget_points = [budget_points[i] for i in (0, len(budget_points) // 2, -1)]

        if not budget_points:
            tier, reason = "control", "no_intermediate_budget"
        elif len(savings) < 2:
            tier, reason = "control", "uniform_latency_gains"
        elif len(set(actions)) < min_actions:
            tier, reason = "control", "few_distinct_actions"
        else:
            tier, reason = "main", "nonuniform_gains_on_multistep_trace"

        plan[name] = {
            "tier": tier,
            "reason": reason,
            "distinct_actions": len(set(actions)),
            "reference_actions": actions,
            "reference_count": len(accepted[name]),
            "budgets": [
                {"budget": budget, "reference_optimum": best_under_budget(optima, budget)}
                for budget in budget_points
            ],
        }
    return plan


def score_comparison(plan: dict, report: dict) -> list[dict]:
    if report.get("scenario") != SCENARIO or type(report.get("budget")) is not int:
        raise ValueError("comparison must come from paired_eval with --budget")
    budget = report["budget"]
    rows = []
    for pair in report["tasks"]:
        name = pair["task"]
        reference = tuple(plan.get(name, {}).get("reference_actions", ()))
        for arm in PREFERENCES:
            result = pair[arm]
            actions = tuple(a["action"] for a in result.get("attempts", []))
            feasible = result["status"] == "passed" and result.get("within_budget") is True
            row = {
                "task": name,
                "budget": budget,
                "arm": arm,
                "status": result["status"],
                "score": result.get("score"),
                "within_budget": result.get("within_budget"),
                "passed_within_budget": feasible,
                "matches_reference_trace": bool(reference) and actions == reference,
                "cost_units": result.get("cost_units"),
                "simulated_latency_ms": result.get("simulated_latency_ms"),
                "tools": [a["tool"] for a in result.get("attempts", [])],
                "optimality_gap_ms": None,
            }
            if feasible and actions:
                optimum = best_under_budget(fixed_trace_optima(actions), budget)
                simulated_latency = 0
                cost_units = 0
                for action, tool in zip(actions, row["tools"], strict=True):
                    profiles = {
                        f"{action}_{profile.suffix}": profile
                        for profile in profiles_for_action(action, SCENARIO)
                    }
                    if tool not in profiles:
                        raise ValueError(f"{name} used an unknown budget tool {tool}")
                    simulated_latency += profiles[tool].latency_ms
                    cost_units += profiles[tool].cost_units
                if (
                    optimum is None
                    or simulated_latency != row["simulated_latency_ms"]
                    or cost_units != row["cost_units"]
                ):
                    raise ValueError(f"{name} results do not match the current budget profile")
                row["optimality_gap_ms"] = simulated_latency - optimum["simulated_latency_ms"]
            rows.append(row)
    return rows


async def evaluate_plan(args, plan: dict) -> list[dict]:
    by_budget = defaultdict(list)
    for task, entry in plan.items():
        if entry["tier"] == "main" or (args.include_controls and entry["tier"] == "control"):
            for point in entry["budgets"]:
                by_budget[point["budget"]].append(task)
    if not by_budget:
        raise ValueError("no selected tasks have an intermediate budget")
    rows = []
    for budget, tasks in sorted(by_budget.items()):
        domains = list(dict.fromkeys(task.split(".", 1)[0] for task in tasks))
        arm_args = SimpleNamespace(
            **{**vars(args), "output_dir": args.output_dir / f"budget-{budget}", "budget": budget}
        )
        report = await run_pair(arm_args, tuple(tasks), domains)
        rows.extend(score_comparison(plan, report))
    return rows


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, action="append", required=True)
    parser.add_argument("--comparison", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-actions", type=int, default=4)
    parser.add_argument("--include-controls", action="store_true")
    parser.add_argument("--model", help="Run selected tasks using this model")
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL"))
    parser.add_argument("--api", default="auto", choices=("auto", "chat_completions", "responses"))
    parser.add_argument("--max-concurrent", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument("--arm-timeout", type=int, default=300)
    parser.add_argument("--variant-seed", type=int, default=0)
    args = parser.parse_args()
    if min(args.min_actions, args.max_concurrent, args.max_steps, args.arm_timeout) < 1:
        parser.error("action threshold, concurrency, step cap and timeout must be positive")
    if args.output_dir.exists():
        parser.error("--output-dir must be a new directory")

    plan = build_plan([json.loads(path.read_text()) for path in args.reference], args.min_actions)
    args.output_dir.mkdir(parents=True)
    (args.output_dir / "selection.json").write_text(json.dumps(plan, indent=2) + "\n")
    for task, entry in plan.items():
        print(f"{entry['tier']:>7}  {task} ({entry['reason']})")
    rows = [
        row
        for path in args.comparison
        for row in score_comparison(plan, json.loads(path.read_text()))
    ]
    if args.model:
        rows.extend(asyncio.run(evaluate_plan(args, plan)))
    if args.model or args.comparison:
        (args.output_dir / "scores.json").write_text(json.dumps(rows, indent=2) + "\n")
        if any(row["status"] in ("missing", "incomplete") for row in rows):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
