"""
entry/cli.py

命令行入口。`run` 是主入口：一次任务、一个仓库、一份可审计日志。

用法：
    # 直接传任务描述
    python -m entry.cli run --repo /path/to/repo --task "Fix the failing test"

    # 从文件读任务描述
    python -m entry.cli run --repo . --task-file task.txt

    # 覆盖模型
    python -m entry.cli run --repo . --task "fix it" --model deepseek-chat

    # 查看 event log 统计
    python -m entry.cli log show logs/abc123_20240101_120000.jsonl

安装为命令行工具后（pyproject.toml 里配置了 scripts）：
    forgeagent run --repo . --task "fix it"
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import click

# 把项目根加入 path（直接跑脚本时需要）
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 模块级 import（供 patch 使用）
from agent.eval_dataset import (  # noqa: E402
    DEFAULT_DATASET_NAME as DEFAULT_EVAL_DATASET,
)
from config.schema import load_config, merge_cli_overrides  # noqa: E402
from llm.router import create_backend_from_config           # noqa: E402


# ---------------------------------------------------------------------------
# 辅助：彩色输出
# ---------------------------------------------------------------------------

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

def green(t: str) -> str:  return _c(t, "32")
def yellow(t: str) -> str: return _c(t, "33")
def red(t: str) -> str:    return _c(t, "31")
def cyan(t: str) -> str:   return _c(t, "36")
def bold(t: str) -> str:   return _c(t, "1")
def dim(t: str) -> str:    return _c(t, "2")
def magenta(t: str) -> str: return _c(t, "35")


# ---------------------------------------------------------------------------
# 构建 agent 各组件
# ---------------------------------------------------------------------------

def _build_registry(cfg, confirm_callback=None, runtime=None, repo_path=None):
    """根据配置组装工具注册表。"""
    from tools.base import ToolRegistry
    from tools.file_tool import FileReadTool, FileViewTool, FileWriteTool
    from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool
    from tools.path_guard import WorkspaceBoundary
    from tools.runtime import LocalRuntime
    from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool
    from tools.shell_tool import ShellTool
    from tools.test_tool import PytestTool

    boundary = (
        WorkspaceBoundary(repo_path, confirm_callback=confirm_callback)
        if repo_path is not None else None
    )
    if runtime is None and boundary is not None:
        runtime = LocalRuntime(boundary=boundary)

    return (
        ToolRegistry()
        .register(ShellTool(
            runtime=runtime,
            boundary=boundary,
            enforce_confirmation=False,
        ))
        .register(FileReadTool(boundary=boundary))
        .register(FileViewTool(boundary=boundary))
        .register(FileWriteTool(boundary=boundary))
        .register(SearchTextTool(boundary=boundary))
        .register(FindFilesTool(boundary=boundary))
        .register(FindSymbolTool(boundary=boundary))
        .register(PytestTool(runtime=runtime, boundary=boundary))
        .register(GitStatusTool(runtime=runtime, boundary=boundary))
        .register(GitDiffTool(runtime=runtime, boundary=boundary))
        .register(GitAddTool(runtime=runtime, boundary=boundary))
        .register(GitCommitTool(runtime=runtime, boundary=boundary))
    )


def _message_was_streamed(message: str, streamed_text: str) -> bool:
    msg = message.strip()
    streamed = streamed_text.strip()
    return bool(msg and (streamed == msg or streamed.endswith(msg)))


def _print_step(event, *, streamed_text: str = "") -> None:
    """实时打印单条 event。"""
    from agent.task import EventType
    etype = event.event_type
    payload = event.payload

    if etype == EventType.TASK_START:
        task = payload["task"]
        click.echo(bold(f"\n{'─'*60}"))
        click.echo(bold(f"  Task : {task['description'][:80]}"))
        click.echo(bold(f"  Repo : {task['repo_path']}"))
        click.echo(bold(f"{'─'*60}\n"))

    elif etype == EventType.ACTION:
        step = payload["step"]
        action = payload["action"]
        thought = action.get("thought", "")[:160]
        atype = action.get("action_type", "")
        tc = action.get("tool_call")
        tool_calls = action.get("tool_calls") or ([tc] if tc else [])
        if atype == "give_up":
            return
        click.echo(cyan(f"[Step {step}] {atype}"))
        if thought:
            click.echo(dim(f"  ↳ {thought}"))
        if tool_calls:
            for idx, tool_call in enumerate(tool_calls, start=1):
                label = f"{step}.{idx}" if len(tool_calls) > 1 else str(step)
                params_str = str(tool_call["params"])[:100]
                click.echo(
                    f"  Tool[{label}]: {tool_call['name']}  params: {params_str}"
                )

    elif etype == EventType.OBSERVATION:
        obs = payload["observation"]
        status = obs.get("status", "")
        tool = obs.get("tool_name", "")
        output = obs.get("output", "")
        if status == "success":
            click.echo(green(f"  ✓ [{tool}]"))
        else:
            click.echo(red(f"  ✗ [{tool}] {obs.get('error', '')}"))
        # 打印前 5 行输出
        for line in output.splitlines()[:5]:
            click.echo(dim(f"    {line}"))
        if len(output.splitlines()) > 5:
            click.echo(dim(f"    ... ({len(output.splitlines())-5} more lines)"))
        click.echo()

    elif etype == EventType.REFLECTION:
        click.echo(yellow(f"\n  ⟳ Reflection: {payload.get('reason', '')}\n"))

    elif etype == EventType.TASK_COMPLETE:
        summary = payload.get("summary", "")
        if _message_was_streamed(summary, streamed_text):
            click.echo(green(bold("\n✓ COMPLETE\n")))
        else:
            click.echo(green(bold(f"\n✓ COMPLETE: {summary}\n")))

    elif etype == EventType.TASK_FAILED:
        click.echo(red(bold(f"\n✗ FAILED: {payload.get('reason', '')}\n")))


def _print_verification(verification: dict) -> None:
    """Print a compact post-run verification summary."""
    if not verification.get("requested"):
        return

    passed = verification.get("passed") is True
    status = green("PASSED") if passed else red("FAILED")
    click.echo(bold("\nVerification"))
    click.echo(f"  Status: {status}")
    for item in verification.get("commands", []):
        cmd = item.get("command", "")
        item_status = item.get("status", "")
        label = green("✓") if item_status == "passed" else red("✗")
        click.echo(f"  {label} {cmd}  [{item_status}]")
        error = item.get("error")
        if error:
            click.echo(dim(f"    {error[:180]}"))
    click.echo()


# ---------------------------------------------------------------------------
# CLI 主命令组
# ---------------------------------------------------------------------------

@click.group()
@click.option(
    "--config", "-c",
    default=None,
    help="Path to config YAML file (default: config/default.yaml)",
)
@click.pass_context
def cli(ctx: click.Context, config: str | None) -> None:
    """Coding Agent runner — secure, auditable repository task execution."""
    # ctx 是 Click 的核心机制，用于在命令组及其子命令之间共享数据
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


# ---------------------------------------------------------------------------
# run 子命令
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--task", "-t", default=None, help="Task description for this auditable run")
@click.option("--task-file", "-f", default=None, help="Read the run task description from file")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option(
    "--mode", "--permission-mode", # 两种命令行参数
    "permission_mode", # 传递赋值的变量名
    default="fix",
    show_default=True,
    type=click.Choice(["inspect", "fix", "maintain"]),
    help="Permission mode for this run",
)
@click.option(
    "--verify",
    "verify_commands",
    multiple=True,
    help="Run an explicit verification command after the agent finishes; repeatable",
)
@click.option(
    "--verify-timeout",
    default=300,
    show_default=True,
    type=int,
    help="Timeout in seconds for each --verify command",
)
@click.option(
    "--fail-on-unverified",
    is_flag=True,
    default=False,
    help="Exit non-zero when verification is missing or not passing",
)
@click.option("--max-steps", default=None, type=int, help="Override max steps")
@click.option("--stream", "-s", is_flag=True, default=True, help="Enable streaming output (default: on)")
@click.option("--confirm", is_flag=True, default=False, help="Ask before confirmation-required actions")
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def run(
    ctx: click.Context,
    repo: str,
    task: str | None,
    task_file: str | None,
    model: str | None,
    provider: str | None,
    permission_mode: str,
    verify_commands: tuple[str, ...],
    verify_timeout: int,
    fail_on_unverified: bool,
    max_steps: int | None,
    stream: bool,
    confirm: bool,
    sandbox: bool,
    verbose: bool,
) -> None:
    """Run one auditable coding-agent task on a repository."""
    # 配置日志
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # 加载配置
    config = load_config(ctx.obj.get("config_path"))
    config = merge_cli_overrides(
        config, provider=provider, model=model, max_steps=max_steps
    )

    # 解析任务描述
    if task_file:
        description = Path(task_file).read_text(encoding="utf-8").strip()
    elif task:
        description = task
    else:
        click.echo(red("Error: provide --task or --task-file"), err=True)
        sys.exit(1)

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)
    if verify_timeout <= 0:
        click.echo(red("Error: --verify-timeout must be positive"), err=True)
        sys.exit(1)

    # 打印运行信息
    click.echo(bold(f"\n🤖 Forge Agent — Run Mode"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Repo     : {repo_path}")
    click.echo(f"  Mode     : {permission_mode}")
    if verify_commands:
        click.echo(f"  Verify   : {len(verify_commands)} command(s)")
    click.echo(f"  Max steps: {config.agent.max_steps}\n")

    # 构建各组件
    try:
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "model":    config.llm.model,
            "api_key":  config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    from tools.shell_tool import terminal_confirm
    from tools.runtime import create_runtime
    confirm_cb = terminal_confirm if confirm else None
    runtime = create_runtime(sandbox=sandbox, repo_path=str(repo_path)) if sandbox else None
    if sandbox:
        click.echo(dim(f"  Sandbox: Docker ({runtime.name})"))
    registry = _build_registry(
        config,
        confirm_callback=confirm_cb,
        runtime=runtime,
        repo_path=str(repo_path),
    )

    from agent.core import Agent, AgentConfig
    from agent.event_log import EventLog
    from agent.run_artifacts import write_run_artifacts
    from agent.task import Task
    from agent.telemetry import build_tracer_from_config
    from agent.verification import run_verifications
    from policy import PolicyEngine
    try:
        from context.token_budget import is_tiktoken_available
    except ImportError:
        is_tiktoken_available = lambda: False

    streamed_text_parts: list[str] = []

    # 流式回调：最终回答正常亮色
    def _stream_cb(text: str) -> None:
        import sys
        streamed_text_parts.append(text)
        sys.stdout.write(text)
        sys.stdout.flush()

    # 推理回调：思考过程 dim 暗色
    def _thought_cb(text: str) -> None:
        import sys
        sys.stdout.write(dim(text))
        sys.stdout.flush()

    try:
        tracer = build_tracer_from_config(
            config,
            mode="run",
            provider=config.llm.provider,
            model=config.llm.model,
        )
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    agent_config = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        history_max_messages=config.context.history_window * 2,
        stream=stream,
        stream_callback=_stream_cb if stream else None,
        thought_callback=_thought_cb if stream else None,
        confirm_callback=confirm_cb,
        tracer=tracer,
        policy_engine=PolicyEngine(mode=permission_mode),
    )
    agent = Agent(backend, registry, agent_config)

    task_obj = Task(
        description=description,
        repo_path=str(repo_path),
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )

    if verbose:
        click.echo(dim(
            f"  tiktoken: {'yes' if is_tiktoken_available() else 'no (char estimate)'}\n"
        ))

    # 运行
    t0 = time.time()
    artifact_paths = None
    verification = None
    try:
        with EventLog.create(task_obj, log_dir=config.agent.log_dir) as log:
            click.echo(dim(f"  Log: {log.path}\n"))
            result = agent.run(task_obj, log)
            # 打印所有 events
            streamed_text = "".join(streamed_text_parts) if stream else ""
            for event in log.replay():
                _print_step(event, streamed_text=streamed_text)
            verification = run_verifications(
                verify_commands,
                repo_path=repo_path,
                runtime=runtime,
                timeout=verify_timeout,
            )
            _print_verification(verification)
            elapsed = time.time() - t0
            artifact_paths = write_run_artifacts(
                task=task_obj,
                result=result,
                log=log,
                duration_seconds=elapsed,
                permission_mode=permission_mode,
                verification=verification,
            )
    finally:
        if getattr(tracer, "flush_on_exit", False):
            tracer.flush()

    # 打印结果
    click.echo(bold("─" * 60))
    status_str = green("SUCCESS") if result.is_success() else red(result.status.value.upper())
    click.echo(f"Status  : {status_str}")
    click.echo(f"Steps   : {result.steps_taken}")
    click.echo(f"Tokens  : {result.total_tokens:,}")
    click.echo(f"Time    : {elapsed:.1f}s")
    if verification is not None:
        verify_status = verification.get("status", "not_requested").upper()
        if verification.get("requested") or fail_on_unverified:
            if verification.get("passed") is True:
                verify_color = green
            elif verification.get("requested"):
                verify_color = red
            else:
                verify_color = yellow
            click.echo(f"Verify  : {verify_color(verify_status)}")
    if artifact_paths is not None:
        click.echo(f"Report  : {artifact_paths.report}")
        click.echo(f"Diff    : {artifact_paths.diff}")
        click.echo(f"Events  : {artifact_paths.events}")
    if result.error:
        click.echo(red(f"Error   : {result.error}"))
    click.echo(bold("─" * 60) + "\n")

    verification_passed = verification is not None and verification.get("passed") is True
    exit_success = result.is_success()
    if fail_on_unverified:
        exit_success = exit_success and verification_passed
    sys.exit(0 if exit_success else 1)



# ---------------------------------------------------------------------------
# chat 子命令 — 交互对话模式
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option(
    "--mode", "--permission-mode",
    "permission_mode",
    default="fix",
    show_default=True,
    type=click.Choice(["inspect", "fix", "maintain"]),
    help="Permission mode for this chat session",
)
@click.option("--max-steps", default=None, type=int, help="Max steps per round")
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def chat(
    ctx: click.Context,
    repo: str,
    model: str | None,
    provider: str | None,
    permission_mode: str,
    max_steps: int | None,
    sandbox: bool,
    verbose: bool,
) -> None:
    """Interactive exploration mode with shared conversation history."""
    import logging
    from entry.chat import ChatSession

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(ctx.obj.get("config_path"))
    config = merge_cli_overrides(config, provider=provider, model=model, max_steps=max_steps)

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    try:
        backend = create_backend_from_config({
            "provider":   config.llm.provider,
            "model":      config.llm.model,
            "api_key":    config.llm.api_key or None,
            "base_url":   config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    from tools.shell_tool import terminal_confirm
    from tools.runtime import create_runtime
    confirm_cb = terminal_confirm
    runtime = create_runtime(sandbox=sandbox, repo_path=str(repo_path)) if sandbox else None
    if sandbox:
        click.echo(dim(f"  Sandbox: Docker ({runtime.name})"))
    registry = _build_registry(
        config,
        confirm_callback=confirm_cb,
        runtime=runtime,
        repo_path=str(repo_path),
    )
    try:
        session = ChatSession(
            backend=backend,
            registry=registry,
            config=config,
            repo_path=str(repo_path),
            log_dir=config.agent.log_dir,
            confirm_callback=confirm_cb,   # chat 模式默认开启确认
            permission_mode=permission_mode,
        )
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    # 欢迎信息
    click.echo(bold(f"\n🤖 Forge Agent — Chat Mode"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Repo     : {repo_path}")
    click.echo(f"  Mode     : {permission_mode}")
    click.echo(dim(f"  Type your task. Commands: /exit /stats /clear /help\n"))

    # 启用行编辑：退格、方向键、Ctrl+A/E、历史记录（↑↓）
    try:
        import readline as _rl
        import sys as _sys
        # 检测后端：libedit（某些 Linux/macOS）还是 GNU readline
        _is_libedit = "libedit" in getattr(_rl, "__doc__", "") or (
            hasattr(_rl, "parse_and_bind") and _sys.platform == "darwin"
        )
        # 更可靠的检测：尝试 libedit 特有的绑定语法
        try:
            _rl.parse_and_bind("bind -e")   # libedit 启用 Emacs 模式
            _is_libedit = True
        except Exception:
            _is_libedit = False

        if _is_libedit:
            _rl.parse_and_bind("bind -e")           # Emacs 模式：Ctrl+A/E/K 等
            _rl.parse_and_bind("bind ^I rl_complete")  # Tab 补全
        else:
            _rl.parse_and_bind("set editing-mode emacs")  # GNU readline Emacs 模式
            _rl.parse_and_bind("tab: complete")

        _rl.set_history_length(500)   # 历史记录最多 500 条
    except ImportError:
        pass  # Windows 没有 readline，降级为普通 input

    # 主 REPL 循环
    while True:
        try:
            # 清理当前行（流式输出后 readline 不知道屏幕上有残留字符）
            # \r 回到行首，\033[2K 清除整行，然后显示提示符
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
            user_input = input(magenta("you") + " > ").strip()
        except EOFError:
            click.echo()
            break
        except KeyboardInterrupt:
            click.echo()
            break

        if not user_input:
            continue

        # 内置命令
        if user_input.startswith("/"):
            cmd = user_input.lower()
            if cmd in ("/exit", "/quit", "/q"):
                break
            elif cmd == "/stats":
                session.print_stats()
            elif cmd == "/clear":
                session._shared_history.clear_except_first()
                click.echo(dim("  History cleared (kept initial context)."))
            elif cmd == "/help":
                click.echo(dim(
                    "  Commands:\n"
                    "    /exit   — quit\n"
                    "    /stats  — show session statistics\n"
                    "    /clear  — clear conversation history\n"
                    "    /help   — show this help\n"
                    "  Anything else is sent to the agent."
                ))
            else:
                click.echo(dim(f"  Unknown command: {user_input}. Type /help for help."))
            continue

        # 运行一轮 agent
        click.echo(dim(f"\n  Agent working..."))
        try:
            session.run_round(user_input)
        except KeyboardInterrupt:
            click.echo(yellow("\n  Interrupted. Type /exit to quit or continue with a new task."))
        except Exception as e:
            click.echo(red(f"\n  Error: {e}"))
            if verbose:
                import traceback
                traceback.print_exc()

    session.close() # 显示将trace刷入langfuse中

    session.print_stats()
    click.echo(dim("  Bye!\n"))


# ---------------------------------------------------------------------------
# eval 子命令组
# ---------------------------------------------------------------------------

@cli.group("eval")
def eval_cmd() -> None:
    """Manage experimental evaluation datasets."""


@eval_cmd.command("run-core")
@click.option(
    "--case",
    "case_ids",
    multiple=True,
    help="Run only selected case ids; repeatable and comma/space separated.",
)
@click.option("--limit", default=None, type=int, help="Limit selected cases.")
@click.option(
    "--work-dir",
    default="runs/core-eval",
    show_default=True,
    help="Directory for disposable repos and the benchmark summary.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write summary JSON to this path; defaults inside --work-dir.",
)
@click.option("--provider", "-p", default=None, help="Override LLM provider.")
@click.option("--model", "-m", default=None, help="Override LLM model.")
@click.option("--max-steps", default=None, type=int, help="Override agent max steps.")
@click.option(
    "--verify-timeout",
    default=300,
    show_default=True,
    type=int,
    help="Timeout in seconds for each case verification command.",
)
@click.option(
    "--case-timeout",
    default=900,
    show_default=True,
    type=int,
    help="Wall-clock timeout in seconds for each case run.",
)
@click.option("--fail-fast", is_flag=True, help="Stop after the first failed case.")
@click.option("--list-cases", is_flag=True, help="List built-in core cases and exit.")
@click.pass_context
def eval_run_core(
    ctx: click.Context,
    case_ids: tuple[str, ...],
    limit: int | None,
    work_dir: str,
    output: Path | None,
    provider: str | None,
    model: str | None,
    max_steps: int | None,
    verify_timeout: int,
    case_timeout: int,
    fail_fast: bool,
    list_cases: bool,
) -> None:
    """Run the local core benchmark suite against `run` mode."""
    from agent.core_benchmark import default_core_cases, run_core_benchmark

    if list_cases:
        click.echo(bold("Core benchmark cases"))
        for case in default_core_cases():
            click.echo(f"  {case.id:<24} {case.mode:<8} {case.name}")
        return

    click.echo(bold("\nForge Agent Core Benchmark"))
    click.echo(f"  Work dir : {Path(work_dir).resolve()}")
    if case_ids:
        click.echo(f"  Cases    : {', '.join(case_ids)}")
    elif limit is not None:
        click.echo(f"  Cases    : first {limit}")
    else:
        click.echo("  Cases    : built-in suite")
    click.echo()

    try:
        result = run_core_benchmark(
            work_dir=work_dir,
            output_path=output,
            case_ids=case_ids,
            limit=limit,
            config_path=ctx.obj.get("config_path"),
            provider=provider,
            model=model,
            max_steps=max_steps,
            verify_timeout=verify_timeout,
            case_timeout=case_timeout,
            fail_fast=fail_fast,
        )
    except ValueError as exc:
        click.echo(red(f"Error: {exc}"), err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(red(f"Error: core benchmark failed: {exc}"), err=True)
        sys.exit(1)

    for case_result in result.cases:
        label = green("PASS") if case_result.passed else red("FAIL")
        click.echo(
            f"[{label}] {case_result.case_id} "
            f"exit={case_result.exit_code} "
            f"verify={case_result.verification_status or '-'} "
            f"changed={case_result.changed_files}"
        )
        if not case_result.passed:
            for failure in case_result.failures:
                click.echo(red(f"  - {failure}"))
            if case_result.report_path:
                click.echo(dim(f"  report: {case_result.report_path}"))
            elif case_result.stderr_tail:
                last_error = case_result.stderr_tail.strip().splitlines()[-1]
                click.echo(dim(f"  stderr: {last_error[:220]}"))

    click.echo()
    click.echo(bold("Summary"))
    click.echo(f"  Passed : {result.passed}/{result.total}")
    click.echo(f"  Failed : {result.failed}")
    click.echo(f"  Output : {result.summary_path}")

    sys.exit(0 if result.success else 1)


@eval_cmd.command("add-trace")
@click.argument("trace_ref")
@click.option(
    "--dataset", "-d",
    "dataset_name",
    default=DEFAULT_EVAL_DATASET,
    show_default=True,
    help="Langfuse dataset name.",
)
@click.option(
    "--source-observation-id",
    default=None,
    help="Observation to link as the dataset source; defaults to URL peek/root.",
)
@click.option(
    "--focused-observation-id",
    default=None,
    help="Observation that triggered the case; defaults to URL observation.",
)
@click.option(
    "--failure-type",
    default=None,
    help="Failure label to store on the dataset item.",
)
@click.option(
    "--notes",
    default=None,
    help="Human notes to store with the dataset item.",
)
@click.option(
    "--no-create-dataset",
    is_flag=True,
    default=False,
    help="Do not create the dataset if it does not exist.",
)
@click.pass_context
def eval_add_trace(
    ctx: click.Context,
    trace_ref: str,
    dataset_name: str,
    source_observation_id: str | None,
    focused_observation_id: str | None,
    failure_type: str | None,
    notes: str | None,
    no_create_dataset: bool,
) -> None:
    """Add a Langfuse trace URL or trace id to a regression dataset."""
    from agent import eval_dataset as eval_dataset_mod

    config = load_config(ctx.obj.get("config_path"))
    try:
        result = eval_dataset_mod.add_trace_to_dataset(
            trace_ref,
            dataset_name=dataset_name,
            config=config,
            source_observation_id=source_observation_id,
            focused_observation_id=focused_observation_id,
            failure_type=failure_type,
            notes=notes,
            create_dataset=not no_create_dataset,
        )
    except ValueError as exc:
        click.echo(red(f"Error: {exc}"), err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(red(f"Error: failed to add trace to dataset: {exc}"), err=True)
        sys.exit(1)

    click.echo(green("Added Langfuse trace to dataset."))
    click.echo(f"  Dataset : {result.dataset_name}")
    click.echo(f"  Item    : {result.dataset_item_id or '(unknown)'}")
    click.echo(f"  Trace   : {result.trace_id}")
    click.echo(f"  Source  : {result.source_observation_id or '-'}")
    if result.focused_observation_id:
        click.echo(f"  Focus   : {result.focused_observation_id}")
    click.echo(f"  Failure : {result.failure_type}")
    if result.created_dataset:
        click.echo(dim("  Dataset was created."))


# ---------------------------------------------------------------------------
# log 子命令组
# ---------------------------------------------------------------------------

@cli.group()
def log() -> None:
    """Inspect auditable event logs."""


@log.command("show")
@click.argument("log_file")
def log_show(log_file: str) -> None:
    """Show a summary of an event log file."""
    from agent.event_log import EventLog, summarize_run

    path = Path(log_file)
    if not path.exists():
        click.echo(red(f"File not found: {path}"), err=True)
        sys.exit(1)

    with EventLog.open_existing(path) as elog:
        events = elog.replay()
        stats = summarize_run(elog)

    click.echo(bold(f"\nEvent Log: {path.name}"))
    click.echo(f"  Total events : {stats['total_events']}")
    click.echo(f"  Actions      : {stats['actions']}")
    click.echo(f"  Reflections  : {stats['reflections']}")
    click.echo(f"  Tool calls   : {stats['tool_calls']}")
    click.echo(f"  Final status : {stats['final_status']}\n")

    click.echo(bold("Events:"))
    for event in events:
        ts = event.timestamp[11:19]   # HH:MM:SS
        etype = event.event_type.value
        detail = ""
        if event.event_type.value == "action":
            action = event.payload.get("action", {})
            tool_calls = action.get("tool_calls") or []
            if not tool_calls and action.get("tool_call"):
                tool_calls = [action["tool_call"]]
            if tool_calls:
                names = ",".join(tc["name"] for tc in tool_calls)
                detail = f"  tool={names}"
        elif event.event_type.value == "observation":
            obs = event.payload.get("observation", {})
            detail = f"  status={obs.get('status')}"
        click.echo(f"  {ts}  {etype:<16}{detail}")


@log.command("list")
@click.option("--dir", "log_dir", default="./logs", help="Log directory")
def log_list(log_dir: str) -> None:
    """List all event log files."""
    log_path = Path(log_dir)
    if not log_path.exists():
        click.echo(f"Log directory not found: {log_path}")
        return

    files = sorted(log_path.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        click.echo("No log files found.")
        return

    click.echo(bold(f"\nLog files in {log_path}:\n"))
    for f in files:
        size_kb = f.stat().st_size / 1024
        click.echo(f"  {f.name}  ({size_kb:.1f} KB)")
    click.echo()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
