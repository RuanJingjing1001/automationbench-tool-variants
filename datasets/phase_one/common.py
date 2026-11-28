"""Shared, dependency-free fixed-workflow dataset utilities."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSIONS = ("economy", "fast", "reliable")


def dispatch_identity(arguments, version, invoke):
    """Semantic kernel for fixed-workflow versions; no cost or failure model here.

    Every version invokes the identical business implementation and arguments.
    Experiments must add their own explicit cost/latency/pre-execution-fault profile.
    """
    import copy

    if version not in VERSIONS:
        raise ValueError(f"Unknown API version: {version}")
    return invoke(copy.deepcopy(arguments))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def metrics(calls):
    reads = set()
    effective = 0
    duplicates = 0
    for call in calls:
        # Any attempted write is a conservative read-deduplication barrier.
        method = call["method"].upper()
        if method not in ("GET", "HEAD"):
            reads.clear()
        if not call.get("business", True) or not call["successful"]:
            continue
        key = canonical([method, call["arguments"], call["result"]])
        if method in ("GET", "HEAD"):
            if key in reads:
                duplicates += 1
                continue
            reads.add(key)
        effective += 1
    business = [c for c in calls if c.get("business", True)]
    return {
        "effective_business_calls": effective,
        "total_attempts": len(calls),
        "business_attempts": len(business),
        "failed_business_attempts": sum(not c["successful"] for c in business),
        "duplicate_reads": duplicates,
        "distinct_apis": len({c["api"] for c in business}),
        "applications": sorted({c["application"] for c in business}),
    }


def length_bin(n):
    if 11 <= n <= 15:
        return "11-15"
    if 16 <= n <= 25:
        return "16-25"
    if 26 <= n <= 40:
        return "26-40"
    return None


def qualified(record):
    v = record["verification"]
    return (
        record["source"]["successful"] is True
        and v["replay_passes"] == [True, True]
        and v["variant_semantics_verified"] is True
        and length_bin(record["metrics"]["effective_business_calls"]) is not None
    )


def select(records, environment):
    """One task per workflow; group related templates on the same split."""
    quotas = {"automationbench": [12, 12, 6], "appworld": [8, 8, 4]}[environment]
    bins = ["11-15", "16-25", "26-40"]
    chosen = []
    seen = set()
    used_groups = set()
    for band, quota in zip(bins, quotas):
        candidates = sorted(
            (
                r
                for r in records
                if qualified(r) and length_bin(r["metrics"]["effective_business_calls"]) == band
            ),
            key=lambda r: (r["metrics"]["failed_business_attempts"], r["task_id"]),
        )
        count = 0
        while candidates and count < quota:
            row = min(
                candidates,
                key=lambda r: (
                    r["template_group"] in used_groups,
                    r["metrics"]["failed_business_attempts"],
                    r["task_id"],
                ),
            )
            candidates.remove(row)
            if row["task_id"] in seen:
                continue
            chosen.append(row)
            seen.add(row["task_id"])
            used_groups.add(row["template_group"])
            count += 1
    # Keep quotas as targets; fill gaps using other qualified lengths, without padding.
    for row in sorted(records, key=lambda r: r["task_id"]):
        if len(chosen) == sum(quotas):
            break
        if qualified(row) and row["task_id"] not in seen:
            chosen.append(row)
            seen.add(row["task_id"])
    # Prefer independent templates in the base set, so dev can have its exact quota.
    dev_target = {"automationbench": 6, "appworld": 4}[environment]
    group_sizes = {}
    for row in chosen:
        group_sizes[row["template_group"]] = group_sizes.get(row["template_group"], 0) + 1
    dev_groups = set()
    dev_size = 0
    if environment == "automationbench":
        # The six development workflows cover all six business domains.
        for domain in sorted({r["task_id"].split(".")[0] for r in chosen}):
            group = min(r["template_group"] for r in chosen if r["task_id"].split(".")[0] == domain)
            if dev_size + group_sizes[group] <= dev_target:
                dev_groups.add(group)
                dev_size += group_sizes[group]
    for group, size in sorted(group_sizes.items(), key=lambda item: (item[1], item[0])):
        if group not in dev_groups and dev_size + size <= dev_target:
            dev_groups.add(group)
            dev_size += size
    for row in chosen:
        row["split"] = "dev" if row["template_group"] in dev_groups else "test"
        row["length_bin"] = length_bin(row["metrics"]["effective_business_calls"])
    return sorted(chosen, key=lambda r: r["task_id"])
