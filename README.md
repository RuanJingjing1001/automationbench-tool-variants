# AutomationBench

A benchmark for evaluating AI agents on realistic business workflows.

- **White Paper:** https://arxiv.org/abs/2604.18934
- **GitHub:** https://github.com/zapier/AutomationBench
- **Artificial Analysis:** https://artificialanalysis.ai/evaluations/automationbench-aa

Learn more at [zapier.com/benchmarks](https://zapier.com/benchmarks) or run it on the [Prime Intellect Environments Hub](https://app.primeintellect.ai/dashboard/environments/zapier/AutomationBench).

## 第一阶段 · 同功能工具变体

基于 Zapier AutomationBench `4a8e106`。选择 `--toolset variants` 时，每个任务原先可用的业务动作会展开成两个独立可调用的工具，比如 `salesforce_find_records_economy` 和 `salesforce_find_records_fast`。两版复用原参数和业务函数，原任务断言照常评分。`--toolset limited_zapier` 可运行单版本对照。

| `--variant-scenario` | 两个版本 | 实验差异 |
| --- | --- | --- |
| `cost-speed`（默认） | `economy` / `fast` | 每次 1 单位 / 300 ms 与 3 单位 / 100 ms。 |
| `stability` | `economy` / `reliable` | 两版均 300 ms，费用分别为 1 和 3 单位，执行前超时概率分别为 10% 和 1%。 |
| `quality` | `economy` / `standard` | 两版均 300 ms，费用分别为 1 和 3 单位。仅 `google_sheets_find_many_rows` 的省钱版每次最多返回 5 行，标准版使用原有参数和返回上限。使用数据超过 5 行且实际需要这些行的题检验质量差异。 |

使用已配置凭据的模型运行同一任务两次。以下以 OpenAI 接口为例，需设置 `OPENAI_API_KEY`。

```sh
uv sync --locked
for preference in cost speed; do
  uv run auto-bench --model gpt-5-mini --domains simple \
    --tasks simple.email_sf_contact_phone_update --toolset variants \
    --tool-preference "$preference" --export-json "phase-one-$preference.json"
done
```

在导出的 `tasks[].tool_variants` 里，查看 `attempts[].tool` 比较实际选择，再对照 `total_cost_units`、`total_simulated_latency_ms`、`injected_timeouts` 和任务得分。工具费用按尝试累计，包括模拟超时和重试。模拟延迟是当前串行执行顺序下的累加值。实测工具耗时和模型费用使用原导出字段。稳定性场景可重复同一组 `--variant-seed` 比较策略，超时在业务函数执行前返回。

统一配置位于 [tool_variants.py](automationbench/tool_variants.py)。可用 `uv run pytest -q tests/test_tool_variants.py` 做离线回放验证。

### 配对评测

配置模型凭据后运行。默认固定五道题、同一模型和工具配置，分别给出成本优先与速度优先指令。可用 `--tasks 域.任务名,域.任务名` 覆盖题目列表。

```sh
uv run python -m automationbench.scripts.paired_eval --model gpt-4.1-mini \
  --output-dir visualizer/runs/local/paired-pilot
```

输出 `cost.json`、`speed.json` 和 `comparison.json`。汇总保留失败与未完成的任务，按业务动作比较实际工具名和调用序列。费用节省值为速度组费用减成本组费用，时延节省值为成本组模拟时延减速度组模拟时延。

### 50 道长任务的成本与延迟实验

使用[固定的 50 道题名单](benchmarks/holo4-effective-calls-gt10-pilot50.json)。在仓库根目录配置 `OPENAI_API_KEY`，使用兼容网关时同时配置 `OPENAI_BASE_URL`。先跑名单里的前 5 道。

```sh
uv run python -m automationbench.scripts.paired_eval \
  --model gpt-5.5 --api chat_completions --pilot50 --limit 5 \
  --output-dir visualizer/runs/local/pilot50-first5
```

跑完整 50 道时去掉 `--limit 5`，并指定新的输出目录。`--pilot50` 默认每题最多 64 个模型回合，每组最多运行 3600 秒，可通过 `--max-steps` 和 `--arm-timeout` 调整。

两组使用相同的题面、初始状态、评分器和工具。系统提示词末尾分别追加成本优先或延迟优先的执行偏好。经济版每次 1 成本单位、300 ms 模拟延迟，快速版每次 3 成本单位、100 ms 模拟延迟。查看 `comparison.json` 中每题两组的任务得分、`variant_attempt_counts`、成本和模拟延迟。用任务得分和完成状态判断两组结果是否可比。

### 带预算的配对评测

可对联系人更新题加 `--budget 5 --tasks simple.email_sf_contact_phone_update`，使用不同的加速收益。查邮件、定位联系人和写入手机号码的快速版分别节省 600、250 和 50 ms，一条三步轨迹最多升级一个动作。预算写入两组提示词，报告记录实际是否超预算。

### 从公开轨迹筛长任务

[首批 8 道题及轨迹证据](benchmarks/holo4-long-trace-shortlist.json)。

```sh
uv run python -m automationbench.scripts.long_trace_screen \
  --cache-dir data/holo4-trajectories \
  --output visualizer/runs/local/holo4-long-trace-screen.json
```

脚本读取 H Company 发布的 Holo4 公开轨迹。先保留至少有一条成功轨迹达到 15 个索引步的题，再读取这些题的全部成功轨迹，包括较短的成功记录。逐条统计 `api_fetch` 调用，单列 `api_search`、可识别的失败、重试和无法判读的结果。同一请求返回同一结果的重复 GET 或 HEAD 在两次写入之间只计一次。写入和处理不同记录的调用逐次计入。

默认要求每道入围题至少有两条成功轨迹达到 15 次有效业务调用。报告同时保留最短、最长和中位调用数。默认选取最多 8 题、每个业务领域最多 2 题，优先选所有已发布成功轨迹都达标的题。结果衡量公开轨迹的实际调用长度。不同工具接口或模型可能找到更短的解法。

用 `--shortlist-output PATH` 可导出精简短名单，用 `--offline` 可从缓存重算，用 `--revision` 可指定 Hugging Face 的版本。若有轨迹无法读取，脚本仍输出覆盖情况和可计算的结果，并以非零状态退出。筛出的任务可通过现有 `paired_eval --tasks ...` 接入成本与速度评测。

### 筛选预算评测题目

把已有通过记录的原始评测文件交给筛选器。可重复传入 `--reference` 合并多个来源。

```sh
uv run python -m automationbench.scripts.budget_eval \
  --reference visualizer/runs/local/paired-pilot/cost.json \
  --reference visualizer/runs/local/paired-pilot/speed.json \
  --output-dir visualizer/runs/local/budget-plan
```

`selection.json` 把任务分为主实验、简单对照和待补参考。主实验默认要求至少四种不同业务动作、一条满分完成的参考轨迹、至少一个无法全选快速版的中间预算，以及不同步骤之间的加速收益差。筛选使用已经取得的参考轨迹，可独立于待测模型取得。

加 `--model MODEL` 在各预算下运行主实验题，加 `--include-controls` 同时运行对照。用 `--comparison PATH` 可离线给已有预算配对结果打分。`scores.json` 记录通过、超预算和模拟时延差。只有通过且预算内的结果计算时延差，最优值是在该结果**实际执行的同一动作序列**中重新选择工具版本得到的，执行其他动作路线时也有可比定义。计划中的参考最优值则以参考轨迹为条件。

## Overview

AutomationBench evaluates how well AI models complete realistic business workflows—the kind of tasks that happen daily in sales, marketing, operations, support, finance, and HR. Each task initializes a simulated business environment (CRM, calendar, inbox, etc.) across 47 simulated SaaS tools and checks whether the agent leaves it in the correct state.

## Domains

| Domain     | Tasks | Coverage                                                                |
| ---------- | ----- | ----------------------------------------------------------------------- |
| Sales      | 100   | CRM, lead management, cross-app workflows                               |
| Marketing  | 100   | Campaigns, ad performance, content ops, brand monitoring                |
| Operations | 100   | Facility management, project tracking, vendor workflows, compliance     |
| Support    | 100   | Ticket routing, SLA monitoring, knowledge base, multi-platform helpdesk |
| Finance    | 100   | AP/AR, expenses, reporting, bookkeeping                                 |
| HR         | 100   | Recruitment, employee onboarding, time off, payroll                     |

### Simple Domain

The `simple` domain contains 200 foundational single- and two-step tasks across CRM, email, Slack, calendar, project management, and more. It is not included in the benchmark score but serves as a baseline to verify that a model can handle basic tool use before tackling the more complex domain tasks.

### Public vs. Official Scores

This repository ships the **public** task set. The **official** AutomationBench leaderboard at [zapier.com/benchmarks](https://zapier.com/benchmarks) is scored on a separate, held-out private task set per domain. The private set follows a similar task distribution and assertion framework as the public set but is purposely harder and is not released, so scores you measure locally may not match the official leaderboard 1:1. Expect directional agreement — if a model improves on the public set, it is likely (but not guaranteed) to improve on the private set.

Also note that private tasks are sometimes made even harder in version updates to keep the benchmark around the same top score when fixing bugs to help the benchmark be challenging and useful for longer. When we make changes that affect scores, we try to rerun all models on the benchmark and update scores when they change outside run to run variance.

Pass rates on the 600-task **public** benchmark (100 tasks/domain across sales, marketing, operations, support, finance, and HR), each model run at its highest available reasoning effort:

| Model            | Reasoning Effort | Pass Rate |
| ---------------- | ---------------- | --------- |
| Claude Opus 5    | max              | 50.3%     |
| Kimi K3          | max              | 46.67%    |
| Claude Fable 5   | max              | 46.17%    |
| GPT-5.6 Sol      | max              | 45.83%    |
| Gemini 3.6 Flash | high             | 45.00%    |
| Claude Opus 4.8  | max              | 41.00%    |
| Gemini 3.5 Flash | high             | 38.33%    |
| GPT-5.6 Terra    | max              | 37.17%    |
| Claude Sonnet 5  | max              | 34.67%    |
| GLM 5.2          | max              | 26.17%    |

(Fable 5 score is after the July 2026 stricter classifier.)

## Principles

- **Verifiability** - All tasks must be programmatically verifiable. If we can't automatically check whether a task was completed correctly, it doesn't belong in the benchmark.

## How It Works

1. **Trigger Data** - Initial context that kicks off the workflow (no user interaction)
2. **Initial State** - Pre-populated simulated business environment
3. **Tools** - Domain-specific functions to interact with the environment
4. **Evaluation** - Compare final state against expected state using assertion-based rubrics

## Scoring

Every run reports two per-task metrics:

- **`partial_credit`** (0.0 - 1.0) - fraction of assertions satisfied. Used as the environment reward signal for denser training and iteration feedback.
- **`task_completed_correctly`** (0.0 or 1.0) - strict pass/fail; 1.0 only if _every_ assertion passes. The average of this across scored tasks (simple excluded) is the official AutomationBench pass rate.

## Usage

```bash
# Clone the repo
git clone https://github.com/zapier/AutomationBench.git
cd AutomationBench

# Install dependencies
uv sync

# Set your API key (or create a .env file)
export OPENAI_API_KEY=sk-...

# Run evaluation
uv run auto-bench --model gpt-5-mini

# Run specific domains
uv run auto-bench --model gpt-5-mini --domains sales

# Anthropic models — auto-detected via `claude-*` prefix
export ANTHROPIC_API_KEY=sk-ant-...
uv run auto-bench --model claude-haiku-4-5-20251001
```

### CLI Options

| Option               | Default          | Description                                             |
| -------------------- | ---------------- | ------------------------------------------------------- |
| `--model`            | `gpt-5-mini`     | Model name for evaluation                               |
| `--domains`          | `all`            | Comma-separated domains or "all"                        |
| `--toolset`          | `api`            | Tool style: `api`, `zapier`, or `limited_zapier`        |
| `--base-url`         | OpenAI           | API endpoint URL                                        |
| `--api-key`          | -                | API key (or use env var)                                |
| `--api-key-var`      | `OPENAI_API_KEY` | Environment variable for API key                        |
| `--num-examples`     | `-1`             | Number of examples (-1 for all)                         |
| `--max-steps`        | `50`             | Max model response steps per task                       |
| `--max-concurrent`   | `100`            | Max concurrent tasks                                    |
| `--tasks`            | -                | Comma-separated task names to run                       |
| `--skip`             | -                | Skip first N tasks                                      |
| `--reasoning-effort` | -                | Reasoning level: `low`/`medium`/`high`/`xhigh`/`max`    |
| `--responses-api`    | off              | Use the OpenAI Responses API (required for some models) |
| `--search-top-k`     | `20`             | Hard cap on search_tools top_k (zapier toolset only)    |
| `--headers`          | -                | Custom HTTP headers (`KEY=VALUE`, repeatable)           |
| `--input-cost`       | -                | Per-token input cost in USD (overrides lookup)          |
| `--output-cost`      | -                | Per-token output cost in USD (overrides lookup)         |
| `--export-json`      | auto             | Path to export results JSON                             |
| `--save-every`       | `1`              | Save incremental results every N tasks                  |

## Prime Intellect Environments Hub

AutomationBench is available as a hosted environment on Prime Intellect:

```bash
prime env install zapier/AutomationBench
prime eval run zapier/AutomationBench

# Smoke test with 5 examples
prime eval run zapier/AutomationBench --num-examples 5

# Run a single domain
prime eval run zapier/AutomationBench --env-args '{"domains": "sales"}'
```

See the [environment page](https://app.primeintellect.ai/dashboard/environments/zapier/AutomationBench) for details.

## Visualizer

AutomationBench includes a web-based visualizer for analyzing evaluation results:

```bash
# Run evaluation (results saved automatically to visualizer/runs/local/)
uv run auto-bench --model gpt-5-mini

# Or specify a custom export path
uv run auto-bench --model gpt-5-mini --export-json visualizer/results.json

# Start the visualizer (serves on http://localhost:8000)
python3 visualizer/serve.py
```

The visualizer provides:

- 📊 Interactive charts for score distribution and token usage
- 📈 Summary metrics with pass/fail counts and cost estimates
- 🔍 Filterable task results table
- 🔄 **Multi-run comparison mode** (compare 2-5 runs side-by-side)
- 📱 Responsive design for all devices

See [visualizer/README.md](visualizer/README.md) for details.

## Found a bug or unfair task?

Though we have taken many measures to try to decrease bugs in the benchmark, there are still many we have not caught. Benchmarking is hard!

Feel free to open an issue or contact us about bugs or unfair tasks in the benchmark. Help us make AutomationBench more accurate and provide a clear signal for what businesses care about.

Ideas for improvements? We are open to those too!

## Development

```bash
uv sync                    # Install dependencies
uv run pytest              # Run tests
uv run ruff check --fix .  # Lint and auto-fix
```
