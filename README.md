# AutomationBench

A benchmark for evaluating AI agents on realistic business workflows.

- **White Paper:** https://arxiv.org/abs/2604.18934
- **GitHub:** https://github.com/zapier/AutomationBench
- **Artificial Analysis:** https://artificialanalysis.ai/evaluations/automationbench-aa

Learn more at [zapier.com/benchmarks](https://zapier.com/benchmarks) or run it on the [Prime Intellect Environments Hub](https://app.primeintellect.ai/dashboard/environments/zapier/AutomationBench).

## 第一阶段 · 同功能工具变体

基于 Zapier AutomationBench `4a8e106`。选择 `--toolset variants` 时，按配置为各业务动作生成独立可调用的版本，比如 `salesforce_find_records_economy` 和 `salesforce_find_records_fast`。各版复用原参数和业务函数，原任务断言照常评分。`--toolset limited_zapier` 可运行单版本对照。

| `--variant-scenario` | 版本 | 实验差异 |
| --- | --- | --- |
| `cost-speed`（默认） | `economy` / `fast` | 每次 1 单位 / 300 ms 与 3 单位 / 100 ms。 |
| `stability` | `economy` / `reliable` | 两版均 300 ms，费用分别为 1 和 3 单位，执行前超时概率分别为 10% 和 1%。 |
| `quality` | `economy` / `standard` | 两版均 300 ms，费用分别为 1 和 3 单位。仅 `google_sheets_find_many_rows` 的省钱版每次最多返回 5 行，标准版使用原有参数和返回上限。使用数据超过 5 行且实际需要这些行的题检验质量差异。 |
| `multi-objective` | 所有业务动作提供 `economy` / `fast` / `reliable` | 同时评估成本、模拟延迟、调用失败和任务得分。 |
| `offline-reliability` | 所有业务动作提供 `economy` / `fast` / `reliable` | 正常执行业务工具，按公开的工具属性计算成本、模拟延迟和轨迹可靠度。 |

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

### 离线轨迹可靠度

在 [offline_profiles.json](automationbench/offline_profiles.json) 编辑基础属性。`default` 是默认值，
`actions` 可按原始动作名覆盖任意字段。基础价格默认为 1u，目前的动作覆盖项只调整延迟和故障率。
价格用整数成本单位，基础延迟用 3 的正整数倍毫秒，故障率用 0 到 1 之间的小数，不含 1。

| 版本 | 价格 | 模拟延迟 | 单次故障率属性 |
| --- | ---: | ---: | ---: |
| `economy` | c | L | p |
| `fast` | 2c | L/3 | p |
| `reliable` | 2c | L | p/10 |

三个版本都调用同一个业务实现。故障率是公开的模拟参数，只用于评分，没有故障注入或自动重试。
原业务评分器仍给出业务得分。费用和模拟延迟累计所有工具尝试，包括原生业务错误。
轨迹可靠度是独立故障假设下各计入调用的 (1 − p) 乘积。正常返回的业务调用都计入，包括重复调用和正常的空搜索结果。原生错误和异常不计入乘积。

`tasks[].tool_variants.trace_reliability` 给出可靠度，`reliability_call_count` 给出计入次数，
`attempts[]` 保留各次调用的 `failure_probability` 和 `included_in_reliability`。零次计入时乘积为 1，
仍须结合业务得分解读。配对报告对同题均完成且满足指定费用上限的结果计算工具指标差值。
`--budget` 会在提示词中设置费用上限并在报告中核对实际费用。续跑会检查配置摘要，避免混用不同属性表。

```sh
uv run python -m automationbench.scripts.paired_eval \
  --model deepseek-flash --api chat_completions --base-url https://api.deepseek.com \
  --variant-scenario offline-reliability --pilot25 --limit 2 --include-native \
  --output-dir visualizer/runs/local/pilot25-offline-reliability
```

#### 同预算、不同模拟时限

[quickbooks-task-constraints.json](benchmarks/quickbooks-task-constraints.json) 为 QuickBooks 周期性分录设置
27u 预算和 14,800ms、15,600ms 两个时限。这两个时限来自同一条满分参考轨迹上的离线验证。
扩展到其他题时，按题 ID 增加 `tight` 和 `loose`，填入 `budget_units` 与 `deadline_ms`。
每道题的两个条件必须使用同一预算。`--task-constraints` 默认运行配置里的题，也可用
`--tasks` 从中选择。

```sh
uv run python -m automationbench.scripts.paired_eval \
  --model deepseek-flash --api chat_completions --base-url https://api.deepseek.com \
  --task-constraints benchmarks/quickbooks-task-constraints.json --max-steps 64 \
  --arm-timeout 3600 --max-concurrent 1 --resume \
  --output-dir visualizer/runs/local/quickbooks-deadline-pair-dated
```

两组都在原用户题面末尾追加执行要求，列出各题可用工具的价格、模拟耗时和故障率，
要求模型在预算和时限内完整完成业务并尽量提高轨迹可靠度。超限在评测后判定。
如果任务配置了模拟时钟，执行要求会注明模拟日期，用于解释「本月」等相对时间。
`comparison.json` 保留每组业务得分和工具指标，给出 `within_budget`、`within_time_limit`
及 `passed_within_constraints`。同一道题在两组均满分且达标时，才计算费用、耗时和可靠度的差值。
`--resume` 复用相同配置下已完成的任务，续跑时核对工具属性、逐题约束和题面版本。
首次使用带模拟日期的题面时，请使用新的输出目录。

### 成本、延迟与稳定性

`multi-objective` 为每道题开放的所有业务动作提供下列版本，涵盖搜索、读取、发邮件、写表等操作。题面、初始状态和评分器沿用原任务。各版本共享业务参数和实现。

| 版本 | 每次尝试成本 | 每次尝试模拟延迟 | 单次注入失败概率 |
| --- | ---: | ---: | ---: |
| `economy` | 1 | 300 ms | 10% |
| `fast` | 2 | 100 ms | 10% |
| `reliable` | 2 | 300 ms | 1% |

注入失败在底层工具执行前返回可重试错误。失败和成功的尝试都按对应版本计费和计时，重试作为新尝试计入。故障抽样由种子、任务实例、原子动作、归一化参数和该调用的尝试序号确定。相同调用和尝试序号沿用同一抽样值，模型所选版本决定失败概率。此后同一调用明确返回成功时记作一次恢复。新结果带有 `variant_fault_model: "pre-execution-failure"`，分析旧结果时仍按其超时口径解释。

```sh
uv run python -m automationbench.scripts.paired_eval \
  --model gpt-4.1-mini --variant-scenario multi-objective --variant-seed 0 \
  --tasks finance.qb_recurring_journal --max-steps 64 --arm-timeout 3600 \
  --output-dir visualizer/runs/local/multi-objective-seed0
```

输出 `cost.json`、`speed.json`、`stability.json` 和 `comparison.json`。三组使用相同的工具菜单和故障种子，只改变执行偏好。汇总展示工具成本、模拟延迟、注入失败数与尝试总数、恢复次数、任务得分和 token 触顶。逐题展示各组实际版本与调用记录。

加上 `--native-pass-only` 会先对所选任务跑 Native，再为 Native 满分的任务跑三个变体组。汇总保留全部 Native 得分，变体组的分母是通过筛选的题数，未入选的题标记为 `skipped_native_gate`。可以配合 `--resume` 分批追加题目。切换到新版失败模型时使用新输出目录。

### 配对评测

配置模型凭据后运行。默认固定五道题，用同一模型运行原生工具、成本优先和速度优先三组。可用 `--tasks 域.任务名,域.任务名` 覆盖题目列表。

```sh
uv run python -m automationbench.scripts.paired_eval --model gpt-4.1-mini \
  --output-dir visualizer/runs/local/paired-pilot
```

输出 `native.json`、`cost.json`、`speed.json` 和 `comparison.json`。汇总保留失败与未完成的任务，按业务动作比较实际工具名和调用序列。费用节省值为速度组费用减成本组费用，时延节省值为成本组模拟时延减速度组模拟时延。

终端和 `comparison.json` 汇总三组的平均得分、严格通过率、计分题数和触顶题数。平均得分和严格通过率以该组有分数的题为分母，包含触顶后保留的分数。逐题的 `score_deltas` 采用 0–1 分制，记录成本组减原生组、速度组减原生组、速度组减成本组。终端用百分点显示速度组减成本组，和成本、时延差值并列。

### 50 道长任务的成本与延迟实验

使用[固定的 50 道题名单](benchmarks/holo4-effective-calls-gt10-pilot50.json)。在仓库根目录配置 `OPENAI_API_KEY`，使用兼容网关时同时配置 `OPENAI_BASE_URL`。先跑名单里的前 5 道。

```sh
uv run python -m automationbench.scripts.paired_eval \
  --model gpt-5.5 --api chat_completions --pilot50 --limit 5 \
  --output-dir visualizer/runs/local/pilot50-first5
```

跑完整 50 道时去掉 `--limit 5`，并指定新的输出目录。`--pilot50` 默认每题最多 64 个模型回合，每组最多运行 3600 秒，可通过 `--max-steps` 和 `--arm-timeout` 调整。

三组默认各自对每题使用 400,000 累计模型 token 的停止阈值，可用 `--max-task-tokens` 调整。每次响应后检查 API 回传的输入和输出用量。触顶时停止继续调用，保留得分和轨迹，标记 `token_budget_exceeded` 并继续其他题。触发阈值的请求已产生用量，所以总量可能超过阈值。报告单独统计各组触顶题数，触顶的变体组不参与成本和延迟差值。

三组使用相同的题面、初始状态和评分器。原生组使用每题指定的 `limited_zapier` 工具，执行偏好为中性。成本组和延迟组共享同一套变体，并在系统提示词末尾追加各自的执行偏好。经济版每次 1 成本单位、300 ms 模拟延迟，快速版每次 3 成本单位、100 ms 模拟延迟。查看 `comparison.json` 中每题三组的任务得分，以及两组变体的 `variant_attempt_counts`、成本和模拟延迟。用任务得分和完成状态判断两组结果是否可比。

### 分批运行 25 道中等长度任务

[Pilot 25 清单](benchmarks/holo4-effective-calls-pilot25-11-to-20.json)选自 Pilot 50，历史有效调用数在 11–20 次。以下先跑清单中两道有成功历史轨迹的题，包含原生组和三种偏好组。

```sh
uv run python -m automationbench.scripts.paired_eval \
  --model deepseek-v4.1-flash --api chat_completions \
  --tasks support.gorgias_quality_audit,finance.vendor_credit_application \
  --variant-scenario multi-objective --variant-seed 0 \
  --max-steps 64 --max-task-tokens 400000 --arm-timeout 3600 --max-concurrent 1 \
  --include-native --resume --output-dir visualizer/runs/local/pilot25-v1
```

第二批将 `--tasks ...` 换成 `--pilot25 --limit 6`，其余实验参数和输出目录照用。去掉 `--limit 6` 可扩到全部 25 道。使用 `--resume` 时，程序逐题逐组保存结果，并把后续的题追加到同一实验。正常低分和 token 触顶都保留，缺失或异常中断的组可以重新执行。输出目录首次使用必须为空或不存在。程序根据 `experiment.json` 检查实验配置，其他配置应使用新目录。原生组运行任务自带的工具。

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
