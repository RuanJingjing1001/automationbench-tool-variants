# SPDX-License-Identifier: MIT

import asyncio
import json
from types import SimpleNamespace

from automationbench.scripts import paired_eval
from automationbench.scripts.paired_eval import compare_runs, load_pilot50
from automationbench.tool_variants import variants_for


def make_task(name, passed, cost, latency, tools):
    return {
        "name": name,
        "passed": passed,
        "score": float(passed),
        "tool_variants": {
            "total_cost_units": cost,
            "total_simulated_latency_ms": latency,
            "injected_timeouts": 0,
            "attempts": [{"action": action, "tool": tool} for action, tool in tools],
        },
    }


def test_pair_preserves_failure_and_separates_actions_from_tool_switches():
    cost = make_task("t", True, 2, 600, [("find", "find_economy"), ("write", "write_economy")])
    speed = make_task("t", False, 3, 100, [("find", "find_fast")])
    report = compare_runs(
        ("t", "missing"), {"cost": {"tasks": [cost]}, "speed": {"tasks": [speed]}}, 2
    )
    pair, missing = report["tasks"]
    assert pair["speed"]["status"] == "failed"
    assert pair["speed"]["within_budget"] is False
    assert pair["cost"]["variant_attempt_counts"] == {"economy": 2}
    assert pair["speed"]["variant_attempt_counts"] == {"fast": 1}
    assert pair["cost_saved"] == 1
    assert pair["latency_saved_ms"] == 500
    assert [change["action"] for change in pair["tool_switches"]] == ["find"]
    assert pair["cost_only_actions"] == ["write"]
    assert missing["speed"]["status"] == "missing"


def test_pilot50_runs_the_same_first_five_tasks_in_all_three_arms(monkeypatch, tmp_path):
    tasks = load_pilot50()
    first_five = load_pilot50(5)
    assert len(set(tasks)) == 50
    assert first_five == tasks[:5]
    assert first_five[0] == "hr.monday_succession_planning"

    calls = []

    async def evaluate(**kwargs):
        from pathlib import Path

        path = Path(kwargs.pop("export_json"))
        calls.append(kwargs)
        fast = kwargs["tool_preference"] == "speed"
        suffix, cost, latency = ("fast", 3, 100) if fast else ("economy", 1, 300)
        output = {
            "tasks": [
                make_task(name, True, cost, latency, [("find", f"find_{suffix}")])
                for name in first_five
            ]
        }
        if kwargs["toolset"] == "limited_zapier":
            for task in output["tasks"]:
                task.pop("tool_variants")
        path.write_text(json.dumps(output))

    monkeypatch.setattr(paired_eval, "run_evaluation", evaluate)
    args = SimpleNamespace(
        output_dir=tmp_path / "pilot",
        model="test-model",
        base_url=None,
        api="chat_completions",
        budget=None,
        variant_seed=0,
        max_steps=64,
        max_concurrent=2,
        arm_timeout=3600,
    )
    report = asyncio.run(
        paired_eval.run_pair(
            args, first_five, ["hr", "finance", "operations", "marketing"], include_native=True
        )
    )
    assert [(call["tool_preference"], call["tasks"]) for call in calls] == [
        ("neutral", ",".join(first_five)),
        ("cost", ",".join(first_five)),
        ("speed", ",".join(first_five)),
    ]
    assert [call.pop("toolset") for call in calls] == ["limited_zapier", "variants", "variants"]
    for call in calls:
        call.pop("tool_preference")
    assert calls[0] == calls[1] == calls[2]
    assert all(pair["native"] == {"status": "passed", "score": 1.0} for pair in report["tasks"])
    assert (args.output_dir / "native.json").is_file()
    assert report["summary"] == {
        "pairs": 5,
        "both_passed": 5,
        "with_tool_switch": 5,
        "with_incomplete_arm": 0,
    }
    assert (args.output_dir / "comparison.json").is_file()


def test_budget_scenario_gives_same_premium_different_latency_savings():
    def gmail_find_email():
        return "email"

    def salesforce_contact_update():
        return "updated"

    email = variants_for(gmail_find_email, "budget-speed")
    contact = variants_for(salesforce_contact_update, "budget-speed")
    assert [tool.__name__ for tool in email] == [
        "gmail_find_email_economy",
        "gmail_find_email_fast",
    ]
    assert "1 cost units, 700 ms" in email[0].__doc__
    assert "1 cost units, 150 ms" in contact[0].__doc__
    assert (
        "3 cost units, 100 ms" in email[1].__doc__ and "3 cost units, 100 ms" in contact[1].__doc__
    )
