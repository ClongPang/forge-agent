# Forge Agent

Forge Agent is a secure, auditable coding-agent runner for local and
self-hosted workflows.

Forge Agent 是一个安全、可审计、可控的 coding agent 执行器。它以一次
`run` 为核心执行单元，在明确的仓库边界内运行代码任务，并通过策略引擎、
Docker 沙箱、事件日志、结构化报告和 diff artifact，让 AI 修改代码这件事
变得可检查、可回放、可治理。

当前项目仍支持交互式 `chat`，但主入口是 `run`：适合批处理、CI、评测、
团队内受控执行和后续自动化集成。

---

## 快速开始

```bash
# 安装
git clone <repo-url>
cd forge-agent
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 配置模型 Key（示例）
export DEEPSEEK_API_KEY=sk-xxx

# 在目标仓库中执行一次受控任务
forgeagent run --repo /path/to/project --task "fix the failing tests" --sandbox

# 查看本次运行日志
forgeagent log list
forgeagent log show logs/<task_id>_<timestamp>.jsonl
```

默认配置在 `config/default.yaml`。支持 Anthropic、OpenAI、DeepSeek、
Groq、Ollama 以及 OpenAI-compatible endpoint。

---

## 核心定位

Forge Agent 不主打“另一个聊天式写代码助手”，而主打 agent 执行层：

- **明确输入输出**：一次 `run` 接收一个任务、一个仓库，返回 `RunResult`、结构化报告和 patch。
- **稳定运行产物**：每次运行生成固定命名的 `events.jsonl`、`report.json`、`diff.patch`。
- **内置权限模式**：`--mode inspect|fix|maintain` 用代码强制只读、修复、维护三类执行边界。
- **验证闭环**：`--verify CMD` 在 agent 结束后执行显式验证，并写入 `report.json`。
- **策略先行**：工具调用先经过参数准备、策略判断、确认，再执行。
- **工作区边界**：文件、搜索、测试、git 和可识别 shell 路径默认限制在 repo 内。
- **敏感信息保护**：拒绝读取 `.env`、私钥、`.git/config`、运行日志等敏感文件。
- **高风险动作确认**：写依赖文件、提交、危险 shell 命令等动作可拒绝或人工确认。
- **沙箱执行**：`--sandbox` 将 shell、pytest、git 放进 Docker 容器，默认断网。
- **可审计日志**：每次运行写入 append-only JSONL event log，记录 action、policy decision、observation、reflection 和最终状态。

---

## 主入口：run

`run` 是推荐主入口，适合明确、可验证、可审计的任务。

```bash
# 当前目录执行
forgeagent run --task "fix the failing tests"

# 只读分析
forgeagent run --mode inspect --task "inspect the project architecture"

# 指定仓库
forgeagent run --repo /path/to/project --task "add a health check endpoint and tests"

# 从文件读取复杂任务
forgeagent run --repo . --task-file task.txt

# 高风险动作需要终端确认
forgeagent run --repo . --mode maintain --task "update dependencies and run tests" --confirm

# Docker 沙箱执行
forgeagent run --repo . --task "run the test suite and fix failures" --sandbox

# 显式验证，并在验证失败或未验证时返回非零退出码
forgeagent run --repo . --task "fix the failing tests" \
    --verify "pytest" --fail-on-unverified

# 本地 Core Benchmark：自动跑一组 run-mode 回归样例
forgeagent eval run-core
```

每次 `run` 都会生成事件日志和 artifact 目录：

```text
logs/<task_id>_<timestamp>.jsonl
logs/<task_id>_<timestamp>/
  events.jsonl
  report.json
  diff.patch
```

后续可以用 `forgeagent log show` 审计，也可以让 CI 或脚本直接读取
`report.json`。

---

## 辅助入口：chat

`chat` 是交互式探索入口，适合本地边问边改。它复用同一个 `Agent.run()`，
但在 `ChatSession` 中跨轮保留对话历史，并默认开启终端确认。

```bash
forgeagent chat --repo /path/to/project
forgeagent chat --repo . --model deepseek-v4-pro --sandbox
forgeagent chat --repo . --mode maintain
```

对话内命令：

- `/exit`：退出
- `/stats`：查看累计轮次、步数、token
- `/clear`：清空会话历史
- `/help`：显示帮助

---

## 高级与实验入口

这些入口保留，但不是当前主线。

### GitHub Issue 自动修复

```bash
export GITHUB_TOKEN=ghp_xxx
python -m entry.github_issue \
    --repo owner/repo --issue 42 --local-path /tmp/myrepo
```

流程：拉取 Issue、准备本地仓库、创建分支、运行 agent、可选 push 并创建 PR。
这属于工作流集成，默认应在受控仓库或临时 checkout 中使用。

### SWE-bench Predictions

```bash
pip install -e ".[swebench]"
python -m entry.swebench generate \
    --split dev --limit 1 \
    --output runs/swebench/dev_predictions.jsonl
```

该入口只生成官方 harness 需要的 `predictions.jsonl`，不负责评分。

### Langfuse 回归数据集

```bash
forgeagent eval add-trace TRACE_OR_URL --dataset forge-agent/regression
```

用于把失败 trace 沉淀为后续回归样本。

### Local Core Benchmark

```bash
forgeagent eval run-core
forgeagent eval run-core --suite medium
forgeagent eval run-core --suite all --list-cases
```

该入口会创建临时样例仓库，调用现有 `run` 命令，并根据 `report.json`
检查退出码、verification、permission mode、changed files 和 artifact。

默认 `smoke` suite 包含快速 runner 回归 case；`medium` suite 包含更接近
日常开发任务的本地仓库 case。case 可以单独启动：

```bash
forgeagent eval run-core --case basic_python_fix
forgeagent eval run-core --case multi_file_python_fix
forgeagent eval run-core --case inspect_readonly
forgeagent eval run-core --case fail_on_unverified
forgeagent eval run-core --case verification_guard
forgeagent eval run-core --case no_test_cheating
forgeagent eval run-core --case existing_tests_must_stay_green
forgeagent eval run-core --case multi_file_indirect_call
forgeagent eval run-core --case config_override_priority
forgeagent eval run-core --case cli_exit_code_bug
forgeagent eval run-core --case path_normalization_security
forgeagent eval run-core --case parser_edge_cases
forgeagent eval run-core --case minimal_patch_required
```

---

## 项目结构

```text
agent/              # ReAct loop、任务结构、事件日志、遥测
  core.py           # Agent.run() 主循环
  tool_execution.py # 工具调用准备、策略、确认、执行、日志编排
  task.py           # Task / Action / Observation / RunResult / Event
  event_log.py      # append-only JSONL event log

policy/             # 工具调用策略引擎
  engine.py
  types.py

tools/              # agent 可调用工具
  base.py           # BaseTool / ToolRegistry / PreparedToolCall
  file_tool.py      # file_read / file_view / file_write
  shell_tool.py     # shell execution and shell safety checks
  test_tool.py      # pytest runner
  git_tool.py       # git status / diff / add / commit
  runtime.py        # LocalRuntime / DockerRuntime

context/            # repo-map、token budget、history
llm/                # Anthropic 和 OpenAI-compatible backend
entry/              # CLI、chat、GitHub Issue、SWE-bench 入口
config/             # YAML 配置加载与校验
tests/              # pytest 测试
docs/               # 设计和问题分析文档
examples/           # 示例策略和任务配置
```

---

## 安全机制

当前默认策略覆盖这些行为：

- 阻断明显破坏性命令：`rm -rf /`、`mkfs`、`dd if=`、fork bomb 等。
- 默认拒绝读取敏感文件：`.env`、`.env.*`、`*.pem`、`*.key`、`.git/config`、`logs/*.jsonl`。
- 搜索工具跳过敏感路径和运行日志。
- 写入高风险文件需要确认：依赖配置、CI workflow、部署/发布脚本等。
- `git_add` 必须传显式路径，拒绝 `git add .`。
- `git_commit` 需要确认。
- `fix` 是默认 run 模式：允许普通代码/测试修改，依赖安装、原始网络命令和 docker 会被拒绝。
- `maintain` 用于依赖和 CI 维护：依赖安装仍需要 `--confirm` 人工确认。
- `inspect` 是只读模式：拒绝文件写入、测试执行、stage/commit 和非只读 shell。
- `--verify` 是用户/CI 显式命令，适合 `pytest`、`npm test`、`go test ./...`、`cargo test`、`ruff check` 等验证命令。
- 验证命令拒绝 shell 控制符、依赖安装、git 写操作、网络下载、sudo/docker、敏感文件和仓库外路径。
- 没有确认回调的非交互 `run` 会 fail closed。
- `--sandbox` 通过 Docker 执行命令，容器默认断网。

策略文件不是当前主入口。它被保留为未来高级治理能力，用于团队级 hard deny
和默认权限模式。设计见 [docs/policy.md](docs/policy.md)，示例见
[examples/forge-policy.yaml](examples/forge-policy.yaml)。

---

## 当前改进顺序

当前主线不做 Task Contract。优先级是：

1. **Run Artifact**：已实现，稳定产出 `events.jsonl`、`report.json`、`diff.patch`。
2. **Permission Mode**：已实现，支持 `--mode inspect|fix|maintain`。
3. **验证闭环**：已实现，支持 `--verify`、`--verify-timeout`、`--fail-on-unverified`。

`forge-policy.yaml` 放到这些能力之后，作为高级团队配置接入。

---

## 开发

```bash
pip install -e ".[dev]"

# 运行测试
pytest
pytest tests/test_policy_engine.py
pytest --cov

# 直接从源码测试 CLI
python -m entry.cli run --repo . --task "inspect the project"
python -m entry.cli log list
```

可选依赖：

```bash
# 更多 tree-sitter 语言支持
pip install tree-sitter-javascript tree-sitter-typescript \
            tree-sitter-go tree-sitter-rust tree-sitter-java

# 精确 token 计数
pip install tiktoken

# SWE-bench prediction 生成
pip install -e ".[swebench]"
```

---

## 命令参考

```bash
# main runner
forgeagent run --task TEXT [--repo PATH] [--task-file FILE]
          [--provider PROVIDER] [--model MODEL]
          [--mode inspect|fix|maintain] [--max-steps N]
          [--verify CMD] [--fail-on-unverified]
          [--stream] [--confirm] [--sandbox] [-v]

# interactive exploration
forgeagent chat [--repo PATH] [--provider PROVIDER] [--model MODEL]
          [--mode inspect|fix|maintain] [--max-steps N] [--sandbox] [-v]

# audit logs
forgeagent log list [--dir DIR]
forgeagent log show LOG_FILE

# advanced / experimental
forgeagent eval add-trace TRACE_OR_URL [--dataset DATASET]
python -m entry.github_issue -r owner/repo -i ISSUE_NUM -l LOCAL_PATH [--no-pr]
python -m entry.swebench generate --split dev --limit 1 --output predictions.jsonl
```

详细用法见 [USAGE.md](USAGE.md)。
