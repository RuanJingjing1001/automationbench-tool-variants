"""Check the trigger and same-path attempts in an existing, unguarded trace."""

import argparse
import json
from pathlib import Path

from automationbench.lookup_recovery import LookupRecovery, lookup_outcome


def audit(task: dict, threshold: int) -> dict:
    baseline = LookupRecovery(0)
    guard = LookupRecovery(threshold)
    variants = {
        attempt["tool"]: attempt["action"]
        for attempt in (task.get("tool_variants") or {}).get("attempts", [])
    }
    calls = {}
    responses = 0
    first_trigger = None
    blocked_not_found = 0
    blocked_other = 0
    for message in task["messages"]:
        if message["role"] == "assistant":
            for call in message.get("tool_calls") or []:
                if isinstance(call, str):
                    call = json.loads(call)
                name = call.get("name") or call["function"]["name"]
                calls[call["id"]] = variants.get(name, name)
        elif message["role"] == "tool" and message.get("tool_call_id") in calls:
            call_id = message["tool_call_id"]
            action = calls.pop(call_id)
            content = message.get("content")
            responses += 1
            baseline.observe(action, content, call_id)
            if action in guard.paused_actions:
                if lookup_outcome(content) == "not_found":
                    blocked_not_found += 1
                else:
                    blocked_other += 1
            elif guard.observe(action, content, call_id) and first_trigger is None:
                first_trigger = {"tool_response": responses, "action": action}

    return {
        "task": task["name"],
        "score": task["score"],
        "recorded_tool_responses": responses,
        "repeated_not_found_calls": baseline.report["repeated_not_found_calls"],
        "first_trigger": first_trigger,
        "same_trace_blocked_not_found": blocked_not_found,
        "same_trace_blocked_other": blocked_other,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--threshold", type=int, default=3)
    args = parser.parse_args()
    if args.threshold < 1:
        parser.error("--threshold must be positive")
    trace = json.loads(args.trace.read_text())
    if trace.get("meta", {}).get("lookup_retry_limit", 0):
        parser.error("provide a trace recorded with the guard disabled")
    print(json.dumps([audit(task, args.threshold) for task in trace["tasks"]], indent=2))


if __name__ == "__main__":
    main()
