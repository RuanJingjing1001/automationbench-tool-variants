"""Screen AutomationBench tasks for long successful Holo4 business-call traces."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
from statistics import median
import subprocess

SOURCE = "https://huggingface.co/datasets/Hcompany/trajectories/resolve"
SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")


def fetch_json(url: str, path: Path, offline: bool) -> dict:
    if not path.exists():
        if offline:
            raise FileNotFoundError(path)
        tmp = path.with_suffix(".part")
        try:
            subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--silent",
                    "--show-error",
                    "--retry",
                    "2",
                    "--retry-max-time",
                    "45",
                    "--max-time",
                    "25",
                    url,
                    "--output",
                    str(tmp),
                ],
                check=True,
                capture_output=True,
                timeout=75,
            )
            payload = json.loads(tmp.read_text())
            if not isinstance(payload, dict):
                raise ValueError(f"expected a JSON object from {url}")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
    return json.loads(path.read_text())


def encode(value) -> str:
    """Normalize JSON expressed in tool arguments or results as a string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def result_status(result) -> str:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            if re.match(r"^\s*(error|failed|exception|timeout)\b", result, re.I):
                return "failed"
            return "unclassified"

    if isinstance(result, dict):
        status = result.get("status_code", result.get("status"))
        if (
            result.get("error")
            or result.get("errors")
            or result.get("Fault")
            or result.get("success") is False
            or result.get("ok") is False
            or result.get("isError") is True
            or (str(status).isdigit() and int(status) >= 400)
        ):
            return "failed"
        return "successful"
    if isinstance(result, list):
        return "successful"
    return "unclassified"


def count_calls(trace: dict) -> dict:
    counts = Counter(
        api_fetch=0,
        api_search=0,
        other_calls=0,
        successful_calls=0,
        failed_calls=0,
        unclassified_calls=0,
        duplicate_reads=0,
        retry_attempts=0,
    )
    reads = set()
    pending_failures = set()
    for step in trace["steps"]:
        results = step.get("results") or []
        calls = step.get("calls") or []
        for index, call in enumerate(calls):
            name = call["name"]
            if name != "api_fetch":
                counts["api_search" if name == "api_search" else "other_calls"] += 1
                continue
            counts["api_fetch"] += 1
            args = call.get("args") or {}
            method = str(args.get("method") or "").upper()
            key = (method, args.get("url"), encode(args.get("params")), encode(args.get("body")))
            if key in pending_failures:
                counts["retry_attempts"] += 1
            # If the logged results cannot be aligned, avoid guessing which call succeeded.
            result = results[index] if len(results) == len(calls) else None
            status = result_status(result)
            counts[f"{status}_calls"] += 1
            if status == "failed":
                pending_failures.add(key)
            elif status == "successful":
                pending_failures.discard(key)
                if method in ("GET", "HEAD") and args.get("url"):
                    read = (key, encode(result))
                    counts["duplicate_reads"] += read in reads
                    reads.add(read)
                else:
                    # Even a write to a different resource can change a later read.
                    reads.clear()

    counts["effective_calls"] = counts["successful_calls"] - counts["duplicate_reads"]
    return dict(counts)


def public_candidates(index: dict, min_index_steps: int) -> list[dict]:
    successful = [
        row
        for row in index["trajectories"]
        if row["benchmark"] == "AutomationBench" and row["success"]
    ]
    candidate_ids = {row["task"] for row in successful if row["steps"] >= min_index_steps}
    # Keep short successful traces for those same tasks. They guard against
    # mistakenly ranking a task highly based on just one long run.
    return [row for row in successful if row["task"] in candidate_ids]


def rank_tasks(
    references: list[dict],
    metrics: dict[str, dict],
    min_calls: int,
    min_long_traces: int,
    shortlist_size: int,
) -> tuple[list[dict], list[str]]:
    groups = defaultdict(list)
    for row in references:
        groups[row["task"]].append(row)
    tasks = []
    for source_id, rows in groups.items():
        name, example = source_id.rsplit("-", 1)
        observed = [metrics[row["id"]]["effective_calls"] for row in rows if row["id"] in metrics]
        long_count = sum(calls >= min_calls for calls in observed)
        complete = len(observed) == len(rows)
        tasks.append(
            {
                "task": name,
                "example_id": int(example),
                "domain": name.split(".", 1)[0],
                "successful_traces": len(rows),
                "counted_traces": len(observed),
                "long_successful_traces": long_count,
                "min_effective_calls": min(observed, default=None),
                "median_effective_calls": median(observed) if observed else None,
                "max_effective_calls": max(observed, default=None),
                "all_successful_traces_long": complete and long_count == len(rows),
                "eligible": complete and long_count >= min_long_traces,
                "trace_ids": [row["id"] for row in rows],
            }
        )
    tasks.sort(
        key=lambda row: (
            not row["eligible"],
            not row["all_successful_traces_long"],
            -row["long_successful_traces"],
            -(row["min_effective_calls"] or 0),
            -(row["median_effective_calls"] or 0),
            row["task"],
        )
    )
    shortlist = []
    domain_counts = Counter()
    for row in tasks:
        if len(shortlist) >= shortlist_size:
            break
        if row["eligible"] and domain_counts[row["domain"]] < 2:
            shortlist.append(row["task"])
            domain_counts[row["domain"]] += 1
    return tasks, shortlist


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--shortlist-output", type=Path, help="Write a compact shortlist JSON")
    parser.add_argument("--index", type=Path, help="Use an already downloaded index")
    parser.add_argument("--revision", default="main", help="Hugging Face branch or commit")
    parser.add_argument("--offline", action="store_true", help="Only read cached JSON")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--min-index-steps", type=int, default=15)
    parser.add_argument("--min-calls", type=int, default=15)
    parser.add_argument("--min-long-traces", type=int, default=2)
    parser.add_argument("--shortlist-size", type=int, default=8)
    args = parser.parse_args()
    if not SAFE_NAME.fullmatch(args.revision):
        parser.error("revision must be a branch name or commit hash")
    if (
        min(
            args.workers,
            args.min_index_steps,
            args.min_calls,
            args.min_long_traces,
            args.shortlist_size,
        )
        < 1
    ):
        parser.error("numeric options must be positive")
    if args.shortlist_output and args.shortlist_output.resolve() == args.output.resolve():
        parser.error("the report and shortlist need different output paths")
    trace_cache = args.cache_dir / args.revision
    trace_cache.mkdir(parents=True, exist_ok=True)
    base = f"{SOURCE}/{args.revision}/data"
    index_path = args.index or args.cache_dir / f"index-{args.revision}.json"
    index = fetch_json(f"{base}/index.json", index_path, args.offline or bool(args.index))
    rows = public_candidates(index, args.min_index_steps)

    def count(row):
        trace_id = row["id"]
        if not SAFE_NAME.fullmatch(trace_id):
            raise ValueError("invalid trace identifier")
        url = f"{base}/t/{trace_id}.json"
        try:
            trace = fetch_json(url, trace_cache / f"{trace_id}.json", args.offline)
            if trace["id"] != trace_id or trace["task"] != row["task"] or not trace["success"]:
                raise ValueError("trace does not match its successful index row")
            stats = count_calls(trace)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            return trace_id, None, type(exc).__name__
        return (
            trace_id,
            {
                **stats,
                "task": row["task"],
                "model": row["model"],
                "run": row["run"],
                "index_steps": row["steps"],
                "source": url,
            },
            None,
        )

    metrics = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=args.workers) as workers:
        for i, (trace_id, stats, error) in enumerate(workers.map(count, rows), 1):
            if error:
                errors[trace_id] = error
            else:
                metrics[trace_id] = stats
            if i % 50 == 0 or i == len(rows):
                print(f"Processed {i}/{len(rows)} traces ({len(errors)} unavailable)", flush=True)
    tasks, shortlist = rank_tasks(
        rows, metrics, args.min_calls, args.min_long_traces, args.shortlist_size
    )
    report = {
        "source_index": f"{base}/index.json",
        "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "thresholds": {
            "min_index_steps": args.min_index_steps,
            "min_effective_calls": args.min_calls,
            "min_long_traces": args.min_long_traces,
            "max_shortlist_tasks_per_domain": 2,
        },
        "summary": {
            "candidate_tasks": len(tasks),
            "successful_traces_in_scope": len(rows),
            "traces_counted": len(metrics),
            "traces_unavailable": len(errors),
            "eligible_tasks": sum(row["eligible"] for row in tasks),
        },
        "shortlist": shortlist,
        "tasks": tasks,
        "traces": metrics,
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if args.shortlist_output and not errors:
        selected = set(shortlist)
        compact = {
            key: report[key] for key in ("source_index", "index_sha256", "thresholds", "summary")
        }
        compact["shortlist"] = shortlist
        compact["tasks"] = [row for row in tasks if row["task"] in selected]
        args.shortlist_output.parent.mkdir(parents=True, exist_ok=True)
        args.shortlist_output.write_text(json.dumps(compact, indent=2, ensure_ascii=False) + "\n")
    print(",".join(shortlist))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
