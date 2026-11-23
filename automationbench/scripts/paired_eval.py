# SPDX-License-Identifier: MIT

"""Compare native, cost-priority and latency-priority tool runs."""

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
from itertools import combinations
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from automationbench.domains import get_combined_dataset
from automationbench.scripts.complete_run import recompute_aggregates
from automationbench.scripts.eval import _task_name, run_evaluation
from automationbench.tool_variants import (
    MULTI_OBJECTIVE,
    MULTI_OBJECTIVE_FAULT_MODEL,
    MULTI_OBJECTIVE_PROFILE_SCOPE,
    PREFERENCES as EXECUTION_PREFERENCES,
    SCENARIOS,
)

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
PILOT25_PATH = PILOT50_PATH.with_name("holo4-effective-calls-pilot25-11-to-20.json")


def load_pilot50(limit: int | None = None) -> tuple[str, ...]:
    return load_pilot(PILOT50_PATH, 50, limit)


def load_pilot(path: Path, size: int, limit: int | None) -> tuple[str, ...]:
    manifest = json.loads(path.read_text())
    tasks = tuple(row["task"] for row in manifest["tasks"])
    if len(tasks) != size or len(set(tasks)) != size or manifest["sample_size"] != size:
        raise ValueError(f"pilot manifest must contain {size} unique task IDs")
    if limit is not None and not 1 <= limit <= len(tasks):
        raise ValueError(f"pilot limit must be between 1 and {size}")
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
        if "injected_failures" in report:
            failures = report["injected_failures"]
            result.update(
                injected_failures=failures,
                tool_attempt_count=len(attempts),
                injected_failure_rate=failures / len(attempts) if attempts else None,
                recovered_failures=sum(a.get("recovered_after_failure", False) for a in attempts),
            )
        elif report.get("scenario") == MULTI_OBJECTIVE:
            result["recovered_timeouts"] = sum(
                attempt.get("recovered_after_timeout", False) for attempt in attempts
            )
    return result


def native_full_score_tasks(run: dict) -> set[str]:
    aborted = set(run.get("summary", {}).get("aborted_tasks", []))
    return {
        task["name"]
        for task in run.get("tasks", [])
        if task.get("score") == 1.0 and task["name"] not in aborted and not task.get("errors")
    }


def compare_runs(
    tasks: tuple[str, ...], runs: dict, budget: int | None, *, native_pass_only: bool = False
) -> dict:
    arms = ("native", *PREFERENCES) if "native" in runs else PREFERENCES
    if "stability" in runs:
        arms = (*arms, "stability")
    eligible = native_full_score_tasks(runs.get("native", {})) if native_pass_only else set(tasks)
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
            if arm != "native" and name not in eligible:
                pair[arm] = {"status": "skipped_native_gate"}
                continue
            pair[arm] = arm_summary(
                by_arm[arm].get(name), name in aborted_by_arm[arm], budget, native=arm == "native"
            )
        scores = {arm: pair[arm].get("score") for arm in arms}
        pair["score_deltas"] = {
            f"{right}_minus_{left}": scores[right] - scores[left]
            for left, right in combinations(arms, 2)
            if scores[left] is not None and scores[right] is not None
        }
        if "stability" in arms:
            comparable = [
                arm
                for arm in arms
                if "cost_units" in pair[arm] and pair[arm]["status"] in ("passed", "failed")
            ]
            pair["tool_metric_deltas"] = {
                f"{right}_minus_{left}": {
                    key: pair[right][key] - pair[left][key]
                    for key in (
                        "cost_units",
                        "simulated_latency_ms",
                        "injected_failures",
                        "injected_timeouts",
                    )
                    if key in pair[left] and key in pair[right]
                }
                for left, right in combinations(comparable, 2)
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
        if "stability" in arms:
            tool_results = [pair[arm] for pair in pairs if "cost_units" in pair[arm]]
            if tool_results:
                by_arm_metrics[arm].update(
                    tasks_with_tool_logs=len(tool_results),
                    total_cost_units=sum(result["cost_units"] for result in tool_results),
                    total_simulated_latency_ms=sum(
                        result["simulated_latency_ms"] for result in tool_results
                    ),
                    injected_timeouts=sum(result["injected_timeouts"] for result in tool_results),
                    recovered_timeouts=sum(
                        result.get("recovered_timeouts", 0) for result in tool_results
                    ),
                )
                failure_results = [r for r in tool_results if "injected_failures" in r]
                if failure_results:
                    failures = sum(r["injected_failures"] for r in failure_results)
                    attempts = sum(r["tool_attempt_count"] for r in failure_results)
                    by_arm_metrics[arm].update(
                        injected_failures=failures,
                        recovered_failures=sum(r["recovered_failures"] for r in failure_results),
                        tool_attempt_count=attempts,
                        injected_failure_rate=failures / attempts if attempts else None,
                    )
    report = {
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
    if native_pass_only:
        report["summary"]["native_eligible_tasks"] = [name for name in tasks if name in eligible]
    return report


async def run_pair(
    args,
    tasks: tuple[str, ...],
    domains: list[str],
    *,
    include_native: bool = False,
    max_task_tokens: int | None = None,
    variant_scenario: str | None = None,
) -> dict:
    resume = getattr(args, "resume", False)
    native_pass_only = getattr(args, "native_pass_only", False)
    include_native = include_native or native_pass_only
    scenario = variant_scenario or ("budget-speed" if args.budget is not None else "cost-speed")
    arms = ("native", *PREFERENCES) if include_native else PREFERENCES
    if scenario == MULTI_OBJECTIVE:
        arms = (*arms, "stability")

    async def evaluate(arm, requested, run_domains, path, concurrency):
        native = arm == "native"
        await run_evaluation(
            model=args.model,
            domains=run_domains,
            base_url=args.base_url,
            api=args.api,
            tasks=",".join(requested),
            toolset="limited_zapier" if native else "variants",
            variant_scenario=scenario,
            variant_seed=args.variant_seed,
            tool_preference="neutral" if native else arm,
            tool_budget=None if native else args.budget,
            max_turns=args.max_steps,
            max_concurrent=concurrency,
            max_task_tokens=max_task_tokens,
            export_json=str(path),
        )
        return json.loads(path.read_text())

    if resume:
        import fcntl

        config = {
            "model": args.model,
            "endpoint_sha256": sha256((args.base_url or "").encode()).hexdigest(),
            "api": args.api,
            "arms": list(arms),
            "scenario": scenario,
            "profiles": [
                asdict(profile)
                if scenario == MULTI_OBJECTIVE
                else {
                    **{k: v for k, v in asdict(profile).items() if k != "failure_probability"},
                    "timeout_ms": None,
                }
                for profile in SCENARIOS[scenario]
            ],
            "preferences": {arm: EXECUTION_PREFERENCES.get(arm, "") for arm in arms},
            "variant_seed": args.variant_seed,
            "profile_scope": MULTI_OBJECTIVE_PROFILE_SCOPE if scenario == MULTI_OBJECTIVE else None,
            "budget": args.budget,
            "max_steps": args.max_steps,
            "max_task_tokens": max_task_tokens,
        }
        if native_pass_only:
            config["native_pass_only"] = True
        if scenario == MULTI_OBJECTIVE:
            config["fault_model"] = MULTI_OBJECTIVE_FAULT_MODEL
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with (args.output_dir / ".resume.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("another process is already running this experiment") from exc
            tasks, runs, errors = await run_resumable(args, tasks, arms, config, evaluate)
            return save_comparison(args, tasks, runs, errors, scenario, max_task_tokens)
    else:
        args.output_dir.mkdir(parents=True, exist_ok=False)
        runs, errors = {}, {}
        for arm in arms:
            raw_path = args.output_dir / f"{arm}.json"
            requested = tasks
            if native_pass_only and arm != "native":
                eligible = native_full_score_tasks(runs["native"])
                requested = tuple(name for name in tasks if name in eligible)
            if not requested:
                runs[arm] = {"tasks": []}
                write_json(raw_path, runs[arm])
                continue
            print(f"Running {arm} arm", flush=True)
            try:
                async with asyncio.timeout(args.arm_timeout):
                    runs[arm] = await evaluate(
                        arm, requested, domains, raw_path, args.max_concurrent
                    )
            except Exception as exc:
                errors[arm] = type(exc).__name__
                runs[arm] = {}
                print(f"{arm} evaluation did not finish ({errors[arm]})", flush=True)

    return save_comparison(args, tasks, runs, errors, scenario, max_task_tokens)


def save_comparison(args, tasks, runs, errors, scenario, max_task_tokens):
    report = compare_runs(
        tasks, runs, args.budget, native_pass_only=getattr(args, "native_pass_only", False)
    )
    report.update(model=args.model, scenario=scenario, budget=args.budget, run_errors=errors)
    if scenario == MULTI_OBJECTIVE:
        report["variant_seed"] = args.variant_seed
        report["variant_profile_scope"] = MULTI_OBJECTIVE_PROFILE_SCOPE
        report["variant_fault_model"] = MULTI_OBJECTIVE_FAULT_MODEL
    if max_task_tokens is not None:
        report["max_task_tokens"] = max_task_tokens
    write_json(args.output_dir / "comparison.json", report)
    return report


def write_json(path: Path, data: dict) -> None:
    with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as tmp:
        temporary_path = Path(tmp.name)
        try:
            json.dump(data, tmp, indent=2)
            tmp.write("\n")
            tmp.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


async def run_resumable(args, requested, arms, config, evaluate):
    directory = args.output_dir
    manifest_path = directory / "experiment.json"
    previous = []
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        old_config = manifest["config"]
        changed = [
            key
            for key in sorted(old_config.keys() | config.keys())
            if old_config.get(key) != config.get(key)
        ]
        if changed or manifest.get("version") != 1:
            raise ValueError(f"resume configuration changed: {', '.join(changed) or 'format'}")
        previous = manifest["tasks"]
    elif any(path.name != ".resume.lock" for path in directory.iterdir()):
        raise ValueError("resume requires a new directory or one with experiment.json")

    tasks = tuple(dict.fromkeys([*previous, *requested]))
    write_json(manifest_path, {"version": 1, "config": config, "tasks": list(tasks)})
    runs, errors = {}, {}
    for arm in arms:
        arm_tasks = tasks
        if config.get("native_pass_only") and arm != "native":
            eligible = native_full_score_tasks(runs["native"])
            arm_tasks = tuple(name for name in tasks if name in eligible)
        checkpoints = directory / ".rollouts" / arm
        checkpoints.mkdir(parents=True, exist_ok=True)
        records, pending, arm_errors = {}, [], {}
        for name in arm_tasks:
            path = checkpoints / f"{sha256(name.encode()).hexdigest()}.json"
            if path.exists():
                records[name] = json.loads(path.read_text())
            else:
                pending.append((name, path))
        print(f"{arm}: reusing {len(records)}, running {len(pending)}", flush=True)
        slots = asyncio.Semaphore(args.max_concurrent)

        async def run_one(name, path):
            async with slots:
                try:
                    with TemporaryDirectory(dir=checkpoints) as temporary:
                        result = await evaluate(
                            arm,
                            (name,),
                            [name.split(".", 1)[0]],
                            Path(temporary) / "result.json",
                            1,
                        )
                    (task,) = result["tasks"]
                    if task["name"] != name:
                        raise ValueError("unexpected task in rollout export")
                    aborted = name in result.get("summary", {}).get("aborted_tasks", [])
                    status = arm_summary(task, aborted, args.budget, native=arm == "native")[
                        "status"
                    ]
                    if status == "incomplete":
                        arm_errors[name] = "IncompleteRollout"
                    else:
                        write_json(path, result)
                    records[name] = result
                except Exception as exc:
                    arm_errors[name] = type(exc).__name__

        try:
            async with asyncio.timeout(args.arm_timeout):
                await asyncio.gather(*(run_one(name, path) for name, path in pending))
        except TimeoutError:
            arm_errors.update(
                (name, "TimeoutError")
                for name, _ in pending
                if name not in records and name not in arm_errors
            )
        if arm_errors:
            errors[arm] = arm_errors

        exports = [records[name] for name in arm_tasks if name in records]
        rows = [dict(result["tasks"][0], id=index) for index, result in enumerate(exports, 1)]
        meta = dict(exports[0].get("meta", {})) if exports else {}
        meta.update(
            domains=list(dict.fromkeys(task["name"].split(".", 1)[0] for task in rows)),
            total_tasks=len(rows),
            duration_seconds=sum(
                result.get("meta", {}).get("duration_seconds", 0) for result in exports
            ),
        )
        combined = {"meta": meta, "tasks": rows}
        recompute_aggregates(combined, args.max_steps)
        combined["summary"]["aborted_tasks"] = [
            name
            for name in tasks
            if name in records and name in records[name].get("summary", {}).get("aborted_tasks", [])
        ]
        for key, field in (
            ("total_cached_input_tokens", "cached_input_tokens"),
            ("total_reasoning_tokens", "reasoning_tokens"),
            ("total_tool_calls", "num_tool_calls"),
            ("total_model_time_s", "model_time_s"),
            ("total_tool_time_s", "tool_time_s"),
            ("tasks_refusal_stopped", "died_on_refusal"),
        ):
            combined["summary"][key] = sum(task.get(field) or 0 for task in rows)
        runs[arm] = combined
        write_json(directory / f"{arm}.json", combined)
    return tasks, runs, errors


def print_report(report: dict) -> None:
    console = Console()
    summary = report["summary"]
    metrics_table = Table(
        "Arm", "Scored / scheduled", "Avg. task score", "Strict pass rate", "Token cap hits"
    )
    multi_objective = report.get("scenario") == MULTI_OBJECTIVE
    failure_model = report.get("variant_fault_model") == MULTI_OBJECTIVE_FAULT_MODEL
    eligible = summary.get("native_eligible_tasks")
    if eligible is not None:
        console.print(
            f"Native full score: {len(eligible)}/{summary['pairs']} tasks eligible for variants"
        )
    if multi_objective:
        faults_heading = "Failures / attempts" if failure_model else "Timeouts"
        for heading in ("Total cost", "Total ms", faults_heading, "Recoveries"):
            metrics_table.add_column(heading, justify="right")
    for arm, metrics in summary["by_arm"].items():
        avg_score = metrics["avg_score"]
        pass_rate = metrics["strict_pass_rate"]
        scheduled = len(eligible) if eligible is not None and arm != "native" else summary["pairs"]
        tool_columns = []
        if multi_objective:
            tool_columns = [
                str(metrics.get(key, "–"))
                for key in ("total_cost_units", "total_simulated_latency_ms")
            ]
            if failure_model:
                failures = metrics.get("injected_failures")
                fault_count = (
                    f"{failures}/{metrics['tool_attempt_count']}" if failures is not None else "–"
                )
                tool_columns.extend([fault_count, str(metrics.get("recovered_failures", "–"))])
            else:
                tool_columns.extend(
                    str(metrics.get(key, "–"))
                    for key in ("injected_timeouts", "recovered_timeouts")
                )
        metrics_table.add_row(
            arm.title(),
            f"{metrics['scored_tasks']}/{scheduled}",
            f"{avg_score:.1%}" if avg_score is not None else "–",
            f"{pass_rate:.1%} ({metrics['strict_pass_count']}/{metrics['scored_tasks']})"
            if pass_rate is not None
            else "–",
            str(summary["token_budget_exceeded_by_arm"][arm]),
            *tool_columns,
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
            if status == "skipped_native_gate":
                label = "native gate"
            elif status not in ("passed", "failed"):
                label += "\n" + ("token cap" if status == "token_budget_exceeded" else status)
            if "cost_units" in result:
                label += f"\n{result['cost_units']}u/{result['simulated_latency_ms']}ms"
            if "recovered_failures" in result:
                label += f"\n{result['injected_failures']}/{result['tool_attempt_count']} failed, {result['recovered_failures']} recovered"
            elif "recovered_timeouts" in result:
                label += f"\n{result['injected_timeouts']} timeouts/{result['recovered_timeouts']} recovered"
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
    task_selection.add_argument(
        "--pilot25", action="store_true", help="Use the 25-task sample with 11–20 effective calls"
    )
    parser.add_argument("--limit", type=int, help="Select the first N tasks from a pilot manifest")
    parser.add_argument(
        "--resume", action="store_true", help="Save each task and arm and reuse completed runs"
    )
    parser.add_argument(
        "--include-native", action="store_true", help="Also run the original tools without variants"
    )
    parser.add_argument(
        "--native-pass-only",
        action="store_true",
        help="Run Native first and variants only for tasks with a full Native score",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Report directory (reuse with --resume)"
    )
    parser.add_argument(
        "--arm-timeout", type=int, help="Seconds allowed per arm (pilots: 3600, otherwise: 300)"
    )
    parser.add_argument(
        "--max-steps", type=int, help="Model turns per task (pilots: 64, otherwise: 16)"
    )
    parser.add_argument("--max-concurrent", type=int, default=2)
    token_budget = parser.add_mutually_exclusive_group()
    token_budget.add_argument(
        "--max-task-tokens",
        type=int,
        default=DEFAULT_MAX_TASK_TOKENS,
        help="Cumulative model tokens per task in each arm (default: 400000)",
    )
    token_budget.add_argument(
        "--no-task-token-cap",
        action="store_const",
        const=None,
        dest="max_task_tokens",
        help="Disable the cumulative per-task token threshold",
    )
    parser.add_argument("--variant-seed", type=int, default=0)
    parser.add_argument(
        "--variant-scenario",
        choices=("cost-speed", "budget-speed", MULTI_OBJECTIVE),
        help="Use multi-objective for cost, speed and stability preference arms",
    )
    parser.add_argument(
        "--budget", type=int, help="Tool cost ceiling stated in the variant prompts"
    )
    args = parser.parse_args()
    pilot = args.pilot50 or args.pilot25
    if args.limit is not None and not pilot:
        parser.error("--limit requires --pilot50 or --pilot25")
    if args.pilot25 and args.variant_scenario is None and args.budget is None:
        args.variant_scenario = MULTI_OBJECTIVE
    if args.arm_timeout is None:
        args.arm_timeout = 3600 if pilot else 300
    if args.max_steps is None:
        args.max_steps = 64 if pilot else 16
    if (
        args.arm_timeout < 1
        or args.max_steps < 1
        or args.max_concurrent < 1
        or (args.max_task_tokens is not None and args.max_task_tokens < 1)
        or (args.budget is not None and args.budget < 1)
    ):
        parser.error("timeout, step limit, concurrency and token budgets must be positive")
    if args.output_dir.exists() and not args.resume:
        parser.error("--output-dir must be a new directory")
    if pilot:
        try:
            tasks = (
                load_pilot50(args.limit)
                if args.pilot50
                else load_pilot(PILOT25_PATH, 25, args.limit)
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(f"cannot load pilot tasks: {exc}")
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
    try:
        report = asyncio.run(
            run_pair(
                args,
                tasks,
                domains,
                include_native=args.include_native or args.variant_scenario != MULTI_OBJECTIVE,
                max_task_tokens=args.max_task_tokens,
                variant_scenario=args.variant_scenario,
            )
        )
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    print_report(report)
    print(f"Comparison saved to {args.output_dir / 'comparison.json'}")
    if report["summary"]["with_incomplete_arm"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
