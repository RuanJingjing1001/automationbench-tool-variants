# SPDX-License-Identifier: MIT

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from automationbench.scripts import eval as benchmark_eval, paired_eval
from automationbench.scripts.paired_eval import compare_runs, load_pilot50
from automationbench.task_constraints import load_task_constraints
from automationbench.tool_variants import MULTI_OBJECTIVE, OFFLINE_RELIABILITY, variants_for


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


def test_offline_reliability_comparison_requires_both_traces_to_qualify():
    cost = make_task("t", True, 1, 600, [("find", "find_economy")])
    speed = make_task("t", False, 2, 200, [("find", "find_fast")])
    stability = make_task("t", True, 2, 600, [("find", "find_reliable")])
    for task, reliability in ((cost, 0.9), (speed, 0.9), (stability, 0.99)):
        task["tool_variants"].update(trace_reliability=reliability, reliability_call_count=1)
    runs = {
        arm: {"tasks": [task]}
        for arm, task in (("cost", cost), ("speed", speed), ("stability", stability))
    }
    report = compare_runs(("t",), runs, budget=2)
    pair = report["tasks"][0]
    assert pair["speed"]["trace_reliability"] == 0.9
    assert "latency_saved_ms" not in pair
    assert set(pair["tool_metric_deltas"]) == {"stability_minus_cost"}
    assert pair["tool_metric_deltas"]["stability_minus_cost"]["trace_reliability"] == pytest.approx(
        0.09
    )
    assert report["summary"]["by_arm"]["speed"]["avg_trace_reliability"] is None
    assert report["summary"]["by_arm"]["stability"]["avg_trace_reliability"] == 0.99

    report = compare_runs(("t",), runs, budget=1)
    assert report["tasks"][0]["tool_metric_deltas"] == {}
    assert report["summary"]["by_arm"]["stability"]["reliability_scored_tasks"] == 0


@pytest.mark.parametrize(
    ("score", "cost", "latency", "expected"),
    [
        (1.0, 27, 14800, True),
        (1.0, 28, 14800, False),
        (1.0, 27, 14801, False),
        (0.5, 26, 14799, False),
    ],
)
def test_deadline_comparison_keeps_business_score_and_requires_both_limits(
    score,
    cost,
    latency,
    expected,
):
    name = "finance.qb_recurring_journal"
    constraints = load_task_constraints(
        paired_eval.PILOT25_PATH.with_name("quickbooks-task-constraints.json")
    )
    tight = make_task(name, score == 1.0, cost, latency, [("read", "read_fast")])
    tight["score"] = score
    loose = make_task(name, True, 27, 15600, [("read", "read_reliable")])
    for task, reliability in ((tight, 0.9), (loose, 0.99)):
        task["tool_variants"].update(trace_reliability=reliability, reliability_call_count=1)
    report = compare_runs(
        (name,),
        {"tight": {"tasks": [tight]}, "loose": {"tasks": [loose]}},
        None,
        task_constraints=constraints,
    )
    pair = report["tasks"][0]
    assert pair["tight"]["score"] == score
    assert pair["tight"]["trace_reliability"] == 0.9
    assert pair["tight"]["within_budget"] == (cost <= 27)
    assert pair["tight"]["within_time_limit"] == (latency <= 14800)
    assert pair["tight"]["passed_within_constraints"] is expected
    assert pair["loose"]["passed_within_constraints"] is True
    assert report["summary"]["both_passed_within_constraints"] == int(expected)
    if expected:
        assert pair["tool_metric_deltas"]["loose_minus_tight"][
            "trace_reliability"
        ] == pytest.approx(0.09)
    else:
        assert "tool_metric_deltas" not in pair
        assert report["summary"]["by_arm"]["tight"]["avg_trace_reliability"] is None


async def test_deadline_pair_resumes_from_matching_constraints(monkeypatch, tmp_path):
    name = "finance.qb_recurring_journal"
    constraints = load_task_constraints(
        paired_eval.PILOT25_PATH.with_name("quickbooks-task-constraints.json")
    )
    args = SimpleNamespace(
        output_dir=tmp_path / "deadlines",
        budget=None,
        model="test-model",
        base_url=None,
        api="chat_completions",
        variant_seed=0,
        max_steps=64,
        max_concurrent=1,
        arm_timeout=10,
        resume=True,
    )
    calls = []

    async def evaluate(**kwargs):
        calls.append(kwargs)
        limits = kwargs["task_constraints"][name]
        task = make_task(name, True, 27, limits["deadline_ms"], [("read", "read_reliable")])
        task["tool_variants"].update(trace_reliability=0.99, reliability_call_count=1)
        Path(kwargs["export_json"]).write_text(json.dumps({"tasks": [task]}))

    monkeypatch.setattr(paired_eval, "run_evaluation", evaluate)
    for _ in range(2):
        report = await paired_eval.run_pair(
            args,
            (name,),
            ["finance"],
            variant_scenario=OFFLINE_RELIABILITY,
            task_constraints=constraints,
        )
        assert report["summary"]["both_passed_within_constraints"] == 1
    assert [call["task_constraints"][name]["deadline_ms"] for call in calls] == [14800, 15600]
    assert all(
        call["tool_preference"] == "neutral" and call["tool_budget"] is None for call in calls
    )
    constraints[name]["loose"]["deadline_ms"] += 100
    with pytest.raises(ValueError, match="resume configuration changed: task_constraints"):
        await paired_eval.run_pair(
            args,
            (name,),
            ["finance"],
            variant_scenario=OFFLINE_RELIABILITY,
            task_constraints=constraints,
        )

    constraints[name]["loose"]["deadline_ms"] -= 100
    manifest = args.output_dir / "experiment.json"
    old_experiment = json.loads(manifest.read_text())
    del old_experiment["config"]["execution_requirements_version"]
    manifest.write_text(json.dumps(old_experiment))
    with pytest.raises(
        ValueError, match="resume configuration changed: execution_requirements_version"
    ):
        await paired_eval.run_pair(
            args,
            (name,),
            ["finance"],
            variant_scenario=OFFLINE_RELIABILITY,
            task_constraints=constraints,
        )


@pytest.mark.parametrize("deadline_ms", [14800, 15600])
async def test_deadline_prompt_appends_simulated_date_limits_and_tool_menu(
    monkeypatch, deadline_ms
):
    name = "finance.qb_recurring_journal"
    rows = benchmark_eval.get_combined_dataset(["finance"])
    original = next(row for row in rows if benchmark_eval._task_name(row) == name)

    class PromptVerified(Exception):
        pass

    def inspect_dataset(*, dataset, **_):
        (prepared,) = dataset
        assert prepared["info"] == original["info"]
        assert prepared["prompt"][0] == original["prompt"][0]
        user_message = prepared["prompt"][1]["content"]
        assert user_message.startswith(
            original["prompt"][1]["content"] + "\n\n## Execution requirements"
        )
        assert "The simulated date is 2026-02-01." in user_message
        assert "relative dates and periods such as 'this month'." in user_message
        assert "Tool budget: 27 cost units." in user_message
        assert f"time limit: {deadline_ms} ms." in user_message
        assert "| `google_sheets_get_many_rows_fast` | 2 | 400 | 2% |" in user_message
        assert sum(line.startswith("| `") for line in user_message.splitlines()) == 24
        raise PromptVerified

    monkeypatch.setattr(benchmark_eval, "AutomationBenchEnv", inspect_dataset)
    with pytest.raises(PromptVerified):
        await benchmark_eval.run_evaluation(
            model="test-model",
            domains=["finance"],
            tasks=name,
            toolset="variants",
            variant_scenario=OFFLINE_RELIABILITY,
            task_constraints={name: {"budget_units": 27, "deadline_ms": deadline_ms}},
        )


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
        variant.update(scenario=MULTI_OBJECTIVE, injected_failures=1)
        variant["attempts"][-1]["recovered_after_failure"] = True
        Path(kwargs["export_json"]).write_text(json.dumps({"tasks": [task]}))

    monkeypatch.setattr(paired_eval, "run_evaluation", evaluate)
    report = asyncio.run(
        paired_eval.run_pair(args, ("t",), ["finance"], variant_scenario=MULTI_OBJECTIVE)
    )
    assert [call["tool_preference"] for call in calls] == ["cost", "speed", "stability"]
    assert all(call["toolset"] == "variants" and call["variant_seed"] == 7 for call in calls)
    assert report["variant_seed"] == 7
    assert report["variant_profile_scope"] == "all-actions"
    assert report["tasks"][0]["stability"]["recovered_failures"] == 1
    assert report["summary"]["by_arm"]["stability"]["total_simulated_latency_ms"] == 1300
    assert report["summary"]["by_arm"]["stability"]["strict_pass_rate"] == 1.0


def test_resume_adds_tasks_and_only_retries_incomplete_arms(monkeypatch, tmp_path):
    pilot = paired_eval.load_pilot(paired_eval.PILOT25_PATH, 25, None)
    first = "support.gorgias_quality_audit"
    second = "finance.vendor_credit_application"
    assert first in pilot and second in pilot
    calls = []

    async def evaluate(**kwargs):
        name = kwargs["tasks"]
        arm = kwargs["tool_preference"]
        calls.append((name, arm))
        task = make_task(name, True, 1, 300, [("find", "find_economy")])
        if arm == "neutral":
            del task["tool_variants"]
        if name == first and arm == "speed":
            task.update(score=0.0, passed=False)
        if name == first and arm == "cost":
            task.update(score=0.5, passed=False, stop_condition="token_budget_exceeded")
        if name == first and arm == "stability" and calls.count((name, arm)) == 1:
            task.update(errors=["connection error"])
        Path(kwargs["export_json"]).write_text(json.dumps({"tasks": [task]}))

    monkeypatch.setattr(paired_eval, "run_evaluation", evaluate)
    args = SimpleNamespace(
        resume=True,
        output_dir=tmp_path / "pilot",
        model="test-model",
        base_url=None,
        api="chat_completions",
        budget=None,
        variant_seed=0,
        max_steps=64,
        max_concurrent=1,
        arm_timeout=3600,
    )

    def run(tasks):
        return asyncio.run(
            paired_eval.run_pair(
                args,
                tasks,
                ["support", "finance"],
                include_native=True,
                variant_scenario=MULTI_OBJECTIVE,
            )
        )

    assert run((first,))["summary"]["with_incomplete_arm"] == 1
    calls.clear()
    calls.append((first, "stability"))  # Allow the interrupted attempt to succeed on retry.
    report = run((second,))
    assert set(calls[1:]) == {
        (second, "neutral"),
        (second, "cost"),
        (second, "speed"),
        (first, "stability"),
        (second, "stability"),
    }
    assert len(calls[1:]) == 5
    assert report["summary"]["with_incomplete_arm"] == 0
    assert report["tasks"][0]["cost"]["status"] == "token_budget_exceeded"
    assert report["tasks"][0]["speed"]["status"] == "failed"
    assert report["tasks"][0]["native"]["status"] == "passed"
    assert [task["task"] for task in report["tasks"]] == [first, second]
    assert [t["id"] for t in json.loads((args.output_dir / "cost.json").read_text())["tasks"]] == [
        1,
        2,
    ]
    calls.clear()
    run((first, second))
    assert not calls
    args.variant_seed = 1
    with pytest.raises(ValueError, match="variant_seed"):
        run((first, second))
    assert not calls


@pytest.mark.parametrize("resume", [False, True])
def test_native_gate_runs_variants_only_after_a_valid_full_native_score(
    monkeypatch, tmp_path, resume
):
    eligible, low, interrupted = ("hr.eligible", "hr.low", "hr.interrupted")
    calls = []

    async def evaluate(**kwargs):
        names = kwargs["tasks"].split(",")
        preference = kwargs["tool_preference"]
        calls.append((preference, names))
        tasks = []
        for name in names:
            task = make_task(name, name != low, 1, 300, [])
            if preference == "neutral":
                del task["tool_variants"]
                if name == interrupted:
                    task["errors"] = ["connection error"]
            tasks.append(task)
        Path(kwargs["export_json"]).write_text(json.dumps({"tasks": tasks}))

    monkeypatch.setattr(paired_eval, "run_evaluation", evaluate)
    args = SimpleNamespace(
        output_dir=tmp_path / "run",
        model="test",
        base_url=None,
        api="chat_completions",
        budget=None,
        variant_seed=0,
        max_steps=64,
        max_concurrent=1,
        arm_timeout=60,
        native_pass_only=True,
        resume=resume,
    )
    report = asyncio.run(
        paired_eval.run_pair(
            args, (eligible, low, interrupted), ["hr"], variant_scenario=MULTI_OBJECTIVE
        )
    )
    assert [entry for entry in calls if entry[0] != "neutral"] == [
        (arm, [eligible]) for arm in ("cost", "speed", "stability")
    ]
    assert report["summary"]["native_eligible_tasks"] == [eligible]
    assert report["summary"]["with_incomplete_arm"] == 1
    assert report["tasks"][1]["cost"]["status"] == "skipped_native_gate"
    assert report["summary"]["by_arm"]["cost"]["scored_tasks"] == 1
