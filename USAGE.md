# Forge Agent 使用教程

Forge Agent 是一个安全、可审计、可控的 coding agent runner。推荐把一次
`run` 当作核心执行单元：输入一个任务和一个仓库，产出事件日志、结构化报告和
代码 diff artifact。

`chat`、GitHub Issue、SWE-bench 和 Langfuse eval 都是围绕这个执行单元的
辅助入口或高级入口。

---

## 目录

1. [安装](#1-安装)
2. [配置](#2-配置)
3. [主入口：run](#3-主入口run)
4. [运行产物：artifact](#4-运行产物artifact)
5. [审计入口：log](#5-审计入口log)
6. [辅助入口：chat](#6-辅助入口chat)
7. [安全机制](#7-安全机制)
8. [Docker 沙箱](#8-docker-沙箱)
9. [当前产品优先级](#9-当前产品优先级)
10. [高级和实验入口](#10-高级和实验入口)
11. [任务描述建议](#11-任务描述建议)
12. [常见问题](#12-常见问题)
13. [配置参考](#13-配置参考)

---

## 1. 安装

环境要求：Python 3.11+、pip。Docker 仅在使用 `--sandbox` 时需要。

```bash
git clone <repo-url>
cd forge-agent

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

forgeagent --help
```

可选依赖：

```bash
# 更多语言的 repo-map 支持
pip install \
  tree-sitter-javascript \
  tree-sitter-typescript \
  tree-sitter-go \
  tree-sitter-rust \
  tree-sitter-java \
  tree-sitter-cpp \
  tree-sitter-c \
  tree-sitter-ruby

# 精确 token 计数
pip install tiktoken

# SWE-bench prediction 生成
pip install -e ".[swebench]"
```

---

## 2. 配置

编辑 `config/default.yaml`，配置模型提供商和 API Key。

DeepSeek 示例：

```yaml
llm:
  provider: deepseek
  model: deepseek-v4-pro
  api_key: ${DEEPSEEK_API_KEY}
  base_url: https://api.deepseek.com
  max_tokens: 8192
```

常用 provider：

```yaml
# Anthropic
llm:
  provider: anthropic
  model: claude-sonnet-4-5
  api_key: ${ANTHROPIC_API_KEY}

# OpenAI
llm:
  provider: openai
  model: gpt-4o
  api_key: ${OPENAI_API_KEY}

# Ollama
llm:
  provider: ollama
  model: llama3
  api_key:
  base_url: http://localhost:11434/v1
```

不要把真实 key 写进仓库。推荐放在 shell 环境或 `.env`：

```bash
export DEEPSEEK_API_KEY=sk-xxx
export ANTHROPIC_API_KEY=sk-ant-xxx
export OPENAI_API_KEY=sk-xxx
```

验证基础联通：

```bash
python smoke_test.py
```

---

## 3. 主入口：run

`run` 是推荐主入口。它适合明确、可验证、可审计的单次任务。

```bash
# 当前目录执行一次任务
forgeagent run --task "fix the failing tests"

# 只读分析，不允许修改文件或执行测试
forgeagent run --mode inspect --task "inspect the project architecture"

# 指定目标仓库
forgeagent run --repo /path/to/project --task "add a health check endpoint and tests"

# 用任务文件描述复杂需求
forgeagent run --repo . --task-file task.txt

# 高风险动作需要终端确认
forgeagent run --repo . --mode maintain --task "update dependencies and run tests" --confirm

# Docker 沙箱执行
forgeagent run --repo . --task "run the test suite and fix failures" --sandbox

# 显式验证；验证失败或未验证时返回非零退出码
forgeagent run --repo . --task "fix the failing tests" \
  --verify "pytest" --fail-on-unverified
```

常用选项：

```text
-r, --repo TEXT       目标 repo 路径，默认当前目录
-t, --task TEXT       任务描述
-f, --task-file TEXT  从文件读取任务描述
-p, --provider TEXT   覆盖 provider
-m, --model TEXT      覆盖模型
    --mode, --permission-mode [inspect|fix|maintain]
                       本次 run 的权限模式，默认 fix
    --verify TEXT      agent 结束后运行的显式验证命令，可重复
    --verify-timeout INT
                       单条验证命令超时秒数，默认 300
    --fail-on-unverified
                       未提供验证或验证失败时返回非零退出码
    --max-steps INT   覆盖最大步数
-s, --stream          启用流式输出，当前默认开启
    --confirm         需要确认的动作进入终端确认
    --sandbox         在 Docker 沙箱中执行命令
-v, --verbose         显示 debug 日志
```

一次 `run` 的结果包括：

- `RunResult`：最终状态、步数、token、错误信息、patch
- `EventLog`：append-only JSONL，记录 task、action、policy、observation、reflection
- artifact 目录：固定命名的 `events.jsonl`、`report.json`、`diff.patch`
- 工作区 diff：agent 对 repo 的实际修改，也会写入 `diff.patch`
- verification：显式验证命令、退出码、状态、输出摘要
- 退出码：成功为 `0`，失败为 `1`

### 权限模式

`run` 当前支持三个内置 permission mode：

```text
inspect   只读分析。允许文件读取、搜索、git status/diff/log 等低风险查看；
          拒绝文件写入、测试执行、git add/commit 和非只读 shell。

fix       默认模式。允许普通代码/测试修改和测试运行；
          高风险文件写入、commit、未知写操作需要确认；
          依赖安装、git push、curl/wget、sudo/docker 会被拒绝。

maintain  维护模式。适合依赖、CI、配置维护；
          依赖安装从拒绝变为需要确认，git push、curl/wget、sudo/docker 仍拒绝。
```

依赖安装类任务建议显式使用：

```bash
forgeagent run --mode maintain --confirm \
  --task "update Python dependencies and run pytest"
```

### 验证闭环

`--verify` 用来声明 agent 完成后必须运行的验证命令。它可以重复：

```bash
forgeagent run --repo . --task "fix the failing tests" \
  --verify "pytest" \
  --verify "ruff check" \
  --fail-on-unverified
```

验证结果会写入 `report.json` 的 `verification` 字段。默认情况下，验证失败会被
记录和打印，但不会改变旧的 agent 退出码语义。加上 `--fail-on-unverified`
后，以下情况会返回非零退出码：

- 没有提供任何 `--verify`
- 任一验证命令失败、超时或被安全规则拦截
- agent 本身失败

验证命令是用户或 CI 显式声明的命令，不是模型生成的工具调用。Forge 仍会拦截：

- shell 控制符：`&&`、`||`、`;`、管道、重定向、命令替换
- 依赖安装：`pip install`、`npm install`、`poetry add` 等
- git 写操作：`git add`、`git commit`、`git push`
- 网络下载和特权命令：`curl`、`wget`、`sudo`、`docker`
- 敏感文件或仓库外路径引用

常见验证命令：

```bash
forgeagent run --task "fix tests" --verify "pytest"
forgeagent run --task "fix frontend tests" --verify "npm test"
forgeagent run --task "fix Go tests" --verify "go test ./..."
forgeagent run --task "fix Rust tests" --verify "cargo test"
forgeagent run --task "fix lint" --verify "ruff check"
```

推荐验证流程：

```bash
forgeagent run --repo . --task "fix the failing tests" --sandbox
forgeagent log list
forgeagent log show logs/<task_id>_<timestamp>.jsonl
cat logs/<task_id>_<timestamp>/report.json
git diff
```

---

## 4. 运行产物：artifact

每次 `run` 会保留原始 event log，同时生成一个同名 artifact 目录：

```text
logs/<task_id>_<timestamp>.jsonl
logs/<task_id>_<timestamp>/
  events.jsonl
  report.json
  diff.patch
```

固定文件名是给脚本、CI 和评测系统用的。

- `events.jsonl`：本次 run 的完整事件流副本。
- `report.json`：机器可读摘要，包含 task、result、stats、changed files、tool calls、policy summary、artifact 路径。
- `diff.patch`：本次 run 相对运行前 baseline 的 patch；没有新增 diff 时为空文件。

`report.json` 是后续自动化集成的主入口。当前已经包含：

```text
schema_version
task
result.status
result.steps_taken
result.total_tokens
result.has_patch
result.patch_chars
permission_mode
verification
stats
duration_seconds
changed_files
tool_calls
policy
artifacts
```

---

## 5. 审计入口：log

每次 `run` 或每轮 `chat` 都会在 `config.agent.log_dir` 下生成 JSONL 事件日志。

列出日志：

```bash
forgeagent log list
forgeagent log list --dir ./logs
```

查看摘要：

```bash
forgeagent log show logs/abc12345_20260719_120000.jsonl
```

日志会展示：

- 总事件数
- action 数
- reflection 数
- tool call 统计
- 最终状态
- 每个事件的时间、类型和关键状态

日志是标准 JSON Lines，可以用 `jq` 分析：

```bash
cat logs/abc12345_*.jsonl \
  | jq 'select(.event_type=="policy_decision") | .payload'
```

---

## 6. 辅助入口：chat

`chat` 用于本地交互式探索。它复用同一个 `Agent.run()`，但由 `ChatSession`
跨轮保存对话历史，并默认开启终端确认。

```bash
forgeagent chat --repo /path/to/project
forgeagent chat --repo . --model deepseek-v4-pro
forgeagent chat --repo . --sandbox
forgeagent chat --repo . --mode maintain
```

对话内命令：

```text
/exit   退出
/stats  查看累计轮次、步数、token
/clear  清空历史
/help   显示帮助
```

`chat` 适合边探索边改。需要可复现、可审计、可集成的任务时，优先写成
`task.txt` 后用 `run --task-file` 执行。

`chat` 默认同样使用 `fix` mode。需要安装依赖或维护 CI/依赖配置时，显式加
`--mode maintain`。

---

## 7. 安全机制

Forge Agent 当前有四类默认保护。

### 工作区边界

文件、搜索、测试、git 和可识别 shell 路径默认限制在 `--repo` 指定目录内。
仓库外路径需要确认；没有确认回调时会拒绝。

会拦截的常见情况：

- `/etc/hosts` 等绝对路径
- `../` 路径逃逸
- sibling-prefix 路径混淆
- 指向仓库外的 symlink

### 敏感文件保护

默认拒绝读取：

- `.env`、`.env.*`
- `*.pem`、`*.key`、`id_rsa*`
- `.git/config`、`.git-credentials`
- `logs/*.jsonl`

`.env.example`、`.env.sample`、`.env.template` 允许读取，用来查看变量名。

### Shell 风险分类

只读白名单命令可直接执行，例如：

```text
ls, cat, head, tail, pwd, find, rg, grep, git status, git diff,
git log, pytest, python -m pytest, wc, diff, tree
```

这些命令若显式引用仓库外路径或敏感文件，仍会被边界和敏感文件策略拦截。

不同 mode 下的高风险命令处理不同：

```text
fix:
  confirm: rm, mv, chmod, shell redirection, git commit, unknown write-like commands
  deny:    pip install, npm install, git push, curl, wget, sudo, docker, docker-compose

maintain:
  confirm: pip install, npm install, rm, mv, chmod, shell redirection, git commit
  deny:    git push, curl, wget, sudo, docker, docker-compose

inspect:
  allow:   low-risk read commands such as ls, rg, git status, git diff
  deny:    file writes, pytest/test execution, git add/commit, non-readonly shell
```

明显破坏性命令永远拒绝：

```text
rm -rf /, rm -rf ~, mkfs, dd if=, fork bomb, chmod -R 777 /, > /dev/sda
```

### Git 策略

- `git_status`、`git_diff` 是低风险查看操作。
- `git_add` 必须传显式路径，拒绝 `git add .`。
- 试图 stage 非本轮 agent 修改过的文件会要求确认。
- `git_commit` 始终要求确认。

---

## 8. Docker 沙箱

`--sandbox` 会把 shell、pytest、git 放到 Docker 容器里执行。

```bash
forgeagent run --repo . --task "run tests and fix failures" --sandbox
forgeagent chat --repo . --sandbox
```

沙箱行为：

- 使用 `python:3.11-slim`
- repo bind mount 到 `/workspace`
- 文件修改会同步回宿主机工作区
- 容器默认断网
- session 结束时清理容器

注意：沙箱保护命令执行环境，但 repo 是双向挂载。需要结合 git diff 和 event log
审计 agent 改了什么。

---

## 9. 当前产品优先级

当前主线不做 Task Contract，也不要求用户在每个任务前写 YAML。

优先级按下面顺序推进：

1. **Run Artifact**：已实现，每次 `run` 稳定产出 `events.jsonl`、`report.json`、`diff.patch`。
2. **Permission Mode**：已实现，支持 `--mode inspect|fix|maintain`，用代码强制不同运行边界。
3. **验证闭环**：已实现，支持 `--verify`、`--verify-timeout`、`--fail-on-unverified`，并把验证结果写入 `report.json`。

`forge-policy.yaml` 暂时不是主入口。它只作为未来高级治理能力保留，用于团队级
hard deny、默认 permission mode 和 CI 非交互规则。设计文档见
`docs/policy.md`，示例见 `examples/forge-policy.yaml`。

---

## 10. 高级和实验入口

### GitHub Issue 自动修复

```bash
export GITHUB_TOKEN=ghp_xxx
python -m entry.github_issue \
  --repo owner/repo \
  --issue 42 \
  --local-path /tmp/myrepo
```

参数：

```text
-r, --repo TEXT         GitHub repo，格式 owner/repo
-i, --issue INTEGER     Issue 编号
-l, --local-path TEXT   本地路径，已存在则复用
-c, --config TEXT       配置文件
    --no-pr             只修复，不创建 PR
    --base-branch TEXT  PR 目标分支，默认 main
-v, --verbose           debug 日志
```

该入口属于工作流集成。建议在临时 checkout 或受控仓库里使用。

### Local Core Benchmark

```bash
forgeagent eval run-core
forgeagent eval run-core --suite medium
forgeagent eval run-core --suite all --list-cases
forgeagent eval run-core \
  --case basic_python_fix \
  --case inspect_readonly \
  --output runs/core-eval/summary.json
```

该入口用于日常 agent 开发迭代。它会自动创建一组小型临时 git 仓库，
逐个调用现有 `run` 命令，然后读取 `report.json` 校验：

- 进程退出码
- permission mode
- verification 状态
- 是否产生预期 patch
- 实际 changed files 是否符合预期

默认 `smoke` suite 覆盖基础修复、多文件定位、inspect 只读、
`--fail-on-unverified` 退出码，以及 verification 命令安全边界。
`medium` suite 覆盖禁止改测试、保留既有行为、间接调用链、配置优先级、
CLI 退出码、路径安全边界、parser 边界输入和最小 patch 约束。

单独启动 smoke case：

```bash
forgeagent eval run-core --case basic_python_fix
forgeagent eval run-core --case multi_file_python_fix
forgeagent eval run-core --case inspect_readonly
forgeagent eval run-core --case fail_on_unverified
forgeagent eval run-core --case verification_guard
```

单独启动 medium case：

```bash
forgeagent eval run-core --case no_test_cheating
forgeagent eval run-core --case existing_tests_must_stay_green
forgeagent eval run-core --case multi_file_indirect_call
forgeagent eval run-core --case config_override_priority
forgeagent eval run-core --case cli_exit_code_bug
forgeagent eval run-core --case path_normalization_security
forgeagent eval run-core --case parser_edge_cases
forgeagent eval run-core --case minimal_patch_required
```

使用源码入口等价写法：

```bash
.venv/bin/python -m entry.cli eval run-core --case basic_python_fix
```

### SWE-bench Predictions

```bash
pip install -e ".[swebench]"

python -m entry.swebench generate \
  --dataset-name princeton-nlp/SWE-bench_Lite \
  --split dev \
  --limit 1 \
  --work-dir runs/swebench \
  --output runs/swebench/dev_predictions.jsonl
```

生成器输出：

- `predictions.jsonl`：给官方 SWE-bench harness 使用
- `*.metadata.jsonl`：记录状态、token、步数、日志路径、patch 大小

正式评分仍由官方 SWE-bench harness 执行。

### Langfuse Trace 到回归数据集

```bash
forgeagent eval add-trace TRACE_OR_URL \
  --dataset forge-agent/regression \
  --failure-type premature_finish \
  --notes "final answer contained unexecuted tool syntax"
```

用于把失败 trace 固化成回归样本，后续评估是否修复同类问题。

---

## 11. 任务描述建议

好任务应该可验证、边界清楚。

推荐模板：

```text
[文件/模块] 的 [函数/类/行为] 在 [输入/场景] 下出现 [错误/不符合预期]。
期望行为是 [具体结果]。
请修改实现，并运行 [测试命令] 验证。
限制：[不要改测试/不要新增依赖/不要改公开 API]。
```

例子：

```bash
forgeagent run --repo . --task \
  "src/parser.py 的 parse() 在输入空字符串时抛 ValueError，期望返回 None。
   修复实现，并补充 tests/test_parser.py 的对应测试。运行 pytest tests/test_parser.py。"
```

复杂任务建议写入文件：

```bash
cat > task.txt << 'EOF'
重构 src/database.py：

1. 保持公开 API 不变。
2. 将连接池逻辑和查询逻辑拆开。
3. 不要修改 tests/ 目录。
4. 完成后运行 pytest tests/test_database.py。
EOF

forgeagent run --repo . --task-file task.txt --sandbox
```

---

## 12. 常见问题

**Q：为什么主推 run，不主推 chat？**

`run` 有明确输入、明确结束、明确日志、明确 patch 和退出码，更适合做安全审计、
CI、评测和团队受控执行。`chat` 适合人工探索，但多轮历史会让边界更模糊。

**Q：不加 `--confirm` 会怎样？**

低风险只读操作可以执行。需要确认的高风险动作会被拒绝。这是非交互 `run` 的
fail-closed 默认行为。

**Q：agent 修改了文件但我不满意，怎么撤销？**

Forge Agent 不会默认 push。用 git 查看和撤销：

```bash
git diff
git checkout -- path/to/file.py
```

**Q：沙箱里没有依赖怎么办？**

可以让 agent 在沙箱中安装，或先在任务里明确安装步骤：

```bash
forgeagent run --repo . --task \
  "先运行 pip install -r requirements.txt，再运行 pytest 并修复失败" \
  --mode maintain --sandbox --confirm
```

**Q：如何判断一次 run 好不好？**

建议先看：

- 是否完成任务
- 测试是否通过
- policy violation 是否为 0
- EventLog 是否能解释每一步
- diff 是否只包含必要修改

---

## 13. 配置参考

```yaml
llm:
  provider: deepseek
  model: deepseek-v4-pro
  api_key: ${DEEPSEEK_API_KEY}
  base_url: https://api.deepseek.com
  max_tokens: 8192

agent:
  max_steps: 40
  budget_tokens: 200000
  log_dir: ./logs

tools:
  shell:
    timeout: 30
    max_output_tokens: 8000
  file:
    max_view_lines: 100

context:
  repo_map_budget: 8000
  history_window: 20

observability:
  langfuse:
    enabled: true
    base_url: ${LANGFUSE_BASE_URL}
    public_key: ${LANGFUSE_PUBLIC_KEY}
    secret_key: ${LANGFUSE_SECRET_KEY}
    trace_content: full
    flush_on_exit: true
    debug: false
```

多环境配置：

```bash
forgeagent run --repo . --task "fix bug" -c config/pro.yaml
forgeagent chat --repo . -c config/dev.yaml
```

---

## 快速参考

```bash
# 主入口
forgeagent run --repo . --task "fix the failing tests"
forgeagent run --repo . --mode inspect --task "inspect the project"
forgeagent run --repo . --task-file task.txt --sandbox
forgeagent run --repo . --mode maintain --task "update dependencies and run tests" --confirm
forgeagent run --repo . --task "fix tests" --verify "pytest" --fail-on-unverified

# 审计
forgeagent log list
forgeagent log show logs/xxx.jsonl

# 辅助交互
forgeagent chat --repo .

# 高级/实验
forgeagent eval run-core
forgeagent eval run-core --suite medium
forgeagent eval run-core --case basic_python_fix --output runs/core-eval/summary.json
python -m entry.github_issue -r owner/repo -i 42 -l /tmp/repo --no-pr
python -m entry.swebench generate --split dev --limit 1 --output predictions.jsonl
forgeagent eval add-trace TRACE_OR_URL
```
