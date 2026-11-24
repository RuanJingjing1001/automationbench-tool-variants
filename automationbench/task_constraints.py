# SPDX-License-Identifier: MIT

"""Task-level constraints for the offline reliability experiment."""

import json
from pathlib import Path
from typing import TypedDict

from automationbench.tool_variants import OFFLINE_RELIABILITY, profiles_for_action


class TaskLimits(TypedDict):
    budget_units: int
    deadline_ms: int


DEADLINE_CONDITIONS = ("tight", "loose")


def load_task_constraints(path: Path) -> dict[str, dict[str, TaskLimits]]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not data:
        raise ValueError("task constraints must be a non-empty object keyed by task ID")
    for task, conditions in data.items():
        if not isinstance(conditions, dict) or set(conditions) != set(DEADLINE_CONDITIONS):
            raise ValueError(f"{task}: provide tight and loose conditions")
        for condition, limits in conditions.items():
            if (
                not isinstance(limits, dict)
                or set(limits) != {"budget_units", "deadline_ms"}
                or any(type(value) is not int or value < 1 for value in limits.values())
            ):
                raise ValueError(
                    f"{task}/{condition}: budget_units and deadline_ms must be positive integers"
                )
        tight, loose = (conditions[condition] for condition in DEADLINE_CONDITIONS)
        if tight["budget_units"] != loose["budget_units"]:
            raise ValueError(f"{task}: conditions must use the same budget")
        if tight["deadline_ms"] >= loose["deadline_ms"]:
            raise ValueError(f"{task}: tight deadline must be shorter than loose deadline")
    return data


def execution_requirements(actions: list[str], limits: TaskLimits) -> str:
    lines = [
        "## Execution requirements",
        "Complete all business requirements correctly.",
        f"Tool budget: {limits['budget_units']} cost units. "
        f"Cumulative simulated tool time limit: {limits['deadline_ms']} ms.",
        "Every tool attempt counts toward cost and simulated time, including business errors.",
        "Among plans that meet both limits, maximize the probability that the trajectory "
        "has no transient tool failures.",
        "Modeled failures are independent and used only for offline scoring. Multiply "
        "(1 - p) across all normally completed calls, including repeated calls and valid "
        "empty search results. Calls returning business errors are excluded from this product.",
        "",
        "| Available tool version | Cost units | Simulated time (ms) | Modeled failure probability |",
        "| --- | ---: | ---: | ---: |",
    ]
    for action in dict.fromkeys(actions):
        for profile in profiles_for_action(action, OFFLINE_RELIABILITY):
            probability = profile.failure_probability
            assert probability is not None
            lines.append(
                f"| `{action}_{profile.suffix}` | {profile.cost_units} | "
                f"{profile.latency_ms} | {probability * 100:.8g}% |"
            )
    return "\n".join(lines)
