"""Execute AppWorld train/dev references, replay API calls, and encrypt outputs.

Use an isolated appworld==0.1.3.post1 environment (Python 3.11).
APPWORLD_ROOT must contain the official data-0.1.0 download.
"""

import argparse
import copy
import json
import re
import tempfile
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from appworld import AppWorld
from appworld.common.constants import PASSWORD, SALT
from appworld.common.path_store import path_store
from appworld.common.utils import pack_bundle
from appworld.requester import Requester, _get_api_name_to_doc
from appworld.task import load_task_ids
from common import ROOT, VERSIONS, digest, dispatch_identity, metrics, qualified, save, select


def replay(task_id, calls, versions=VERSIONS):
    with AppWorld(
        task_id=task_id,
        experiment_name="phase-one-replay",
        ground_truth_mode="full",
        raise_on_failure=False,
    ) as world:
        outcomes = []
        token_bindings = {}

        def bind(value):
            if isinstance(value, str):
                return token_bindings.get(value, value)
            if isinstance(value, dict):
                return {k: bind(v) for k, v in value.items()}
            if isinstance(value, list):
                return [bind(v) for v in value]
            return value

        for index, call in enumerate(calls):
            args = bind(call["arguments"])
            response = dispatch_identity(
                args,
                versions[index % len(versions)],
                lambda materialized, method=call["method"]: getattr(
                    world.requester, "_" + method.lower()
                )(materialized["url"], data=materialized["data"], raise_on_failure=False),
            )
            result = response.json()
            # Session credentials are runtime values, not fixed business parameters.
            # Preserve the login dependency and bind its fresh output on each replay.
            if isinstance(call["result"], dict) and isinstance(result, dict):
                for key in ("access_token", "refresh_token", "token"):
                    if isinstance(call["result"].get(key), str) and isinstance(
                        result.get(key), str
                    ):
                        token_bindings[call["result"][key]] = result[key]
            outcomes.append({"status_code": response.status_code, "result": result})
        # AppWorld evaluates its saved output DB. A no-op shell execution performs
        # the official post-execution persistence after direct requester replay.
        world.execute("pass")
        tracker = world.evaluate()
        return {
            "passed": bool(tracker.success),
            "evaluation": tracker.to_dict(),
            "outcomes": outcomes,
        }


def build(task_id, source_split):
    calls = []
    api_patterns = {}

    def wrap(method, original):
        def capture(self, url, data=None, *args, **kwargs):
            # Requester.prepare pops access_token from data. Capture inputs first.
            original_data = copy.deepcopy(data)
            response = original(self, url, data, *args, **kwargs)
            application = url.lstrip("/").split("/", 1)[0]
            if application not in api_patterns:
                api_patterns[application] = []
                for name, doc in _get_api_name_to_doc(
                    application, include_private_apis=True
                ).items():
                    pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", re.escape(doc["path"]))
                    api_patterns[application].append(
                        (doc["method"].upper(), re.compile(pattern), name)
                    )
            api = next(
                (
                    application + "." + name
                    for verb, pattern, name in api_patterns[application]
                    if verb == method.upper() and pattern.fullmatch(url)
                ),
                method.upper() + " " + url,
            )
            try:
                result = response.json()
            except Exception:  # noqa: BLE001 -- preserve non-JSON responses and retry acquisition
                result = response.text
            calls.append(
                {
                    "index": len(calls),
                    "tool": "requester." + method,
                    "method": method.upper(),
                    "arguments": {"url": url, "data": original_data},
                    "api": api,
                    "application": application,
                    "result": result,
                    "status_code": response.status_code,
                    "successful": 200 <= response.status_code < 300,
                    "business": application not in ("supervisor", "api_docs", "admin")
                    and "/auth/" not in url,
                }
            )
            return response

        return capture

    with AppWorld(
        task_id=task_id,
        experiment_name="phase-one-reference",
        ground_truth_mode="full",
        raise_on_failure=False,
    ) as world:
        instruction = world.task.instruction
        code = world.task.ground_truth.compiled_solution_code + "\nsolution(apis, requester)"
        # Patching is limited to solution execution; evaluation must not enter the workflow.
        from contextlib import ExitStack

        with ExitStack() as stack:
            for method in ("get", "post", "put", "patch", "delete"):
                original = getattr(Requester, "_" + method)
                stack.enter_context(patch.object(Requester, "_" + method, wrap(method, original)))
            execution = world.execute(code)
        tracker = world.evaluate()
        reference = {
            "passed": bool(tracker.success),
            "evaluation": tracker.to_dict(),
            "execution": execution,
        }
    record = {
        "schema_version": "1.0",
        "environment": "appworld",
        "task_id": task_id,
        "template_group": task_id.split("_")[0],
        "instruction": instruction,
        "environment_version": {
            "package": "appworld",
            "version": version("appworld"),
            "data_version": "0.1.0",
        },
        "source": {
            "kind": "official_reference_solution",
            "model": None,
            "successful": reference["passed"],
            "source_split": source_split,
            "url": "https://github.com/StonyBrookNLP/appworld",
            "solution_sha256": digest(code),
        },
        "calls": calls,
        "metrics": metrics(calls),
        "reference_verification": reference,
        "reference_solution_code": code,
    }
    if reference["passed"] and 11 <= record["metrics"]["effective_business_calls"] <= 40:
        runs = [replay(task_id, calls), replay(task_id, calls)]
    else:
        runs = []
    record["verification"] = {
        "replay_passes": [r["passed"] for r in runs],
        "runs": runs,
        "variant_semantics_verified": True,
        "variant_contract": "Identity dispatch of the same tool and arguments; "
        "cost/latency/fault profiles are external and not validated here.",
    }
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-candidates", type=int, default=150)
    args = parser.parse_args()
    records, audit = [], []
    with tempfile.TemporaryDirectory(prefix="phase-one-appworld-") as temp:
        cache = Path(path_store.root) / "phase-one-cache-v2"
        cache.mkdir(exist_ok=True)
        for source_split in ("train", "dev"):
            # First examine one instance of every template, then other instances.
            task_ids = sorted(load_task_ids(source_split), key=lambda t: (t.split("_")[-1], t))
            for task_id in task_ids:
                cached = cache / (task_id + ".json")
                try:
                    if cached.exists():
                        record = json.loads(cached.read_text())
                        if 11 <= record["metrics"]["effective_business_calls"] <= 40:
                            runs = [
                                replay(task_id, record["calls"]),
                                replay(task_id, record["calls"]),
                            ]
                            record["verification"]["runs"] = runs
                            record["verification"]["replay_passes"] = [r["passed"] for r in runs]
                    else:
                        record = build(task_id, source_split)
                    save(cached, record)
                    records.append(record)
                    audit.append(
                        {
                            "task_id": task_id,
                            "qualified": qualified(record),
                            "metrics": record["metrics"],
                            "passes": record["verification"]["replay_passes"],
                        }
                    )
                except Exception as exc:  # noqa: BLE001 -- record candidate failures without aborting collection
                    audit.append({"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"})
                chosen = select(records, "appworld")
                print(
                    f"AW audited={len(audit)} selected={len(chosen)} latest={task_id}", flush=True
                )
                bands = [
                    sum(r["length_bin"] == b for r in chosen) for b in ("11-15", "16-25", "26-40")
                ]
                if bands == [8, 8, 4] or len(audit) >= args.max_candidates:
                    break
            if bands == [8, 8, 4] or len(audit) >= args.max_candidates:
                break
        chosen = select(records, "appworld")
        protected = Path(temp) / "appworld"
        for record in chosen:
            save(protected / (record["task_id"] + ".json"), record)
        save(protected / "audit.json", audit)
        pack_bundle(str(ROOT / "appworld.bundle"), temp, ["appworld"], PASSWORD, SALT)
        save(
            ROOT / "appworld_manifest.json",
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
                        "environment_version",
                    )
                }
                | {
                    "effective_business_calls": r["metrics"]["effective_business_calls"],
                    "replay_passes": r["verification"]["replay_passes"],
                    "payload_sha256": digest(r),
                }
                for r in chosen
            ],
        )
        save(
            ROOT / "appworld_audit_summary.json",
            {
                "audited": len(audit),
                "qualified": sum(qualified(r) for r in records),
                "selected": len(chosen),
                "errors": sum("error" in r for r in audit),
                "full_audit": "appworld.bundle:appworld/audit.json",
            },
        )
        print(f"Saved {len(chosen)} encrypted AppWorld workflows", flush=True)


if __name__ == "__main__":
    main()
