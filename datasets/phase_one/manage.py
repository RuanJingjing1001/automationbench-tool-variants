"""Assemble, validate, and locally unpack the phase-one fixed-workflow release."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from common import ROOT, digest, length_bin, metrics, qualified, save


def read_manifest():
    return [
        row
        for env in ("automationbench", "appworld")
        for row in json.loads((ROOT / f"{env}_manifest.json").read_text())
    ]


def validate(records):
    identities = set()
    groups = defaultdict(set)
    for record in records:
        identity = record["environment"], record["task_id"]
        if identity in identities:
            raise ValueError(f"Duplicate task: {identity}")
        identities.add(identity)
        groups[record["environment"], record["template_group"]].add(record["split"])
        n = record.get("metrics", {}).get(
            "effective_business_calls", record.get("effective_business_calls")
        )
        if length_bin(n) != record["length_bin"]:
            raise ValueError(f"Invalid length band: {identity}")
    if any(len(splits) != 1 for splits in groups.values()):
        raise ValueError("A template group leaks across dev and test")
    return records


def unpack(destination):
    from appworld.common.constants import PASSWORD, SALT
    from appworld.common.utils import unpack_bundle

    destination.mkdir(parents=True, exist_ok=True)
    unpack_bundle(str(ROOT / "appworld.bundle"), str(destination), PASSWORD, SALT)


def verify_payload(row, record):
    if row["environment"] != record["environment"] or row["task_id"] != record["task_id"]:
        raise ValueError("Manifest/payload identity mismatch")
    if not qualified(record) or metrics(record["calls"]) != record["metrics"]:
        raise ValueError(f"Unqualified or inconsistent workflow: {row['task_id']}")
    if "payload_sha256" in row and digest(record) != row["payload_sha256"]:
        raise ValueError(f"Payload hash mismatch: {row['task_id']}")


def build():
    rows = validate(read_manifest())
    for row in rows:
        if row["environment"] == "automationbench":
            path = ROOT / "automationbench" / (row["task_id"] + ".json")
            record = json.loads(path.read_text())
            verify_payload(row, record)
            row["payload"] = "automationbench/" + path.name
            row["payload_sha256"] = digest(record)
        else:
            row["payload"] = "appworld.bundle:appworld/" + row["task_id"] + ".json"
    counts = Counter(r["environment"] for r in rows)
    splits = Counter(r["split"] for r in rows)
    ready = counts == {"automationbench": 30, "appworld": 20} and splits == {"dev": 10, "test": 40}
    summary = {
        "schema_version": "1.0",
        "dataset_status": "complete" if ready else "partial",
        "workflow_count": len(rows),
        "environments": dict(counts),
        "splits": dict(splits),
        "length_bins": {
            env: dict(Counter(r["length_bin"] for r in rows if r["environment"] == env))
            for env in counts
        },
        "replay_runs_per_workflow": 2,
        "model_api_calls_for_collection": 0,
        "variant_scope": "Identity business dispatch only; experiment cost/latency/fault "
        "profiles and model version-selection runs are not part of this release.",
        "manifest_sha256": digest(rows),
    }
    save(ROOT / "manifest.json", rows)
    save(ROOT / "summary.json", summary)
    for split in ("dev", "test"):
        save(
            ROOT / "splits" / f"{split}.json",
            [
                {"environment": r["environment"], "task_id": r["task_id"]}
                for r in rows
                if r["split"] == split
            ],
        )
    save(
        ROOT / "checksums.json",
        {
            str(p.relative_to(ROOT)): __import__("hashlib").sha256(p.read_bytes()).hexdigest()
            for p in sorted(ROOT.rglob("*"))
            if p.is_file()
            and (p.suffix == ".json" or p.suffix == ".bundle")
            and p.name != "checksums.json"
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not ready:
        raise SystemExit("Target not met; partial data saved without padding")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "unpack", "verify", "replay"))
    parser.add_argument("--local-dir", type=Path, default=ROOT.parents[1] / "data/phase-one")
    parser.add_argument("--environment", choices=("automationbench", "appworld"))
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build()
    elif args.command == "replay":
        if not args.environment:
            parser.error("replay requires --environment")
        rows = [
            r
            for r in read_manifest()
            if r["environment"] == args.environment and r["split"] == args.split
        ]
        if args.limit is not None:
            rows = rows[: args.limit]
        if args.environment == "appworld":
            unpack(args.local_dir)
            from collect_appworld import replay
        else:
            from collect_automationbench import replay
        results = []
        for row in rows:
            directory = ROOT if args.environment == "automationbench" else args.local_dir
            record = json.loads(
                (directory / args.environment / (row["task_id"] + ".json")).read_text()
            )
            verify_payload(row, record)
            if args.environment == "automationbench":
                result = replay(record["task_snapshot"], record["calls"])
            else:
                result = replay(row["task_id"], record["calls"])
            results.append({"task_id": row["task_id"], "passed": result["passed"]})
            print(f"{row['task_id']}: {'PASS' if result['passed'] else 'FAIL'}", flush=True)
        if args.output:
            save(
                args.output,
                {
                    "environment": args.environment,
                    "split": args.split,
                    "versions": ["economy", "fast", "reliable"],
                    "profile": "identity dispatch; no injected faults or objective costs",
                    "results": results,
                },
            )
        if not results or not all(r["passed"] for r in results):
            raise SystemExit(1)
    else:
        unpack(args.local_dir)
        if args.command == "verify":
            rows = validate(read_manifest())
            for row in rows:
                if row["environment"] == "appworld":
                    record = json.loads(
                        (args.local_dir / "appworld" / (row["task_id"] + ".json")).read_text()
                    )
                    verify_payload(row, record)
            print("Encrypted AppWorld payloads and task/template splits verified")


if __name__ == "__main__":
    main()
