# SPDX-License-Identifier: MIT

"""Phase-one variants of existing Zapier actions."""

from __future__ import annotations

import functools
import inspect
import json
import random
from dataclasses import dataclass, replace
from typing import Callable


@dataclass(frozen=True)
class Profile:
    suffix: str
    cost_units: int
    latency_ms: int
    timeout_probability: float = 0.0
    max_rows: int | None = None

    @property
    def description(self) -> str:
        coverage = (
            "Standard results."
            if self.max_rows is None
            else f"Returns at most {self.max_rows} rows per call."
        )
        return (
            f"Per attempt: {self.cost_units} cost units, {self.latency_ms} ms simulated "
            f"latency, {self.timeout_probability:.0%} injected timeout probability. {coverage}"
        )


ECONOMY = Profile("economy", 1, 300)
SCENARIOS = {
    "cost-speed": (ECONOMY, Profile("fast", 3, 100)),
    "budget-speed": (ECONOMY, Profile("fast", 3, 100)),
    "stability": (
        replace(ECONOMY, timeout_probability=0.10),
        Profile("reliable", 3, 300, timeout_probability=0.01),
    ),
    "quality": (ECONOMY, Profile("standard", 3, 300)),
}
# Only the contact-update pilot needs action-specific acceleration.
BUDGET_LATENCIES = {
    "gmail_find_email": (700, 100),
    "salesforce_find_records": (350, 100),
    "salesforce_contact_update": (150, 100),
}
QUALITY_ROW_CAPS = {"google_sheets_find_many_rows": 5}

PREFERENCES = {
    "neutral": "",
    "cost": "Complete the task correctly while minimizing total tool cost units.",
    "speed": "Complete the task correctly while minimizing total simulated tool latency.",
    "stability": "Prioritize completing the task with as few tool timeouts as possible.",
    "quality": "Prioritize complete and correct results even if tools cost more.",
}


@dataclass
class VariantExecution:
    allowed_names: frozenset[str]
    random: random.Random
    report: dict

    @classmethod
    def start(cls, allowed_names, scenario: str, seed: int, example_id: int | None):
        return cls(
            frozenset(allowed_names),
            random.Random(f"{seed}:{example_id}"),
            {
                "scenario": scenario,
                "seed": seed,
                "total_cost_units": 0,
                "total_simulated_latency_ms": 0,
                "injected_timeouts": 0,
                "attempts": [],
            },
        )


def variants_for(tool: Callable, scenario: str) -> list[Callable]:
    """Give a business action independent callable names and the original input schema."""
    profiles = SCENARIOS[scenario]
    if scenario == "quality" and tool.__name__ in QUALITY_ROW_CAPS:
        economy, standard = profiles
        profiles = (replace(economy, max_rows=QUALITY_ROW_CAPS[tool.__name__]), standard)
    if scenario == "budget-speed" and tool.__name__ in BUDGET_LATENCIES:
        profiles = tuple(
            replace(profile, latency_ms=latency)
            for profile, latency in zip(profiles, BUDGET_LATENCIES[tool.__name__], strict=True)
        )
    return [_wrap(tool, profile) for profile in profiles]


def _wrap(tool: Callable, profile: Profile) -> Callable:
    action = tool.__name__
    name = f"{action}_{profile.suffix}"
    original_signature = inspect.signature(tool)
    parameters = list(original_signature.parameters.values())
    context = inspect.Parameter("_variant_execution", inspect.Parameter.KEYWORD_ONLY)
    if parameters and parameters[-1].kind == inspect.Parameter.VAR_KEYWORD:
        parameters.insert(-1, context)
    else:
        parameters.append(context)

    @functools.wraps(tool)
    def variant(*args, _variant_execution: VariantExecution, **kwargs):
        execution = _variant_execution
        if name not in execution.allowed_names:
            return json.dumps({"error": "Tool is not available for this task."})

        report = execution.report
        attempts = report["attempts"]
        attempt = {
            "index": len(attempts) + 1,
            "action": action,
            "tool": name,
            "cost_units": profile.cost_units,
            "simulated_latency_ms": profile.latency_ms,
            "timeout_probability": profile.timeout_probability,
            "max_rows": profile.max_rows,
            "status": "raised_exception",
        }
        attempts.append(attempt)
        report["total_cost_units"] += profile.cost_units
        report["total_simulated_latency_ms"] += profile.latency_ms

        if execution.random.random() < profile.timeout_probability:
            attempt["status"] = "injected_timeout"
            report["injected_timeouts"] += 1
            return json.dumps({"error": "Simulated tool timeout", "retryable": True})

        if profile.max_rows is not None:
            # The Sheets action owns the limit and updates its paging metadata.
            bound = original_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            bound.arguments["row_count"] = max(
                0, min(bound.arguments["row_count"], profile.max_rows)
            )
            result = tool(*bound.args, **bound.kwargs)
        else:
            result = tool(*args, **kwargs)
        attempt["status"] = "returned"
        return result

    variant.__name__ = name
    variant.__qualname__ = name
    # Start with a separate paragraph so the original Args section remains intact.
    description = inspect.getdoc(tool) or ""
    variant.__doc__ = f"{profile.description}\n\n{description}"
    variant.__signature__ = original_signature.replace(parameters=parameters)
    return variant
