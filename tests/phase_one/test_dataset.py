"""Dataset invariants: deduplication, credentials, acceptance, and split leakage."""

import json
import sys
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[2] / "datasets/phase_one"
sys.path.insert(0, str(MODULE_ROOT))
from common import dispatch_identity, metrics, qualified, select
from manage import validate


def call(method="GET", result=None, successful=True, business=True):
    return {
        "method": method,
        "arguments": {"url": "/records", "data": {}},
        "result": result or {"records": [1]},
        "successful": successful,
        "business": business,
        "api": "records.list",
        "application": "example",
    }


def test_duplicate_reads_only_between_writes():
    calls = [call(), call(), call("POST"), call(), call(successful=False), call()]
    stats = metrics(calls)
    assert stats["effective_business_calls"] == 3
    assert stats["duplicate_reads"] == 2
    assert stats["failed_business_attempts"] == 1
    assert len(calls) == 6  # Counting never deletes execution steps.


def test_helper_and_auth_calls_preserved_but_not_counted():
    calls = [call(business=False), call(result={"records": [2]})]
    assert metrics(calls)["total_attempts"] == 2
    assert metrics(calls)["effective_business_calls"] == 1


def row(name, group, n=12):
    return {
        "environment": "appworld",
        "task_id": name,
        "template_group": group,
        "metrics": {"effective_business_calls": n, "failed_business_attempts": 0},
        "source": {"successful": True},
        "verification": {"replay_passes": [True, True], "variant_semantics_verified": True},
    }


def test_source_success_does_not_replace_replay():
    item = row("a_1", "a")
    item["verification"]["replay_passes"] = [True, False]
    assert not qualified(item)


def test_related_instances_share_split():
    rows = [row(f"{g}_{i}", g) for g in ("a", "b", "c", "d") for i in (1, 2, 3)]
    selected = select(rows, "appworld")
    validate(selected)
    for group in ("a", "b", "c", "d"):
        assert len({r["split"] for r in selected if r["template_group"] == group}) == 1


def test_validate_detects_template_leakage():
    a, b = row("a_1", "a"), row("a_2", "a")
    a.update(split="dev", length_bin="11-15")
    b.update(split="test", length_bin="11-15")
    with pytest.raises(ValueError, match="leaks"):
        validate([a, b])


def test_identity_versions_preserve_business_inputs_and_isolate_mutation():
    arguments = {"ids": [1, 2]}
    observed = []

    def invoke(args):
        observed.append(args.copy())
        args["ids"].append(3)
        return args

    outcomes = [dispatch_identity(arguments, v, invoke) for v in ("economy", "fast", "reliable")]
    assert outcomes[0] == outcomes[1] == outcomes[2] == {"ids": [1, 2, 3]}
    assert arguments == {"ids": [1, 2]}
    with pytest.raises(ValueError):
        dispatch_identity(arguments, "unknown", invoke)


def test_legacy_schema_replay_keeps_public_fixture_immutable():
    from collect_automationbench import replay
    from common import digest

    record = json.loads(
        (MODULE_ROOT / "automationbench/hr.diversity_metrics_report.json").read_text()
    )
    before = digest(record["task_snapshot"])
    assert replay(record["task_snapshot"], record["calls"])["passed"]
    assert digest(record["task_snapshot"]) == before
