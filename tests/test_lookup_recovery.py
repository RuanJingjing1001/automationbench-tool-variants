import json

import pytest
from verifiers.types import AssistantMessage, ToolCall

from automationbench.domains.sales.tasks import get_sales_dataset
from automationbench.lookup_recovery import LookupRecovery
from automationbench.rubric import create_rubric
from automationbench.runner import AutomationBenchEnv


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 3])
async def test_guard_handles_parallel_requests_and_restores_lookup(limit):
    requested_ids = []

    def find_by_id(resource_id: str) -> str:
        requested_ids.append(resource_id)
        if resource_id == "real":
            return '{"success": true}'
        return json.dumps({"error": f"Record {resource_id} not found"})

    def list_records() -> str:
        return '{"success": true, "ids": ["real"]}'

    env = AutomationBenchEnv(
        dataset=get_sales_dataset(),
        rubric=create_rubric(),
        tools=[find_by_id, list_records],
        toolset="limited_zapier",
        lookup_retry_limit=limit,
    )
    state = await env.setup_state({"info": {"zapier_tools": ["find_by_id", "list_records"]}})

    calls = [
        ToolCall(id=str(i), name="find_by_id", arguments=json.dumps({"resource_id": str(i)}))
        for i in range(4)
    ]
    calls += [
        ToolCall(id="list", name="list_records", arguments="{}"),
        ToolCall(id="real", name="find_by_id", arguments='{"resource_id": "real"}'),
    ]
    responses = await env.env_response([AssistantMessage(tool_calls=calls)], state)

    assert [response.tool_call_id for response in responses] == [call.id for call in calls]
    report = state["lookup_recovery"]
    if limit:
        assert requested_ids == ["0", "1", "2", "real"]
        assert "recovery_skill" in json.loads(responses[2].content)
        assert json.loads(responses[3].content)["code"] == "lookup_recovery_required"
        assert report["blocked_calls"] == 1
        assert report["repeated_not_found_calls"] == 2
    else:
        assert requested_ids == ["0", "1", "2", "3", "real"]
        assert report["blocked_calls"] == 0
        assert report["repeated_not_found_calls"] == 3


def test_retryable_timeouts_break_the_missing_resource_streak():
    recovery = LookupRecovery(3)
    missing = '{"error": "Spreadsheet not found"}'
    assert not recovery.observe("read", missing, "1")
    assert not recovery.observe("read", missing, "2")
    assert not recovery.observe(
        "read", '{"error": "Simulated tool timeout", "retryable": true}', "3"
    )
    assert not recovery.observe("read", missing, "4")
    assert recovery.report["not_found_calls"] == 3
    assert not recovery.paused_actions


@pytest.mark.asyncio
async def test_profiles_share_streak_and_blocked_variant_is_not_charged():
    env = AutomationBenchEnv(
        dataset=get_sales_dataset(),
        rubric=create_rubric(),
        toolset="variants",
        lookup_retry_limit=3,
    )
    action = "google_sheets_get_spreadsheet_by_id"
    state = await env.setup_state({"info": {"zapier_tools": [action]}})
    calls = [
        ToolCall(
            id=str(i),
            name=f"{action}_{profile}",
            arguments=json.dumps({"spreadsheet": f"wrong-{i}"}),
        )
        for i, profile in enumerate(("economy", "fast", "economy", "fast"))
    ]
    responses = await env.env_response([AssistantMessage(tool_calls=calls)], state)
    assert json.loads(responses[-1].content)["code"] == "lookup_recovery_required"
    assert state["lookup_recovery"]["not_found_calls"] == 3
    assert len(state["tool_variants"]["attempts"]) == 3
    assert state["tool_variants"]["total_cost_units"] == 5
