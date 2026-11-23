"""Rollout-local retry guard and its on-demand recovery instruction."""

import json
import re
from dataclasses import dataclass, field

RECOVERY_INSTRUCTION = (
    "This lookup is paused after repeated not-found errors. Stop guessing IDs. "
    "Use a different available search, list, or discovery tool to locate the resource "
    "by name or business fields and obtain its exact ID before retrying. "
    "If there is no valid way to locate it, report the missing resource."
)


def lookup_outcome(content: object) -> str:
    if not isinstance(content, str):
        return "unknown"
    try:
        result = json.loads(content)
    except ValueError:
        return "unknown"
    if not isinstance(result, dict):
        return "unknown"
    error = result.get("error")
    if error:
        if (
            result.get("retryable") is not True
            and isinstance(error, str)
            and re.search(r"\bnot found\b", error, re.IGNORECASE)
        ):
            return "not_found"
        return "other_error"
    return "other_error" if result.get("success") is False else "success"


@dataclass
class LookupRecovery:
    threshold: int
    action: str | None = None
    streak: int = 0
    paused_actions: set[str] = field(default_factory=set)
    report: dict = field(
        default_factory=lambda: {
            "not_found_calls": 0,
            "repeated_not_found_calls": 0,
            "blocked_calls": 0,
            "triggers": [],
        }
    )

    def observe(self, action: str, content: object, call_id: str) -> bool:
        outcome = lookup_outcome(content)
        if outcome == "success":
            self.paused_actions.clear()
        if outcome != "not_found":
            self.action, self.streak = None, 0
            return False
        self.report["not_found_calls"] += 1
        if action == self.action:
            self.streak += 1
            self.report["repeated_not_found_calls"] += 1
        else:
            self.action, self.streak = action, 1
        if self.threshold and self.streak == self.threshold:
            self.paused_actions.add(action)
            self.report["triggers"].append({"action": action, "tool_call_id": call_id})
            return True
        return False
