# SPDX-License-Identifier: MIT

"""Phase-one variants of existing Zapier actions."""

from __future__ import annotations

import functools
import inspect
import json
import random
from collections import Counter
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Profile:
    suffix: str
    cost_units: int
    latency_ms: int
    timeout_probability: float = 0.0
    max_rows: int | None = None
    failure_probability: float | None = None

    def describe(self, *, offline: bool = False) -> str:
        coverage = (
            "Standard results."
            if self.max_rows is None
            else f"Returns at most {self.max_rows} rows per call."
        )
        if offline and self.failure_probability is not None:
            return (
                f"Per attempt: {self.cost_units} cost units and {self.latency_ms} ms simulated "
                f"latency. Modeled failure probability: {self.failure_probability * 100:.8g}%. "
                f"Used to score trace reliability offline; no failures are injected. {coverage}"
            )
        if self.failure_probability is not None:
            return (
                f"Per attempt: {self.cost_units} cost units and {self.latency_ms} ms simulated "
                f"latency. Retryable pre-execution failure probability: "
                f"{self.failure_probability:.0%}. Failed attempts have the same cost and latency. "
                f"{coverage}"
            )
        return (
            f"Per attempt: {self.cost_units} cost units, {self.latency_ms} ms simulated "
            f"latency, {self.timeout_probability:.0%} injected timeout probability. {coverage}"
        )


ECONOMY = Profile("economy", 1, 300)
MULTI_OBJECTIVE = "multi-objective"
MULTI_OBJECTIVE_PROFILE_SCOPE = "all-actions"
MULTI_OBJECTIVE_FAULT_MODEL = "pre-execution-failure"
OFFLINE_RELIABILITY = "offline-reliability"
_OFFLINE_CONFIG_BYTES = Path(__file__).with_name("offline_profiles.json").read_bytes()
OFFLINE_PROFILE_CONFIG_SHA256 = sha256(_OFFLINE_CONFIG_BYTES).hexdigest()
_OFFLINE_CONFIG = json.loads(_OFFLINE_CONFIG_BYTES)


def _offline_profiles(
    *, cost_units: int, latency_ms: int, failure_probability: float
) -> tuple[Profile, ...]:
    if type(cost_units) is not int or cost_units <= 0:
        raise ValueError("offline profile cost_units must be a positive integer")
    if type(latency_ms) is not int or latency_ms <= 0 or latency_ms % 3:
        raise ValueError("offline profile latency_ms must be a positive multiple of three")
    if type(failure_probability) not in (int, float) or not 0 <= failure_probability < 1:
        raise ValueError("offline profile failure_probability must be in [0, 1)")
    return (
        Profile("economy", cost_units, latency_ms, failure_probability=failure_probability),
        Profile("fast", 2 * cost_units, latency_ms // 3, failure_probability=failure_probability),
        Profile(
            "reliable", 2 * cost_units, latency_ms, failure_probability=failure_probability / 10
        ),
    )


_OFFLINE_ACTION_PROFILES = {
    action: _offline_profiles(**(_OFFLINE_CONFIG["default"] | overrides))
    for action, overrides in _OFFLINE_CONFIG["actions"].items()
}
SCENARIOS = {
    "cost-speed": (ECONOMY, Profile("fast", 3, 100)),
    "budget-speed": (ECONOMY, Profile("fast", 3, 100)),
    "stability": (
        replace(ECONOMY, timeout_probability=0.10),
        Profile("reliable", 3, 300, timeout_probability=0.01),
    ),
    "quality": (ECONOMY, Profile("standard", 3, 300)),
    MULTI_OBJECTIVE: (
        Profile("economy", 1, 300, failure_probability=0.10),
        Profile("fast", 2, 100, failure_probability=0.10),
        Profile("reliable", 2, 300, failure_probability=0.01),
    ),
    OFFLINE_RELIABILITY: _offline_profiles(**_OFFLINE_CONFIG["default"]),
}
# Action-specific latency gains for the budget pilot.
BUDGET_LATENCIES = {
    "gmail_find_email": (700, 100),
    "salesforce_find_records": (350, 100),
    "salesforce_contact_update": (150, 100),
    "google_sheets_get_many_rows": (700, 100),
    "trello_card_update": (350, 100),
    "trello_card_label": (150, 100),
    "basecamp3_todo": (350, 100),
}
QUALITY_ROW_CAPS = {"google_sheets_find_many_rows": 5}

PREFERENCES = {
    "neutral": "",
    "cost": "Complete the task correctly while minimizing total tool cost units.",
    "speed": "Complete the task correctly while minimizing total simulated tool latency.",
    "stability": "Prioritize completing the task with as few failed tool calls as possible.",
    "quality": "Prioritize complete and correct results even if tools cost more.",
}


def preference_for(preference: str, scenario: str) -> str:
    if preference == "stability" and scenario == OFFLINE_RELIABILITY:
        return (
            "Complete the task correctly while maximizing the trace's modeled probability "
            "that every tool call succeeds without a transient failure. Multiply (1 - p) "
            "over eligible calls using the published failure probabilities."
        )
    return PREFERENCES[preference]


@dataclass
class VariantExecution:
    allowed_names: frozenset[str]
    random: random.Random
    report: dict
    example_id: int | None = None
    attempts_by_call: Counter[str] = field(default_factory=Counter)
    pending_failures: set[str] = field(default_factory=set)

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
                **(
                    {"fault_model": MULTI_OBJECTIVE_FAULT_MODEL, "injected_failures": 0}
                    if scenario == MULTI_OBJECTIVE
                    else {}
                ),
                **(
                    {
                        "profile_config_sha256": OFFLINE_PROFILE_CONFIG_SHA256,
                        "trace_reliability": 1.0,
                        "reliability_call_count": 0,
                    }
                    if scenario == OFFLINE_RELIABILITY
                    else {}
                ),
                "attempts": [],
            },
            example_id=example_id,
        )

    def fault_draw(self, call_id: str) -> tuple[int, float]:
        self.attempts_by_call[call_id] += 1
        attempt = self.attempts_by_call[call_id]
        key = f"{self.report['seed']}:{self.example_id}:{call_id}:{attempt}"
        return attempt, random.Random(key).random()


def profiles_for_action(action: str, scenario: str) -> tuple[Profile, ...]:
    profiles = SCENARIOS[scenario]
    if scenario == OFFLINE_RELIABILITY:
        return _OFFLINE_ACTION_PROFILES.get(action, profiles)
    if scenario == "quality" and action in QUALITY_ROW_CAPS:
        economy, standard = profiles
        profiles = (replace(economy, max_rows=QUALITY_ROW_CAPS[action]), standard)
    if scenario == "budget-speed" and action in BUDGET_LATENCIES:
        profiles = tuple(
            replace(profile, latency_ms=latency)
            for profile, latency in zip(profiles, BUDGET_LATENCIES[action], strict=True)
        )
    return profiles


def variants_for(tool: Callable, scenario: str) -> list[Callable]:
    """Give a business action independent callable names and the original input schema."""
    return [
        _wrap(tool, profile, offline=scenario == OFFLINE_RELIABILITY)
        for profile in profiles_for_action(tool.__name__, scenario)
    ]


def _tool_call_id(action: str, arguments: dict) -> str:
    # Hash resolved arguments, omitting mutable world state and collapsing input aliases.
    inputs = {name: value for name, value in arguments.items() if name != "world"}
    if action in ("google_sheets_get_many_rows", "google_sheets_find_many_rows"):
        sheet_alias = inputs.pop("spreadsheet_id")
        worksheet_alias = inputs.pop("worksheet_id")
        worksheet_title = inputs.pop("worksheet_title", None)
        inputs["spreadsheet"] = inputs["spreadsheet"] or sheet_alias or ""
        inputs["worksheet"] = inputs["worksheet"] or worksheet_alias or worksheet_title or ""
    payload = json.dumps([action, inputs], sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode()).hexdigest()


def _reported_success(result: str) -> bool:
    # Recovery metrics must not interfere with the original tool result.
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("success") is True


def _eligible_for_reliability(result: str) -> bool:
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return False
    if isinstance(payload, dict):
        return (
            payload.get("success") is not False
            and payload.get("status") != "error"
            and not payload.get("error")
            and not payload.get("errors")
        )
    return isinstance(payload, list)


def _wrap(tool: Callable, profile: Profile, *, offline: bool = False) -> Callable:
    action = tool.__name__
    name = f"{action}_{profile.suffix}"
    offline_probability = profile.failure_probability if offline else None
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
        call_id = None
        report["total_simulated_latency_ms"] += profile.latency_ms
        if offline_probability is not None:
            attempt.pop("timeout_probability")
            attempt["failure_probability"] = offline_probability
            attempt["included_in_reliability"] = False
            result = tool(*args, **kwargs)
            attempt["status"] = "returned"
            if _eligible_for_reliability(result):
                attempt["included_in_reliability"] = True
                report["trace_reliability"] *= 1 - offline_probability
                report["reliability_call_count"] += 1
            return result
        if profile.failure_probability is None:
            if execution.random.random() < profile.timeout_probability:
                attempt["status"] = "injected_timeout"
                report["injected_timeouts"] += 1
                return json.dumps({"error": "Simulated tool timeout", "retryable": True})
        else:
            attempt.pop("timeout_probability")
            attempt["failure_probability"] = profile.failure_probability
            bound = original_signature.bind(*args, **kwargs)
            bound.apply_defaults()
            call_id = _tool_call_id(action, bound.arguments)
            fault_attempt, draw = execution.fault_draw(call_id)
            attempt.update(fault_call_id=call_id, fault_attempt=fault_attempt)
            if draw < profile.failure_probability:
                attempt["status"] = "injected_failure"
                report["injected_failures"] += 1
                execution.pending_failures.add(call_id)
                return json.dumps({"error": "Simulated tool failure", "retryable": True})

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
        if call_id in execution.pending_failures and _reported_success(result):
            execution.pending_failures.remove(call_id)
            attempt["recovered_after_failure"] = True
        return result

    variant.__name__ = name
    variant.__qualname__ = name
    # Start with a separate paragraph so the original Args section remains intact.
    description = inspect.getdoc(tool) or ""
    variant.__doc__ = f"{profile.describe(offline=offline)}\n\n{description}"
    variant.__signature__ = original_signature.replace(parameters=parameters)
    return variant
