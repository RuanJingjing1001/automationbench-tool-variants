# SPDX-License-Identifier: MIT

from automationbench.scripts.paired_eval import compare_runs
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
    assert pair["cost_saved"] == 1
    assert pair["latency_saved_ms"] == 500
    assert [change["action"] for change in pair["tool_switches"]] == ["find"]
    assert pair["cost_only_actions"] == ["write"]
    assert missing["speed"]["status"] == "missing"


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
