# Run Mode Core Changes Analysis

日期：2026-07-19

本文记录本轮围绕 Forge Agent 核心定位所做的代码修改、背后的产品问题、设计取舍、测试结论和后续风险。

## 背景问题

Forge Agent 当前处在 coding agent 赛道，和 Aider、SWE-agent、OpenHands、Claude Code 等项目在目标上有重叠。如果继续强调“让模型自动改代码”，差异度不够高，也很难和成熟产品直接竞争。

本轮讨论后确定的核心方向是：

> Forge 不优先做一个更聪明的聊天式 coding agent，而是主推可控、可验证、可审计的 coding-agent runner。

这意味着产品重点从“模型回答得好不好”转向：

- 一次任务是否有明确执行边界。
- 执行过程是否可复查。
- 修改结果是否由命令验证，而不是只由模型自述。
- 不同风险任务是否能选择不同权限模式。
- 结果是否适合进入自动化流水线或团队评审。

## 已放弃或后置的设计

### Task Contract

最初考虑过在每次任务前生成 Task Contract，用来描述本次任务的执行边界。但进一步分析后认为它不适合作为当前核心入口。

主要问题：

- 每个任务执行前无法准确预测完整命令序列。
- 资深用户会把它视为额外流程负担。
- 它容易变成一份“模型生成的计划”，但实际安全边界仍需要运行时策略保证。
- 如果用户每次都需要确认和维护 contract，产品会显得比 Claude Code 更重，但核心收益不够明确。

结论：

- Task Contract 不作为当前主线能力。
- 任务边界改由内置 permission mode 和运行时策略表达。

### `forge-policy.yaml`

曾经考虑引入 `forge-policy.yaml`。但它不应该用来预测某次任务会执行哪些命令，也不应该成为普通用户每次运行前必须编辑的文件。

当前结论：

- `forge-policy.yaml` 后置为高级团队治理能力。
- 它只适合表达长期稳定规则，例如默认模式、敏感文件、永不允许的命令、需要确认的维护操作。
- 当前运行时不依赖该文件。
- 已在 `docs/policy.md` 中记录为未来设计方向。

## 本轮核心改动

### 1. Run Artifact

新增 `agent/run_artifacts.py`，在每次 `run` 后生成稳定产物：

- `events.jsonl`：本次 agent 运行事件流。
- `report.json`：结构化报告，便于机器读取。
- `diff.patch`：本次任务产生的代码差异。

`report.json` 记录：

- task
- result status
- permission mode
- verification status
- changed files
- tool calls
- duration
- policy summary
- artifact paths

解决的问题：

- 用户不需要只依赖终端输出判断任务发生了什么。
- 资深用户可以复查 diff、工具调用和验证结果。
- 后续可以接入 CI、benchmark 或 dashboard。

### 2. Permission Mode

新增 `PermissionMode`：

- `inspect`
- `fix`
- `maintain`

相关文件：

- `policy/types.py`
- `policy/engine.py`
- `policy/__init__.py`
- `entry/cli.py`
- `entry/chat.py`

模式语义：

| Mode | 适用场景 | 行为边界 |
| --- | --- | --- |
| `inspect` | 只读分析 | 拒绝写文件、测试工具调用、git add、git commit、普通 shell 写操作 |
| `fix` | 常规代码修复 | 允许正常代码改动和测试；拒绝依赖安装、外部网络、git push、sudo、docker |
| `maintain` | 仓库维护 | 允许更宽的维护场景；依赖安装走确认；高风险远程或系统操作仍拒绝 |

设计取舍：

- 用少量固定模式覆盖大多数任务，而不是引入复杂的 per-task policy。
- 模式是运行时策略，不是 prompt 约定。
- 危险动作由工具执行前的策略层判断，不能只靠模型自觉。

### 3. Verification Loop

新增 `agent/verification.py`，支持 `run` 结束后执行显式验证命令。

CLI 新增参数：

- `--verify TEXT`：可重复传入多条验证命令。
- `--verify-timeout INT`：单条验证命令超时时间。
- `--fail-on-unverified`：当验证未通过时让整个 run 返回失败退出码。

验证报告写入 `report.json`。

安全边界：

- verification 只接受单条命令。
- 拒绝 shell control，例如 `&&`、`|`、`;`、重定向、命令替换。
- 拒绝明显非验证类操作，例如安装依赖、git push、curl、wget、sudo、docker、rm、mv、chmod、chown。
- 多条验证应使用多个 `--verify`，而不是 shell 拼接。

解决的问题：

- 模型不能只靠“我已经完成”结束任务。
- 用户可以把验收标准变成可执行命令。
- 自动化场景可以依赖退出码判断任务是否可接受。

### 4. CLI Integration

`entry/cli.py` 中 `run` 模式现在支持：

- `--mode / --permission-mode`
- `--verify`
- `--verify-timeout`
- `--fail-on-unverified`
- 运行结束后打印 artifact 路径和 verification 摘要

`chat` 模式也支持 `--mode / --permission-mode`，但它不是当前主推入口。

本轮产品判断：

- `run` 是主线，因为它更适合自动化、验证、产物归档和后续 benchmark。
- `chat` 保留为辅助入口，不作为差异化重点。

### 5. Documentation And Evaluation

更新或新增：

- `README.md`
- `USAGE.md`
- `docs/policy.md`
- `EVAL_GUIDE.md`

其中 `EVAL_GUIDE.md` 是当前手工评测手册，用来验证：

- CLI 参数是否存在。
- 单元测试是否通过。
- `fix` 模式是否能修复样例仓库。
- `inspect` 模式是否保持只读。
- verification 失败是否能影响退出码。
- artifact 是否记录完整结果。

用户已按该文档完成评测，结果为完全通过。

## 代码复杂度与冗余分析

本轮曾经存在的过度设计风险：

- 为了支持未来企业策略，过早引入 `forge-policy.yaml`。
- 为了“任务边界清晰”，引入每次任务都要生成的 Task Contract。
- 为了兼容复杂命令，放宽 verification 命令执行能力。

本轮收敛后的处理：

- 没有把 `forge-policy.yaml` 接入运行时。
- 去掉 Task Contract 主线。
- permission mode 保持三档，不继续拆更多模式。
- verification 命令保持严格，只服务验证，不承担任意脚本执行。
- run artifact 独立成小模块，避免把报告生成逻辑堆进 CLI。

仍需关注的复杂度：

- `entry/cli.py` 承担了较多流程编排，后续如果继续扩展 eval、artifact、verification，可能需要拆出 run orchestration 层。
- `policy/engine.py` 的模式规则目前写在代码中，简单直接，但未来团队治理能力增加时可能需要更清晰的规则组合模型。
- `agent/verification.py` 的命令校验偏保守，短期有利于安全，长期可能需要提供白名单扩展机制。

## 测试结果

本轮新增和更新了测试：

- `tests/test_run_artifacts.py`
- `tests/test_verification.py`
- `tests/test_policy_engine.py`
- `tests/test_confirm.py`
- `tests/test_day6.py`
- `tests/test_chat.py`

已执行全量测试：

```bash
.venv/bin/pytest -q
```

结果：

```text
537 passed, 7 skipped
```

手工评测：

- 已按照 `EVAL_GUIDE.md` 完整执行。
- 结果完全通过。

## 当前结论

本轮修改后，Forge 的核心差异点可以更明确地表达为：

> 一个面向自动化和工程审计的 coding-agent runner，重点是权限边界、验证闭环和可复查产物。

这和 Claude Code 这类交互式编码产品的区别不是“更会聊天”或“模型更强”，而是：

- 输出不是一次聊天回答，而是一组可审计 artifacts。
- 成功不是模型声明，而是 verification 命令结果。
- 风险不是靠用户临时判断，而是由 permission mode 提前选择。
- 任务更容易接入脚本、CI、本地批处理和后续 benchmark。

## 仍然存在的问题

### 1. 还没有固定 benchmark

`EVAL_GUIDE.md` 证明机制可用，但它仍是手工评测。

下一步应该把手工样例固化为自动化 benchmark，例如：

```bash
python -m entry.cli eval --suite core
```

每个 eval case 应记录：

- 初始仓库文件。
- 用户 task。
- permission mode。
- verification command。
- 预期允许修改的文件。
- 预期 artifact 字段。

### 2. 端到端效果仍受模型影响

如果模型修不动简单 bug，Forge 的 runner 机制可能仍是正确的，但用户体验会失败。

后续需要区分两类指标：

- runner 指标：权限、验证、artifact、退出码是否正确。
- model-task 指标：模型是否完成真实代码任务。

### 3. Permission Mode 的用户心智仍需打磨

三档模式比 policy 文件轻，但仍需要用户理解：

- inspect 是只读分析。
- fix 是默认修复模式。
- maintain 是高级维护模式。

后续可以在 CLI 输出中更明确展示当前模式影响，而不是只显示模式名。

### 4. Verification 命令严格但不够灵活

当前拒绝 shell control 是合理的安全默认，但资深用户可能习惯写：

```bash
pytest && ruff check .
```

当前推荐方式是：

```bash
--verify "pytest" --verify "ruff check ."
```

后续可以考虑提供项目级白名单或 `forge eval` 中的结构化命令数组，但不建议为了便利直接放开 shell 拼接。

### 5. Artifact Schema 需要稳定化

`report.json` 已经可用，但如果未来要被 CI 或 dashboard 消费，需要进一步明确：

- schema version 兼容策略。
- 字段是否允许缺失。
- changed files 的计算口径。
- verification blocked / failed / timeout 的语义。

## 建议下一步

优先级从高到低：

1. 新增 `evals/` 目录，把当前手工评测转为自动化 core benchmark。
2. 新增 `forgeagent eval --suite core` 或 `python -m entry.cli eval --suite core`。
3. 固化 `report.json` schema，并为 artifact 增加 schema 测试。
4. 改善 CLI 输出，让用户一眼看到 mode、verification、artifact、exit status。
5. 再考虑高级 `forge-policy.yaml`，但只用于团队治理，不用于普通任务执行。

