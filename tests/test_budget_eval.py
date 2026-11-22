# SPDX-License-Identifier: MIT

from automationbench.scripts.budget_eval import (
    best_under_budget,
    build_plan,
    fixed_trace_optima,
    score_comparison,
)

CONTACT = ("gmail_find_email", "salesforce_find_records", "salesforce_contact_update")
TRELLO = (
    "google_sheets_get_many_rows",
    "trello_card_update",
    "trello_card_label",
    "basecamp3_todo",
)


def test_fixed_trace_uses_budget_where_it_saves_most_time():
    optima = fixed_trace_optima(CONTACT)
    assert best_under_budget(optima, 2) is None
    assert best_under_budget(optima, 5)["simulated_latency_ms"] == 600
    assert best_under_budget(optima, 5)["tools"][0] == "gmail_find_email_fast"
    assert best_under_budget(optima, 7)["simulated_latency_ms"] == 350


def test_screen_uses_distinct_actions_and_passed_references():
    def task(name, actions, passed=True):
        return {
            "name": name,
            "passed": passed,
            "tool_variants": {
                "attempts": [{"action": action, "status": "returned"} for action in actions],
            },
        }

    plan = build_plan(
        [
            {
                "tasks": [
                    task("contact", CONTACT),
                    task("trello", TRELLO),
                    task("pending", TRELLO, passed=False),
                    task("repeat", (*CONTACT, CONTACT[0])),
                    task("uniform", ("x", "y", "z", "w")),
                ]
            }
        ]
    )
    assert {name: entry["tier"] for name, entry in plan.items()} == {
        "contact": "control",
        "trello": "main",
        "pending": "pending",
        "repeat": "control",
        "uniform": "control",
    }


def test_gap_is_conditional_on_trace_and_feasibility():
    result = {
        "status": "passed",
        "score": 1.0,
        "within_budget": True,
        "cost_units": 5,
        "simulated_latency_ms": 1150,
        "attempts": [
            {"action": action, "tool": f"{action}_{suffix}"}
            for action, suffix in zip(CONTACT, ("economy", "economy", "fast"), strict=True)
        ],
    }
    cost, speed = score_comparison(
        {"t": {"reference_actions": CONTACT}},
        {
            "scenario": "budget-speed",
            "budget": 5,
            "tasks": [{"task": "t", "cost": result, "speed": {**result, "within_budget": False}}],
        },
    )
    assert cost["matches_reference_trace"]
    assert cost["optimality_gap_ms"] == 550
    assert speed["optimality_gap_ms"] is None
