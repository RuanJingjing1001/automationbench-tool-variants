# 第一阶段：完整成功的固定工作流

本数据集用于在**业务路径和参数已确定**的条件下研究 API 版本分配。包含真实执行并通过原环境评分器的 50 条工作流，没有用题目名单、动作名称列表或手写模拟轨迹代替成功执行记录。

| 来源 | 开发集 | 正式评测集 | 合计 |
| --- | ---: | ---: | ---: |
| AutomationBench / Holo4 | 6 | 24 | 30 |
| AppWorld / 官方参考解 | 4 | 16 | 20 |
| 合计 | 10 | 40 | 50 |

| 有效业务调用 | AutomationBench | AppWorld | 合计 |
| --- | ---: | ---: | ---: |
| 11–15 | 12 | 8 | 20 |
| 16–25 | 12 | 8 | 20 |
| 26–40 | 6 | 4 | 10 |

每个任务保留一条工作流；AppWorld 同模板的不同实例放在同一划分。AutomationBench 开发集覆盖六个业务领域。AppWorld 的数据源仅为官方 train/dev，本文的 dev/test 是本实验重新划分的开发/评测集，**并非 AppWorld 官方测试集**。比例和长度配额是起步设计，不是最优配比的实证结论。

## 文件

- `manifest.json`：统一的 50 条样本索引、划分、长度、来源、载荷位置及内容哈希。
- `summary.json`：实际数量、长度分布和验证范围。
- `splits/dev.json`、`splits/test.json`：固定 10/40 任务划分。
- `automationbench/*.json`：30 条完整载荷，包含初始状态、原始断言、完整公开轨迹、固定业务调用及两次重放记录。
- `appworld.bundle`：20 条 AppWorld 载荷及完整筛选审计，按官方加密格式发布。解包后包含指令、参考解代码、完整调用参数/返回值、两次重放及官方测试结果。
- `automationbench_audit.json`：已检查的 Holo 候选及通过/未通过原因。
- `appworld_audit_summary.json`：AppWorld 筛选汇总，逐任务审计位于加密包。
- `checksums.json`：数据文件的 SHA-256。
- `smoke_validation.json`：10 条开发样本的混合版本标签重放结果。

## 获取与验证

### AutomationBench

Holo 来源固定为 `Hcompany/trajectories` 的 `ede2a362c99ddba0d03f439c47a0b8fea6abae22`。从仓库完整的 >10 调用候选清单获取成功轨迹，逐领域检查，恢复本仓库当前任务初始状态，用本仓库 `api_fetch` 执行所有业务尝试，再用 `partial_credit` 原评分器验收。每条执行两次，两次均须满分；公开来源声称成功不能替代本地验收。

`task_snapshot` 固定了题面、初始状态和断言。`environment_version.repository_commit` 指向采集时的仓库代码。原始调用中失败的尝试也保留并执行；工具发现与编码助手保留在 `source_trace`，业务重放使用已经具体化的 API 参数。

在仓库根目录，使用 Python 3.13 的独立环境：

```sh
uv venv .venv-phase1-ab --python 3.13
uv pip install --python .venv-phase1-ab/bin/python -r datasets/phase_one/requirements-automationbench.lock.txt
.venv-phase1-ab/bin/python datasets/phase_one/manage.py replay \
  --environment automationbench --split dev
```

重新采集（下载公开轨迹，不调用模型）：

```sh
.venv-phase1-ab/bin/python datasets/phase_one/collect_automationbench.py
```

### AppWorld

固定使用 PyPI `appworld==0.1.3.post1` 与官方 `data-0.1.0`。参考解先通过官方验收，再从新的任务初始状态重放捕获到的 API 调用两次。认证、supervisor 完成任务等步骤仍全部执行；登录产生的新令牌按输出依赖绑定到后续请求，业务参数保持固定。

AppWorld 的依赖与 AutomationBench 不兼容，请使用 Python 3.11 的独立环境。以下下载命令针对新建目录；AppWorld 的官方下载器会替换目标目录中已有的 `data`。

```sh
uv venv .venv-phase1-aw --python 3.11
uv pip install --python .venv-phase1-aw/bin/python -r datasets/phase_one/requirements-appworld.lock.txt
.venv-phase1-aw/bin/appworld install
export APPWORLD_ROOT="$PWD/data/phase-one-appworld-runtime"
.venv-phase1-aw/bin/appworld download data --root "$APPWORLD_ROOT"
.venv-phase1-aw/bin/python datasets/phase_one/manage.py verify
.venv-phase1-aw/bin/python datasets/phase_one/manage.py replay \
  --environment appworld --split dev
```

仅解包，默认写到被 git 忽略的 `data/phase-one/appworld`：

```sh
.venv-phase1-aw/bin/python datasets/phase_one/manage.py unpack
```

重新执行官方参考解、筛选与打包：

```sh
PYTHONHASHSEED=0 .venv-phase1-aw/bin/python datasets/phase_one/collect_appworld.py
```

两个来源完成后重新生成统一索引并校验配额：

```sh
.venv-phase1-ab/bin/python datasets/phase_one/manage.py build
```

## 计数与准入

有效长度只计成功业务 API。相同参数和相同结果的重复 GET/HEAD，在写操作之间只计一次；失败、发现、认证和辅助管理调用不计长度。AppWorld 的 supervisor/api_docs/admin 调用及 `/auth/` 调用作为辅助步骤保存。所有原始尝试都保留，计数去重不会删除执行步骤。后续实验应按实际执行尝试计费，不能按去重后的长度计费。

`metrics` 另记录总尝试、失败业务尝试、不同 API 数量和应用列表。API 身份优先匹配环境定义的端点模板；无法匹配时保留具体 METHOD/路径，不猜测接口身份。

准入要求：来源成功、两次原评分器满分、有效业务调用 11–40 次。保留身份版本分派的语义约定：所有版本调用同一个业务实现和相同参数，`dispatch_identity` 提供此约定的最小执行核。`variant_semantics_verified` **仅指此身份分派约定**，不代表已验证原有 quality 截断场景、故障注入或恢复策略。完整版本组合的任务表现仍需实验验收。

## 使用范围

这次交付包含基础工作流和重放验证，**尚未运行模型的 cost/speed/stability 版本选择实验**，没有把模拟费用、模拟延迟或错误率写成实测数据。`manage.py replay` 在 `economy/fast/reliable` 标签之间循环分派，用于检查业务执行语义；标签本身不注入费用、延迟和故障。现有仓库的 `multi-objective` 包装针对 Zapier 动作，本数据集包含 REST 调用和 AppWorld API，不能直接把其标签当成既有 Zapier 变体的端到端实验结果。

下一步在这些固定调用上定义明确的版本 profile、预算/时限约束与重试规则，再让模型输出每步的版本；独立记录模型选择开销和环境执行开销。可先用 10 条开发集调试，冻结设计后使用 40 条正式评测集。工作流参数、参考解、验收断言和参考返回值是否展示给版本选择器，应由实验协议明确规定；验收断言和答案不要自动拼入模型输入。

这些结果只支持已经存在成功工作流条件下的版本分配研究，不能推断完整原始任务集的端到端成功率。AppWorld 有多个同模板实例，虽已按组划分，50 条也不能解释成 50 个独立工作流模板。

## 来源与许可

- [Holo4 trajectories](https://huggingface.co/datasets/Hcompany/trajectories/tree/ede2a362c99ddba0d03f439c47a0b8fea6abae22)：发布卡标注 Apache-2.0，原轨迹由 H Company 发布，保留每条来源 URL、模型和内容哈希。
- [AutomationBench](https://github.com/zapier/AutomationBench)：Zapier，MIT；本仓库原有许可继续适用。
- [AppWorld](https://github.com/StonyBrookNLP/appworld#lock_with_ink_pen-license)：Apache-2.0；受保护内容及衍生数据公开再发布须加密。`appworld.bundle` 使用官方 `pack_bundle`，附带官方保护部分许可。请勿将解包后的内容以明文提交到公开仓库。

源基准的许可和限制分别适用于对应数据，不由本仓库整体许可替代。
