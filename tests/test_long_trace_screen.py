from automationbench.scripts.long_trace_screen import count_calls, public_candidates, rank_tasks


def step(method, path, result, params=None):
    return {
        "calls": [
            {
                "name": "api_fetch",
                "args": {"method": method, "url": f"https://example.com/{path}", "params": params},
            }
        ],
        "results": [result],
    }


def test_counts_successful_calls_and_keeps_repeated_writes():
    read = step("GET", "items", '{"items":[1]}', '{"b":2,"a":1}')
    duplicate = step("GET", "items", '{"items":[1]}', {"a": 1, "b": 2})
    write = step("POST", "items", '{"success":true}')
    trace = {
        "steps": [
            read,
            duplicate,
            write,
            write,
            duplicate,
            step("GET", "users", '{"error":{"code":401}}'),
            step("GET", "users", '{"users":[]}'),
            {"calls": [{"name": "api_search"}, {"name": "answer"}], "results": []},
            {"calls": [{"name": "api_fetch"}], "results": []},
        ]
    }
    assert count_calls(trace) == {
        "api_fetch": 8,
        "api_search": 1,
        "other_calls": 1,
        "successful_calls": 6,
        "failed_calls": 1,
        "unclassified_calls": 1,
        "duplicate_reads": 1,
        "retry_attempts": 1,
        "effective_calls": 5,
    }


def test_selection_keeps_short_successes_and_prioritizes_consistently_long_tasks():
    def row(id, task, steps, success=True):
        return {
            "benchmark": "AutomationBench",
            "task": task,
            "steps": steps,
            "success": success,
            "id": id,
        }

    index = {
        "trajectories": [
            row("a1", "operations.a-1", 18),
            row("a2", "operations.a-1", 17),
            row("a3", "operations.a-1", 10),
            row("b1", "finance.b-2", 16),
            row("b2", "finance.b-2", 16),
            row("b3", "finance.b-2", 30, success=False),
            row("c1", "sales.c-3", 9),
        ]
    }
    candidates = public_candidates(index, 15)
    assert [r["id"] for r in candidates] == ["a1", "a2", "a3", "b1", "b2"]
    metrics = {
        id: {"effective_calls": n}
        for id, n in {
            "a1": 20,
            "a2": 15,
            "a3": 8,
            "b1": 16,
            "b2": 15,
        }.items()
    }
    tasks, shortlist = rank_tasks(candidates, metrics, 15, 2, 2)
    assert shortlist == ["finance.b", "operations.a"]
    assert tasks[1]["min_effective_calls"] == 8
    metrics.pop("b2")
    _, shortlist = rank_tasks(candidates, metrics, 15, 2, 2)
    assert shortlist == ["operations.a"]
