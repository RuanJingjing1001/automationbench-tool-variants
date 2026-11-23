# SPDX-License-Identifier: MIT

import asyncio
import json
from types import SimpleNamespace

from automationbench.scripts import paired_eval
from automationbench.scripts.paired_eval import compare_runs, load_pilot50
from automationbench.tool_variants import MULTI_OBJECTIVE, variants_for


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
    assert pair["score_deltas"] == {"speed_minus_cost": -1.0}
    assert missing["speed"]["status"] == "missing"
    assert missing["score_deltas"] == {}
    assert report["summary"]["by_arm"]["speed"] == {
        "scored_tasks": 1,
        "avg_score": 0.0,
        "strict_pass_count": 0,
        "strict_pass_rate": 0.0,
    }


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
        elif kwargs["tool_preference"] == "cost":
            output["tasks"][0].update(
                stop_condition="token_budget_exceeded",
                passed=False,
                score=0.5,
                input_tokens=400_000,
                output_tokens=1_000,
            )
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
            args,
            first_five,
            ["hr", "finance", "operations", "marketing"],
            include_native=True,
            max_task_tokens=400_000,
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
    assert calls[0]["max_task_tokens"] == report["max_task_tokens"] == 400_000
    assert all(pair["native"] == {"status": "passed", "score": 1.0} for pair in report["tasks"])
    assert (args.output_dir / "native.json").is_file()
    assert report["summary"] == {
        "pairs": 5,
        "by_arm": {
            "native": {
                "scored_tasks": 5,
                "avg_score": 1.0,
                "strict_pass_count": 5,
                "strict_pass_rate": 1.0,
            },
            "cost": {
                "scored_tasks": 5,
                "avg_score": 0.9,
                "strict_pass_count": 4,
                "strict_pass_rate": 0.8,
            },
            "speed": {
                "scored_tasks": 5,
                "avg_score": 1.0,
                "strict_pass_count": 5,
                "strict_pass_rate": 1.0,
            },
        },
        "both_passed": 4,
        "with_tool_switch": 4,
        "with_incomplete_arm": 0,
        "token_budget_exceeded_by_arm": {"native": 0, "cost": 1, "speed": 0},
    }
    capped = report["tasks"][0]
    assert capped["cost"]["status"] == "token_budget_exceeded"
    assert capped["cost"]["model_tokens"] == 401_000
    assert capped["score_deltas"] == {
        "cost_minus_native": -0.5,
        "speed_minus_native": 0.0,
        "speed_minus_cost": 0.5,
    }
    assert "latency_saved_ms" not in capped
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


def test_multi_objective_paired_run_reports_three_preferences(monkeypatch, tmp_path):
    args = SimpleNamespace(
        output_dir=tmp_path / "multi",
        budget=None,
        model="test-model",
        base_url=None,
        api="chat_completions",
        variant_seed=7,
        max_steps=64,
        max_concurrent=1,
        arm_timeout=3600,
    )
    calls = []

    async def evaluate(**kwargs):
        from pathlib import Path

        calls.append(kwargs)
        task = make_task("t", True, 3, 1300, [("find", "find_economy"), ("find", "find_reliable")])
        variant = task["tool_variants"]
        variant.update(scenario=MULTI_OBJECTIVE, injected_timeouts=1)
        variant["attempts"][-1]["recovered_after_timeout"] = True
        Path(kwargs["export_json"]).write_text(json.dumps({"tasks": [task]}))

    monkeypatch.setattr(paired_eval, "run_evaluation", evaluate)
    report = asyncio.run(
        paired_eval.run_pair(args, ("t",), ["finance"], variant_scenario=MULTI_OBJECTIVE)
    )
    assert [call["tool_preference"] for call in calls] == ["cost", "speed", "stability"]
    assert all(call["toolset"] == "variants" and call["variant_seed"] == 7 for call in calls)
    assert report["variant_seed"] == 7
    assert report["variant_profile_scope"] == "all-actions"
    assert report["tasks"][0]["stability"]["recovered_timeouts"] == 1
    assert report["summary"]["by_arm"]["stability"]["total_simulated_latency_ms"] == 1300
    assert report["summary"]["by_arm"]["stability"]["strict_pass_rate"] == 1.0
