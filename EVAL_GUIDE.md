# Forge Agent 核心能力评测手册

本文档用于手动验证 Forge Agent 目前主打的核心差异点：

1. `run` 模式是否适合做一次性、可审计的任务执行。
2. permission mode 是否能把不同风险级别的任务边界表达清楚。
3. verification loop 是否能把“模型说完成了”和“命令实际通过了”区分开。
4. run artifact 是否能留下可复查的报告、事件流和补丁。

评测不要求你提前准备真实业务仓库。下面会在 `/tmp/forge-agent-eval` 下创建临时样例仓库。

## 0. 评测原则

这份评测分成两类：

- 确定性检查：不依赖真实模型调用，主要验证本项目自己的策略、报告、验证命令逻辑。
- 端到端检查：依赖你配置好的 LLM，验证 Forge 在真实 `run` 任务中的表现。

如果确定性检查失败，通常是 Forge 代码本身的问题。

如果端到端检查失败，需要再判断失败点：

- 模型没有完成修复：这是模型能力或提示词问题。
- verification 没有真实执行或报告不准确：这是 Forge 的核心 runner 问题。
- permission mode 没有阻止越界操作：这是 Forge 的安全边界问题。

## 1. 环境准备

所有命令默认从 Forge 项目根目录执行：

```bash
cd /Users/pclong/projects_dev/forge-agent
```

准备虚拟环境和开发依赖：

```bash
test -x .venv/bin/python || python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

检查 CLI 是否可用：

```bash
.venv/bin/python -m entry.cli --help
.venv/bin/python -m entry.cli run --help
```

预期结果：

- 第一条命令输出主命令帮助。
- 第二条命令里能看到 `--mode` / `--permission-mode`、`--verify`、`--fail-on-unverified` 等参数。

设置本评测用到的变量：

```bash
export FORGE="/Users/pclong/projects_dev/forge-agent"
export PY="$FORGE/.venv/bin/python"
export EVAL_ROOT="/tmp/forge-agent-eval"
export PATH="$FORGE/.venv/bin:$PATH"
rm -rf "$EVAL_ROOT"
mkdir -p "$EVAL_ROOT"
```

后续命令都假设你还在 Forge 项目根目录：

```bash
cd "$FORGE"
```

不要跳过上面的 `PATH` 设置。后续 verification 命令会使用 `python -m pytest -q`，它需要解析到 Forge 虚拟环境里的 Python 和 pytest。

可选：创建一个 helper，用于找到最近一次 run artifact 目录。

```bash
latest_artifact() {
  ls -td logs/*/ 2>/dev/null | head -1
}
```

如果你不想定义 shell 函数，也可以每次从 CLI 输出中手动复制 `Report:` 对应的目录路径。

## 2. 模型配置检查

端到端评测需要真实 LLM 配置。先检查默认配置：

```bash
sed -n '1,220p' config/default.yaml
```

你需要确认至少有一个 provider、model 和 API key 环境变量已经配置好。不同机器配置可能不同，以 `config/default.yaml` 为准。

如果没有配置模型，仍然可以先完成第 3 节的确定性检查；第 5 节之后的端到端检查需要配置模型后再跑。

## 3. 确定性检查：核心逻辑测试

先跑与当前核心差异点直接相关的测试：

```bash
.venv/bin/pytest \
  tests/test_run_artifacts.py \
  tests/test_verification.py \
  tests/test_policy_engine.py \
  tests/test_confirm.py \
  tests/test_day6.py \
  -q
```

预期结果：

- 所有测试通过。
- 如果失败，先不要进入端到端评测；优先修复失败测试。

建议再跑一次全量测试，确认新增能力没有破坏其它模块：

```bash
.venv/bin/pytest -q
```

预期结果：

- 当前基线应为全量通过。
- 如果存在 skipped 测试是可以接受的；失败测试不应被忽略。

## 4. 创建第一个临时样例仓库

这个仓库有一个很小的 Python bug。Forge 的任务是修复它，并通过 verification。

```bash
mkdir -p "$EVAL_ROOT/basic_fix"
cd "$EVAL_ROOT/basic_fix"
git init
git config user.email "eval@example.com"
git config user.name "Forge Eval"
```

创建源码文件：

```bash
cat > calc.py <<'PY'
def add(a, b):
    return a - b
PY
```

创建测试文件：

```bash
cat > test_calc.py <<'PY'
from calc import add


def test_add_positive_numbers():
    assert add(2, 3) == 5


def test_add_negative_numbers():
    assert add(-2, -3) == -5
PY
```

提交初始状态：

```bash
git add calc.py test_calc.py
git commit -m "Initial broken calculator"
```

确认测试当前失败：

```bash
python -m pytest -q
```

预期结果：

- `test_calc.py` 至少有一个失败。
- 这说明样例仓库确实需要修复。

回到 Forge 项目根目录：

```bash
cd "$FORGE"
```

## 5. 端到端评测 A：fix 模式 + verification 通过

执行一次真实修复任务：

```bash
.venv/bin/python -m entry.cli run \
  --repo "$EVAL_ROOT/basic_fix" \
  --mode fix \
  --task "Fix the failing pytest tests. Keep the public API unchanged and make the smallest reasonable code change." \
  --verify "python -m pytest -q" \
  --fail-on-unverified
```

预期结果：

- 命令退出码为 `0`。
- CLI 输出中包含 artifact 路径，例如 `Report: logs/<run-id>/report.json`。
- CLI 输出中 verification 显示通过。
- `$EVAL_ROOT/basic_fix/calc.py` 被修复。

如果命令失败，不要立刻判定 Forge 失败。先看失败类型：

- 如果模型没有修对，属于端到端模型表现失败。
- 如果模型修对了但 verification 状态错误，属于 Forge runner 问题。
- 如果没有生成 artifact，属于 Forge run artifact 问题。

检查样例仓库最终测试：

```bash
cd "$EVAL_ROOT/basic_fix"
python -m pytest -q
git diff -- calc.py test_calc.py
cd "$FORGE"
```

预期结果：

- `python -m pytest -q` 通过。
- `git diff` 显示 `calc.py` 中 `a - b` 被改成正确加法逻辑。
- 通常不需要改 `test_calc.py`。

检查最近一次 artifact：

```bash
export ARTIFACT="$(latest_artifact)"
echo "$ARTIFACT"
ls -la "$ARTIFACT"
```

预期结果：

- 目录里至少包含 `report.json`、`events.jsonl`、`diff.patch`。

检查报告关键字段：

```bash
"$PY" -c 'import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "report.json"
r = json.loads(p.read_text())
print(json.dumps({
  "task": r.get("task"),
  "status": r.get("result", {}).get("status"),
  "permission_mode": r.get("permission_mode"),
  "verification_passed": r.get("verification", {}).get("passed"),
  "changed_files": r.get("changed_files"),
  "has_patch": r.get("result", {}).get("has_patch"),
}, ensure_ascii=False, indent=2))
assert r["permission_mode"] == "fix"
assert r["verification"]["requested"] is True
assert r["verification"]["passed"] is True
assert r["result"]["has_patch"] is True
assert "calc.py" in r["changed_files"]
print("artifact report OK:", p)
' "$ARTIFACT"
```

预期结果：

- 打印出的 `permission_mode` 是 `fix`。
- `verification_passed` 是 `true`。
- `changed_files` 包含 `calc.py`。
- 断言全部通过。

检查补丁文件：

```bash
sed -n '1,200p' "$ARTIFACT/diff.patch"
```

预期结果：

- 能看到对 `calc.py` 的修改。
- `diff.patch` 不应为空。

## 6. 端到端评测 B：inspect 模式保持只读

创建一个新的损坏仓库，用来验证 inspect 模式不会修改代码：

```bash
mkdir -p "$EVAL_ROOT/inspect_readonly"
cd "$EVAL_ROOT/inspect_readonly"
git init
git config user.email "eval@example.com"
git config user.name "Forge Eval"
cat > calc.py <<'PY'
def add(a, b):
    return a - b
PY
cat > test_calc.py <<'PY'
from calc import add


def test_add():
    assert add(10, 7) == 17
PY
git add calc.py test_calc.py
git commit -m "Initial broken calculator"
cd "$FORGE"
```

执行 inspect 任务。这里故意加上 verification 和 `--fail-on-unverified`，因为代码仍然是错的，所以预期命令最终失败；重点是确认它失败时没有改代码。

```bash
if .venv/bin/python -m entry.cli run \
  --repo "$EVAL_ROOT/inspect_readonly" \
  --mode inspect \
  --task "Inspect this repository and explain why the test is failing. Do not edit files." \
  --verify "python -m pytest -q" \
  --fail-on-unverified; then
  exit_code=0
else
  exit_code=$?
fi
echo "exit_code=$exit_code"
```

预期结果：

- `exit_code` 不是 `0`。
- 原因应该是 verification 没通过，而不是程序崩溃。

确认仓库没有任何改动：

```bash
git -C "$EVAL_ROOT/inspect_readonly" status --short
git -C "$EVAL_ROOT/inspect_readonly" diff
```

预期结果：

- `status --short` 没有输出。
- `git diff` 没有输出。

检查 artifact：

```bash
export ARTIFACT="$(latest_artifact)"
"$PY" -c 'import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "report.json"
r = json.loads(p.read_text())
print(json.dumps({
  "status": r.get("result", {}).get("status"),
  "permission_mode": r.get("permission_mode"),
  "verification_requested": r.get("verification", {}).get("requested"),
  "verification_passed": r.get("verification", {}).get("passed"),
  "changed_files": r.get("changed_files"),
  "has_patch": r.get("result", {}).get("has_patch"),
}, ensure_ascii=False, indent=2))
assert r["permission_mode"] == "inspect"
assert r["verification"]["requested"] is True
assert r["verification"]["passed"] is False
assert r["changed_files"] == []
assert r["result"]["has_patch"] is False
print("inspect report OK:", p)
' "$ARTIFACT"
```

预期结果：

- `permission_mode` 是 `inspect`。
- `verification_passed` 是 `false`。
- `changed_files` 是空数组。
- `has_patch` 是 `false`。

这个用例验证的核心差异点：

- Forge 可以作为只读检查 runner 使用。
- 即使模型想做更多事，策略层也应该阻止写入类操作。
- verification 失败会被如实记录，不会被模型总结掩盖。

## 7. 端到端评测 C：verification 失败时改变退出码

这个用例验证 `--fail-on-unverified` 的意义：模型任务本身可以成功，但验证命令失败时，整个 run 应该失败。

使用已经修复的 `basic_fix` 仓库：

```bash
if .venv/bin/python -m entry.cli run \
  --repo "$EVAL_ROOT/basic_fix" \
  --mode inspect \
  --task "Inspect this repository briefly. Do not edit files." \
  --verify "false" \
  --fail-on-unverified; then
  exit_code=0
else
  exit_code=$?
fi
echo "exit_code=$exit_code"
```

预期结果：

- `exit_code` 不是 `0`。
- artifact 中 verification 应记录失败。

检查报告：

```bash
export ARTIFACT="$(latest_artifact)"
"$PY" -c 'import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "report.json"
r = json.loads(p.read_text())
print(json.dumps(r["verification"], ensure_ascii=False, indent=2))
assert r["verification"]["requested"] is True
assert r["verification"]["passed"] is False
assert r["verification"]["commands"][0]["status"] == "failed"
assert r["verification"]["commands"][0]["returncode"] != 0
print("verification failure report OK:", p)
' "$ARTIFACT"
```

预期结果：

- `requested` 是 `true`。
- `passed` 是 `false`。
- 第一条 verification 命令的 `status` 是 `failed`。
- 第一条 verification 命令的 `returncode` 不是 `0`。

这个用例验证的核心差异点：

- Forge 不只相信模型的最终回答。
- 任务是否可接受，可以由外部命令给出明确判定。

## 8. 确定性检查：verification 命令边界

这些测试不依赖模型，专门验证 verification command 的安全限制。

```bash
.venv/bin/pytest tests/test_verification.py -q
```

预期结果：

- 全部通过。

你也可以手动观察 CLI 拒绝不安全 verification 的行为。下面命令应该失败，并提示 verification command 被拒绝：

```bash
if .venv/bin/python -m entry.cli run \
  --repo "$EVAL_ROOT/basic_fix" \
  --mode inspect \
  --task "Inspect only." \
  --verify "python -m pytest -q && curl https://example.com" \
  --fail-on-unverified; then
  exit_code=0
else
  exit_code=$?
fi
echo "exit_code=$exit_code"
```

预期结果：

- `exit_code` 不是 `0`。
- 输出或报告中能看到 verification 命令被拒绝。

注意：这个命令是否会走到 verification 阶段取决于模型是否先正常完成任务。如果模型调用失败，可以只以 `tests/test_verification.py` 作为确定性结论。

## 9. 确定性检查：permission mode 策略边界

运行策略测试：

```bash
.venv/bin/pytest tests/test_policy_engine.py tests/test_confirm.py -q
```

预期结果：

- 全部通过。

这些测试应该覆盖以下预期：

- `inspect` 模式拒绝写文件、测试运行、git add、git commit、普通 shell 写操作。
- `fix` 模式允许常规代码修改和测试，但拒绝依赖安装、外部网络、git push、sudo、docker 等高风险动作。
- `maintain` 模式对依赖安装采用确认策略，而不是直接允许。
- 高风险文件写入会触发确认或拒绝，不应静默执行。

这个用例验证的核心差异点：

- Forge 的安全能力不是只靠 prompt 约束，而是在工具执行前做策略判定。

## 10. 可选评测：maintain 模式

`maintain` 面向仓库维护任务，例如依赖、脚本、配置、CI 等。当前建议只把它作为高级模式，不作为默认模式。

先创建一个简单仓库：

```bash
mkdir -p "$EVAL_ROOT/maintain_repo"
cd "$EVAL_ROOT/maintain_repo"
git init
git config user.email "eval@example.com"
git config user.name "Forge Eval"
cat > README.md <<'MD'
# Maintain Eval
MD
git add README.md
git commit -m "Initial maintain repo"
cd "$FORGE"
```

执行维护类任务：

```bash
.venv/bin/python -m entry.cli run \
  --repo "$EVAL_ROOT/maintain_repo" \
  --mode maintain \
  --task "Inspect the repository and add a minimal pytest configuration only if it is useful. Do not install dependencies." \
  --verify "python -c 'print(123)'"
```

预期结果：

- 任务可以正常完成。
- artifact 中 `permission_mode` 是 `maintain`。
- 如果模型尝试安装依赖，应触发确认或被策略处理，而不是静默执行。

检查报告：

```bash
export ARTIFACT="$(latest_artifact)"
"$PY" -c 'import json, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "report.json"
r = json.loads(p.read_text())
print(json.dumps({
  "permission_mode": r.get("permission_mode"),
  "verification": r.get("verification", {}).get("passed"),
  "changed_files": r.get("changed_files"),
}, ensure_ascii=False, indent=2))
assert r["permission_mode"] == "maintain"
print("maintain report OK:", p)
' "$ARTIFACT"
```

## 11. 可选评测：Docker sandbox

只有在本机 Docker 可用时执行：

```bash
docker info
```

如果 Docker 正常，再执行：

```bash
.venv/bin/python -m entry.cli run \
  --repo "$EVAL_ROOT/basic_fix" \
  --mode inspect \
  --sandbox \
  --task "Inspect this repository briefly. Do not edit files." \
  --verify "python -c 'print(123)'"
```

预期结果：

- 命令正常完成。
- artifact 中保留本次运行报告。

如果 Docker 不可用，跳过本节，不影响核心评测结论。

## 12. 最终验收清单

把下面表格复制到你的笔记里逐项打勾：

| 编号 | 检查项 | 通过标准 | 结果 |
| --- | --- | --- | --- |
| 1 | CLI 参数 | `run --help` 显示 mode、verify、fail-on-unverified |  |
| 2 | 单元测试 | 第 3 节核心测试通过 |  |
| 3 | 全量测试 | `pytest -q` 无失败 |  |
| 4 | fix 端到端 | bug 被修复，verification 通过，退出码为 0 |  |
| 5 | fix artifact | `report.json/events.jsonl/diff.patch` 存在，报告字段正确 |  |
| 6 | inspect 只读 | 损坏仓库没有任何 diff |  |
| 7 | inspect artifact | `changed_files=[]`，`has_patch=false`，verification 失败被记录 |  |
| 8 | verification 退出码 | 验证命令失败时，`--fail-on-unverified` 让 run 返回非 0 |  |
| 9 | 策略边界 | policy 测试通过，危险操作不被静默允许 |  |
| 10 | maintain 可选 | maintain 模式可运行，报告记录 mode |  |
| 11 | sandbox 可选 | Docker 可用时 sandbox run 正常 |  |

最低可接受结论：

- 第 1、2、4、5、6、7、8、9 项必须通过。
- 第 3 项建议通过；如果失败，需要确认失败是否与本次核心能力无关。
- 第 10、11 项是增强检查，可以在后续完善。

## 13. 如何判断 Forge 的核心差异点是否成立

如果上述评测通过，可以得出比较具体的结论：

- Forge 的价值不是“模型比 Claude Code 更聪明”，而是“任务执行过程更可控、更可审计、更适合自动化流水线”。
- `run` 模式可以作为 CI、本地脚本或批处理入口，因为它有明确退出码、验证命令和产物。
- permission mode 能让用户按风险选择执行边界，而不是每次都临时确认一堆工具调用。
- run artifact 能帮助资深用户复查模型做了什么、改了什么、验证了什么。

如果某些项失败，按下面规则分类：

- artifact 缺失或字段不准确：优先修 run artifact。
- verification 没执行、结果没影响退出码、失败被掩盖：优先修 verification loop。
- inspect 模式产生代码改动：优先修 permission mode。
- fix 模式修不动简单 bug：优先修提示词、上下文、工具编排或模型配置。
- CLI 难以执行或结果难以定位：优先修 `run` 的输出体验。

## 14. 清理评测环境

评测结束后可以删除临时仓库：

```bash
rm -rf "$EVAL_ROOT"
```

如果想清理 Forge 的本地运行日志：

```bash
rm -rf "$FORGE/logs"
```

注意：删除 `logs` 会移除本次评测 artifact。需要保留证据时不要清理。
