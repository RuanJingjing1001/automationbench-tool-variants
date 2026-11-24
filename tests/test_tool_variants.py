# SPDX-License-Identifier: MIT

import json
from datetime import datetime
from inspect import signature
from random import Random

import pytest
import verifiers as vf
from datasets import Dataset

from automationbench.domains import get_available_domains, get_combined_dataset
from automationbench.domains.finance.tasks import get_fin_qb_recurring_journal_task
from automationbench.domains.marketing.tasks import get_event_followup_personalization_task
from automationbench.domains.simple.tasks import get_simple_email_sf_contact_phone_update
from automationbench.domains.support.tasks import get_support_zendesk_sf_case_sync_task
from automationbench.rubric import create_rubric, partial_credit
from automationbench.runner import AutomationBenchEnv
from automationbench.schema.salesforce import base as salesforce_base
from automationbench.tool_variants import (
    MULTI_OBJECTIVE,
    OFFLINE_RELIABILITY,
    VariantExecution,
    _tool_call_id,
    profiles_for_action,
)
from automationbench.tools import ALL_TOOLS
from automationbench.tools.zapier.gmail.message import gmail_send_email
from automationbench.tools.zapier.google_sheets.row import (
    google_sheets_append_row,
    google_sheets_get_many_rows,
)


@pytest.fixture(scope="module")
def envs():
    return {
        toolset if scenario is None else scenario: AutomationBenchEnv(
            dataset=Dataset.from_list([{"prompt": "Test"}]),
            rubric=create_rubric(),
            toolset=toolset,
            variant_scenario=scenario or "cost-speed",
        )
        for toolset, scenario in (
            ("limited_zapier", None),
            ("variants", "cost-speed"),
            ("variants", "stability"),
            ("variants", "quality"),
            ("variants", MULTI_OBJECTIVE),
            ("variants", OFFLINE_RELIABILITY),
        )
    }


async def call(env, state, name, **arguments):
    message = vf.AssistantMessage(
        content=None,
        tool_calls=[vf.ToolCall(id="call", name=name, arguments=json.dumps(arguments))],
    )
    (result,) = await env.env_response([message], state)
    return json.loads(result.content)


@pytest.mark.parametrize(
    ("scenario", "suffixes"),
    [
        ("cost-speed", ("economy", "fast")),
        (MULTI_OBJECTIVE, ("economy", "fast", "reliable")),
        (OFFLINE_RELIABILITY, ("economy", "fast", "reliable")),
    ],
)
def test_each_action_has_provider_compatible_variants(envs, scenario, suffixes):
    base = {tool.name: tool for tool in envs["limited_zapier"].tool_defs}
    variants = {tool.name: tool for tool in envs[scenario].tool_defs}
    assert len(variants) == len(base) * len(suffixes) == len(ALL_TOOLS) * len(suffixes)
    for name, tool in base.items():
        for suffix in suffixes:
            variant = variants[f"{name}_{suffix}"]
            assert len(variant.name) <= 64
            expected = {k: v for k, v in tool.parameters.items() if k != "title"}
            assert {k: v for k, v in variant.parameters.items() if k != "title"} == expected


@pytest.mark.parametrize(
    ("tool_name", "parameter"),
    [
        ("hubspot_update_contact_economy", "properties"),
        ("google_sheets_add_row_economy", "cells"),
        ("google_sheets_append_row_economy", "cells"),
        ("google_sheets_update_row_economy", "cells"),
    ],
)
def test_open_tool_parameters_have_typed_schema_and_decode_json(envs, tool_name, parameter):
    env = envs["cost-speed"]
    tool = next(tool for tool in env.tool_defs if tool.name == tool_name)
    schema = tool.parameters["properties"][parameter]
    assert schema["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert "JSON-encoded string" in schema["description"]
    state = {"world": object(), "_variant_execution": object()}
    values = {"column_name": "new value"}
    assert (
        env.update_tool_args(tool_name, {parameter: json.dumps(values)}, [], state)[parameter]
        == values
    )
    assert env.update_tool_args(tool_name, {parameter: values}, [], state)[parameter] == values
    assert env.update_tool_args(tool_name, {parameter: None}, [], state)[parameter] is None


def test_task_allowlists_are_covered():
    names = {action.__name__ for action in ALL_TOOLS}
    for row in get_combined_dataset(get_available_domains()):
        info = row["info"]
        if isinstance(info, str):
            info = json.loads(info)
        assert set(info["zapier_tools"]) <= names, info["task_name"]


@pytest.mark.parametrize(
    ("action", "latency", "probability"),
    [("google_sheets_get_many_rows", 1200, 0.02), ("trello_card_update", 600, 0.02)],
)
def test_offline_profile_uses_action_overrides_or_defaults(action, latency, probability):
    economy, fast, reliable = profiles_for_action(action, OFFLINE_RELIABILITY)
    assert [v.cost_units for v in (economy, fast, reliable)] == [1, 2, 2]
    assert [v.latency_ms for v in (economy, fast, reliable)] == [latency, latency // 3, latency]
    assert [v.failure_probability for v in (economy, fast, reliable)] == pytest.approx(
        [probability, probability, probability / 10]
    )
    assert "0.2%" in reliable.describe(offline=True)


async def test_offline_reliability_uses_real_tool_results_without_sampling(envs, monkeypatch):
    def fail_on_draw(*_args):
        pytest.fail("offline reliability must not sample a tool fault")

    monkeypatch.setattr(VariantExecution, "fault_draw", fail_on_draw)
    monkeypatch.setattr(Random, "random", fail_on_draw)
    env = envs[OFFLINE_RELIABILITY]
    state = await env.setup_state(get_simple_email_sf_contact_phone_update())
    read = "gmail_find_email_fast"
    update = "salesforce_contact_update_reliable"
    for _ in range(2):
        result = await call(env, state, read, query='subject:"Updated phone number"')
        assert result["success"]
    error = await call(env, state, update, id="missing", phone="+1-555-0101")
    assert error["error"]
    success = await call(env, state, update, id="003001", phone="+1-555-0101")
    assert success["success"]
    assert partial_credit(state) == 1.0

    report = state["tool_variants"]
    assert [a["included_in_reliability"] for a in report["attempts"]] == [True, True, False, True]
    assert report["reliability_call_count"] == 3
    assert report["trace_reliability"] == pytest.approx(0.98**2 * 0.99)
    assert (report["total_cost_units"], report["total_simulated_latency_ms"]) == (8, 1200)


async def test_cost_speed_replay_keeps_original_results_and_score(envs, monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 2, 20, 9, tzinfo=tz)

    monkeypatch.setattr(salesforce_base, "datetime", FixedClock)
    calls = [
        ("gmail_find_email", {"query": 'subject:"Updated phone number"'}),
        (
            "salesforce_find_records",
            {
                "object": "Contact",
                "searchField": "Email",
                "searchValue": "jordan.lee@acmecorp.example.com",
            },
        ),
        ("salesforce_contact_update", {"id": "003001", "phone": "+1-555-0101"}),
    ]
    results = {}
    for suffix in ("", "_economy", "_fast"):
        env = envs["cost-speed" if suffix else "limited_zapier"]
        task = get_simple_email_sf_contact_phone_update()
        task["info"]["initial_state"]["meta"] = {"current_time": "2026-02-20T09:00:00Z"}
        state = await env.setup_state(task)
        if suffix:
            assert len(state["tool_defs"]) == 8
        responses = [await call(env, state, name + suffix, **kwargs) for name, kwargs in calls]
        assert partial_credit(state) == 1.0
        results[suffix] = responses, state["world"].model_dump(), state.get("tool_variants")
    baseline = results[""][:2]
    assert results["_economy"][:2] == results["_fast"][:2] == baseline
    economy, fast = (results[suffix][2] for suffix in ("_economy", "_fast"))
    assert (economy["total_cost_units"], economy["total_simulated_latency_ms"]) == (3, 900)
    assert (fast["total_cost_units"], fast["total_simulated_latency_ms"]) == (9, 300)


async def test_stability_timeout_is_reproducible_and_precedes_writes(envs):
    env = envs["stability"]
    # Select a seed whose first call fails so the write would change state.
    seed = next(seed for seed in range(100) if Random(f"{seed}:3001").random() < 0.10)
    fault_env = AutomationBenchEnv(
        dataset=env.dataset,
        rubric=create_rubric(),
        toolset="variants",
        variant_scenario="stability",
        variant_seed=seed,
    )
    reports = []
    for _ in range(2):
        state = await fault_env.setup_state(get_simple_email_sf_contact_phone_update())
        original_world = state["world"].model_dump()
        denied = await call(fault_env, state, "slack_send_channel_message_economy")
        assert "not available" in denied["error"]
        result = await call(
            fault_env,
            state,
            "salesforce_contact_update_economy",
            id="003001",
            phone="+1-555-0101",
        )
        assert result["retryable"]
        assert state["world"].model_dump() == original_world
        reports.append(state["tool_variants"])

    assert reports[0] == reports[1]
    assert reports[0]["injected_timeouts"] == 1
    assert reports[0]["total_cost_units"] == 1


async def test_multi_objective_charges_normal_latency_and_matches_failures_across_versions(
    envs, monkeypatch
):
    env = envs[MULTI_OBJECTIVE]
    action = "google_sheets_get_many_rows"
    arguments = {"spreadsheet": "ss_recurring_je", "worksheet": "ws_schedule_je"}
    bound = signature(google_sheets_get_many_rows).bind(None, **arguments)
    bound.apply_defaults()
    call_id = _tool_call_id(action, bound.arguments)

    def matched_fault(seed):
        execution = VariantExecution.start([], MULTI_OBJECTIVE, seed, 4083)
        _, first = execution.fault_draw(call_id)
        _, retry = execution.fault_draw(call_id)
        return 0.01 <= first < 0.10 and retry >= 0.01

    seed = next(seed for seed in range(1000) if matched_fault(seed))
    monkeypatch.setattr(env, "variant_seed", seed)
    cost = await env.setup_state(get_fin_qb_recurring_journal_task())
    speed = await env.setup_state(get_fin_qb_recurring_journal_task())
    stable = await env.setup_state(get_fin_qb_recurring_journal_task())
    names = {tool.name for tool in cost["tool_defs"]}
    assert {f"{action}_{name}" for name in ("economy", "fast", "reliable")} <= names
    assert "google_sheets_update_row_reliable" in names

    await call(env, speed, "gmail_find_email_economy", query="procedures")
    for state, version, fee, latency in ((cost, "economy", 1, 300), (speed, "fast", 2, 100)):
        result = await call(env, state, f"{action}_{version}", **arguments)
        assert result == {"error": "Simulated tool failure", "retryable": True}
        attempt = state["tool_variants"]["attempts"][-1]
        assert attempt["status"] == "injected_failure"
        assert attempt["fault_call_id"] == call_id
        assert attempt["fault_attempt"] == 1
        assert attempt["cost_units"] == fee
        assert attempt["simulated_latency_ms"] == latency

    success = await call(env, stable, f"{action}_reliable", **arguments)
    # Passing equivalent spreadsheet/worksheet aliases matches the original read.
    recovered = await call(
        env,
        cost,
        f"{action}_reliable",
        spreadsheet_id=arguments["spreadsheet"],
        worksheet_id=arguments["worksheet"],
    )
    assert [row["cells"] for row in recovered["rows"]] == [row["cells"] for row in success["rows"]]
    assert success["result_count"] > 0
    report = cost["tool_variants"]
    assert report["injected_failures"] == 1
    assert report["total_cost_units"] == 3
    assert report["total_simulated_latency_ms"] == 600
    assert report["attempts"][-1]["fault_attempt"] == 2
    assert report["attempts"][-1]["recovered_after_failure"] is True


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        (gmail_send_email, {"to": "test@example.com", "subject": "Follow-up", "body": "Hello"}),
        (
            google_sheets_append_row,
            {
                "spreadsheet": "ss_event",
                "worksheet": "ws_sent",
                "cells": {"email": "test@example.com"},
            },
        ),
    ],
)
async def test_multi_objective_failure_precedes_write_and_retry_executes_once(
    envs, monkeypatch, action, arguments
):
    env = envs[MULTI_OBJECTIVE]
    task = get_event_followup_personalization_task()
    bound = signature(action).bind(None, **arguments)
    bound.apply_defaults()
    call_id = _tool_call_id(action.__name__, bound.arguments)

    def matched_fault(seed):
        execution = VariantExecution.start([], MULTI_OBJECTIVE, seed, task["example_id"])
        return execution.fault_draw(call_id)[1] < 0.10 and execution.fault_draw(call_id)[1] >= 0.01

    monkeypatch.setattr(
        env, "variant_seed", next(seed for seed in range(1000) if matched_fault(seed))
    )
    state = await env.setup_state(task)
    world = state["world"]
    original = world.model_dump()

    assert (await call(env, state, f"{action.__name__}_economy", **arguments))["retryable"]
    assert world.model_dump() == original
    assert (await call(env, state, f"{action.__name__}_reliable", **arguments))["success"]

    report = state["tool_variants"]
    assert [a["fault_call_id"] for a in report["attempts"]] == [call_id, call_id]
    assert report["attempts"][1]["recovered_after_failure"] is True
    assert (report["total_cost_units"], report["total_simulated_latency_ms"]) == (3, 600)
    if action is gmail_send_email:
        assert len(world.gmail.messages) == len(original["gmail"]["messages"]) + 1
    else:
        assert len(world.google_sheets.rows) == len(original["google_sheets"]["rows"]) + 1


async def test_quality_cap_uses_original_response_shape_and_paging(envs):
    task = get_support_zendesk_sf_case_sync_task()
    sheet = task["info"]["initial_state"]["google_sheets"]["spreadsheets"][2]["worksheets"][0]
    batch_reference = sheet["rows"].pop(0)
    sheet["rows"].insert(0, {"row_id": 2, "cells": {"Setting": "Extra", "Value": "Example"}})
    sheet["rows"].append({**batch_reference, "row_id": 7})
    for row in sheet["rows"]:
        row["id"] = f"config-{row['row_id']}"
    # Isolate the original batch-reference assertion from unrelated sync work.
    task["info"]["assertions"] = [
        assertion
        for assertion in task["info"]["assertions"]
        if assertion.get("text_contains") == "SYNC-OPS-20260210"
    ]
    env = envs["quality"]
    observed = {}
    for suffix in ("economy", "standard"):
        state = await env.setup_state(task)
        output = await call(
            env,
            state,
            f"google_sheets_find_many_rows_{suffix}",
            spreadsheet="ss_config",
            worksheet="ws_config",
            row_count=10,
        )
        reference = next(
            (
                row["cells"]["Value"]
                for row in output["rows"]
                if row["cells"]["Setting"] == "Batch_Reference"
            ),
            "missing",
        )
        await call(
            env,
            state,
            "slack_send_channel_message_economy",
            channel="C_SUPPORT",
            text=f"Batch reference: {reference}",
        )
        observed[suffix] = output, partial_credit(state)

    limited, limited_score = observed["economy"]
    complete, complete_score = observed["standard"]
    assert limited.keys() == complete.keys()
    assert limited["rows"] == complete["rows"][:5]
    assert limited["result_count"] == 5
    assert limited["has_more"]
    assert complete["result_count"] == 6
    assert not complete["has_more"]
    assert (limited_score, complete_score) == (0.0, 1.0)
