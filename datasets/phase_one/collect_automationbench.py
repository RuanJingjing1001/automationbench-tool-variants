"""Acquire pinned Holo successes and replay them with the original task scorer.

Run from repository root with the AutomationBench dependencies installed.
No model API calls are made. Source failures remain in audit.json.
"""

import argparse
import copy
import json
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import ROOT, VERSIONS, digest, dispatch_identity, metrics, qualified, save, select

from automationbench.domains import get_domain_dataset
from automationbench.rubric import partial_credit
from automationbench.schema.world import WorldState
from automationbench.scripts.long_trace_screen import result_status
from automationbench.tools.api import api_fetch
from automationbench.tools.api.search import _compute_url, _load_schemas

REVISION = "ede2a362c99ddba0d03f439c47a0b8fea6abae22"
BASE = f"https://huggingface.co/datasets/Hcompany/trajectories/resolve/{REVISION}/data"


def endpoint_patterns():
    patterns = []
    for app, schema in _load_schemas().items():
        for endpoint in schema["endpoints"]:
            url = _compute_url(app, schema["baseUrl"], endpoint["path"])
            pattern = re.escape(url)
            pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/?]+", pattern)
            patterns.append(
                (endpoint["method"], re.compile(pattern + r"/?(?:\?.*)?$"), endpoint["id"], app)
            )
    return patterns


ENDPOINTS = endpoint_patterns()


def identify(args):
    for method, pattern, api, app in ENDPOINTS:
        if args["method"].upper() == method and pattern.fullmatch(args["url"]):
            return api, app
    return args["method"].upper() + " " + urlparse(args["url"]).path, urlparse(args["url"]).netloc


def download(url, path):
    if not path.exists():
        for attempt in range(3):
            try:
                data = urllib.request.urlopen(url, timeout=30).read()
                payload = json.loads(data)
                save(path, payload)
                break
            except Exception:
                if attempt == 2:
                    raise
    return json.loads(path.read_text())


def load_task(name):
    for row in get_domain_dataset(name.split(".")[0]):
        info = json.loads(row["info"])
        if info["task_name"] == name:
            return {**row, "info": info}
    raise ValueError(f"Task not available: {name}")


def replay(task, calls, versions=VERSIONS):
    # Some schema normalizers consume legacy input keys. Keep the public task
    # snapshot immutable across WorldState construction and rubric evaluation.
    info = copy.deepcopy(task["info"])
    world = WorldState(**copy.deepcopy(info["initial_state"]))
    results = []
    for index, call in enumerate(calls):
        try:
            result = dispatch_identity(
                call["arguments"],
                versions[index % len(versions)],
                lambda args: api_fetch(world=world, **args),
            )
            results.append({"result": result, "exception": None})
        except Exception as exc:  # noqa: BLE001 -- record candidate failures without aborting collection
            results.append({"result": None, "exception": f"{type(exc).__name__}: {exc}"})
    state = {"world": world, "info": info, "initial_state": copy.deepcopy(info["initial_state"])}
    score = partial_credit(state)
    return {
        "passed": score == 1.0,
        "score": score,
        "assertions": state.get("_assertion_results", []),
        "results": results,
    }


def build(trace, task, commit):
    calls = []
    for step_index, step in enumerate(trace["steps"]):
        for index, call in enumerate(step.get("calls") or []):
            if call["name"] != "api_fetch":
                continue
            results = step.get("results") or []
            if len(results) != len(step["calls"]):
                raise ValueError("Cannot align calls and results")
            args = call["args"]
            result = results[index]
            api, application = identify(args)
            calls.append(
                {
                    "index": len(calls),
                    "source_step": step_index,
                    "tool": "api_fetch",
                    "arguments": args,
                    "method": args["method"].upper(),
                    "api": api,
                    "application": application,
                    "result": result,
                    "successful": result_status(result) == "successful",
                    "business": True,
                }
            )
    runs = [replay(task, calls), replay(task, calls)]
    # The reference workflow contains failed attempts too. Use actual replay outcomes
    # for length, retaining the source outcomes separately for audit.
    actual_calls = copy.deepcopy(calls)
    for call, outcome in zip(actual_calls, runs[0]["results"]):
        call["source_result"] = call["result"]
        call["result"] = outcome["result"]
        call["successful"] = (
            outcome["exception"] is None and result_status(outcome["result"]) == "successful"
        )
    return {
        "schema_version": "1.0",
        "environment": "automationbench",
        "task_id": task["info"]["task_name"],
        "example_id": task["example_id"],
        "template_group": task["info"]["task_name"],
        "instruction": trace["instruction"],
        "source": {
            "kind": "holo4",
            "model": trace["model"],
            "successful": True,
            "trace_id": trace["id"],
            "revision": REVISION,
            "url": f"{BASE}/t/{trace['id']}.json",
            "sha256": digest(trace),
        },
        "environment_version": {"repository_commit": commit},
        "task_snapshot": task,
        "source_trace": trace,
        "calls": actual_calls,
        "metrics": metrics(actual_calls),
        "verification": {
            "replay_passes": [r["passed"] for r in runs],
            "runs": runs,
            "variant_semantics_verified": True,
            "variant_contract": "Identity dispatch of the same tool and arguments; "
            "cost/latency/fault profiles are external and not validated here.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/phase-one-holo"))
    parser.add_argument("--max-candidates", type=int, default=240)
    args = parser.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    index = download(f"{BASE}/index.json", args.cache_dir / "index.json")
    # Start from the full audited >10-call inventory, not the old Pilot50.
    inventory = json.loads(
        (ROOT.parents[1] / "benchmarks/holo4-effective-calls-gt10.json").read_text()
    )
    names = {r["task"] for r in inventory["tasks"]}
    rows = [
        r
        for r in index["trajectories"]
        if r["benchmark"] == "AutomationBench"
        and r["success"]
        and r["score"] == 1.0
        and r["task"].rsplit("-", 1)[0] in names
    ]
    # Round-robin domains prevents the inventory's alphabetical finance block dominating.
    groups = {}
    for r in rows:
        groups.setdefault(r["task"].split(".")[0], []).append(r)
    rows = []
    for offset in range(max(map(len, groups.values()))):
        for domain in sorted(groups):
            if offset < len(groups[domain]):
                rows.append(groups[domain][offset])
    rows = rows[: args.max_candidates]

    def acquire(row):
        try:
            return (
                row,
                download(f"{BASE}/t/{row['id']}.json", args.cache_dir / f"{row['id']}.json"),
                None,
            )
        except Exception as exc:  # noqa: BLE001 -- record candidate failures without aborting collection
            return row, None, str(exc)

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    records, audit, tasks = [], [], {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        for row, trace, error in pool.map(acquire, rows):
            task_name = row["task"].rsplit("-", 1)[0]
            try:
                if error:
                    raise ValueError(error)
                if trace["id"] != row["id"] or trace["task"] != row["task"] or not trace["success"]:
                    raise ValueError("Source identity mismatch")
                if task_name not in tasks:
                    tasks[task_name] = load_task(task_name)
                record = build(trace, tasks[task_name], commit)
                records.append(record)
                audit.append(
                    {
                        "trace_id": row["id"],
                        "task_id": task_name,
                        "qualified": qualified(record),
                        "metrics": record["metrics"],
                        "passes": record["verification"]["replay_passes"],
                    }
                )
            except Exception as exc:  # noqa: BLE001 -- record candidate failures without aborting collection
                audit.append({"trace_id": row["id"], "task_id": task_name, "error": str(exc)})
            chosen = select(records, "automationbench")
            if len(audit) % 10 == 0:
                print(f"AB audited={len(audit)} selected={len(chosen)}", flush=True)
            bands = [sum(r["length_bin"] == b for r in chosen) for b in ("11-15", "16-25", "26-40")]
            if bands == [12, 12, 6]:
                break
    chosen = select(records, "automationbench")
    for row in chosen:
        save(ROOT / "automationbench" / (row["task_id"] + ".json"), row)
    save(
        ROOT / "automationbench_manifest.json",
        [
            {
                k: r[k]
                for k in (
                    "schema_version",
                    "environment",
                    "task_id",
                    "template_group",
                    "split",
                    "length_bin",
                    "source",
                    "metrics",
                    "environment_version",
                )
            }
            for r in chosen
        ],
    )
    save(ROOT / "automationbench_audit.json", audit)
    print(f"Saved {len(chosen)} replay-verified AutomationBench workflows", flush=True)


if __name__ == "__main__":
    main()
