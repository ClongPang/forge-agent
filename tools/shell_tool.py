"""
tools/shell_tool.py

Shell 命令执行工具。四层防护：
1. 黑名单：拒绝明显破坏性命令（硬拦截，不可绕过）
2. 白名单：只读命令免确认直接执行
3. 权限确认：写操作等待用户 y/n（可通过 confirm_callback 注入）
4. Timeout + 输出截断：防挂起、防上下文爆炸

权限确认设计：
- confirm_callback 是一个 Callable[[str], bool]，返回 True 表示允许
- 默认 None 时，只读命令直接执行；需要确认的命令会被拒绝
- Agent 模式可设置 enforce_confirmation=False，让 PolicyEngine 统一处理确认
- chat 模式 / 交互模式传入真实的终端确认函数
- 测试时传入 mock，不需要真实终端
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from tools.base import BaseTool, ToolResult
from tools.path_guard import WorkspaceBoundary
from tools.runtime import LocalRuntime, Runtime
from tools.shell_safety import (
    check_blocked,
    check_outside_path_references,
    check_sensitive_path_references,
    needs_confirm,
)


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

MAX_OUTPUT_CHARS = 8_000

# 确认回调类型：接收命令字符串，返回 True=允许 / False=拒绝
ConfirmCallback = Callable[[str], bool]


# ---------------------------------------------------------------------------
# ShellTool
# ---------------------------------------------------------------------------

class ShellTool(BaseTool):
    """
    执行 shell 命令，返回 stdout + stderr。

    params:
        cmd (str):     shell 命令字符串
        timeout (int): 超时秒数（默认 30）
        cwd (str):     工作目录（默认使用当前目录）

    构造参数:
        confirm_callback: 需要确认时调用，返回 True 表示用户允许执行。
                          None 表示拒绝需要确认的命令（run 模式默认）。
        enforce_confirmation: False 时跳过本工具内部确认，适用于上层
                              PolicyEngine 已经完成确认的 Agent 模式。
    """

    def __init__(
        self,
        confirm_callback: ConfirmCallback | None = None,
        runtime: Runtime | None = None,
        boundary: WorkspaceBoundary | None = None,
        enforce_confirmation: bool = True,
    ) -> None:
        self._confirm_callback = confirm_callback
        self._boundary = boundary
        self._runtime = runtime or LocalRuntime(boundary=boundary)
        self._enforce_confirmation = enforce_confirmation

    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command and return its output (stdout + stderr combined). "
            "Timeout is 30s by default. Avoid long-running commands; "
            "prefer targeted commands like 'grep', 'pytest tests/foo.py', 'git diff'."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "cmd": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (optional)",
                },
            },
            "required": ["cmd"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cmd: str = params.get("cmd", "").strip()
        timeout: int = int(params.get("timeout", 30))
        cwd: str | None = params.get("cwd", None)

        if not cmd:
            return ToolResult(success=False, output="", error="cmd is required")

        # 层 1：黑名单硬拦截
        blocked = check_blocked(cmd)
        if blocked:
            return ToolResult(
                success=False,
                output="",
                error=f"Command blocked for safety: matched '{blocked}'",
            )

        outside_error = check_outside_path_references(cmd, self._boundary)
        if outside_error:
            return ToolResult(success=False, output="", error=outside_error)

        repo_root = (
            self._boundary.root
            if self._boundary is not None
            else Path(cwd or Path.cwd()).resolve(strict=False)
        )
        sensitive_error = check_sensitive_path_references(cmd, repo_root, cwd)
        if sensitive_error:
            return ToolResult(success=False, output="", error=sensitive_error)

        # 层 2：白名单免确认
        if not needs_confirm(cmd):
            return self._run(cmd, timeout, cwd)

        # 层 3：权限确认
        if not self._enforce_confirmation:
            return self._run(cmd, timeout, cwd)

        if self._confirm_callback is not None:
            allowed = self._confirm_callback(cmd)
            if not allowed:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Command rejected by user: {cmd!r}",
                )
        else:
            return ToolResult(
                success=False,
                output="",
                error=f"Command requires confirmation but no confirmation callback is available: {cmd!r}",
            )

        return self._run(cmd, timeout, cwd)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _run(self, cmd: str, timeout: int, cwd: str | None) -> ToolResult:
        """通过 runtime 执行命令（本地或 Docker 沙箱）。"""
        result = self._runtime.exec(cmd, cwd=cwd, timeout=timeout)
        output = _truncate(result.output, MAX_OUTPUT_CHARS)
        if not result.success:
            # 区分 timeout 和普通错误，error 字段包含可读原因
            if "timed out" in result.stderr.lower():
                error = result.stderr.strip()
            else:
                error = f"Exit code: {result.returncode}"
        else:
            error = None
        return ToolResult(success=result.success, output=output, error=error)


def _truncate(text: str, max_chars: int) -> str:
    """输出过长时截断：保留头部 60% + 尾部 40%。"""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        text[:head]
        + f"\n... [{omitted} characters truncated] ...\n"
        + text[-tail:]
    )


# ---------------------------------------------------------------------------
# 终端确认函数（在 cli/chat 里直接使用）
# ---------------------------------------------------------------------------

def terminal_confirm(cmd: str) -> bool:
    """
    在终端显示命令并等待用户确认。
    返回 True 表示允许，False 表示拒绝。

    显示格式：
        ⚠  Agent wants to run:
           $ git commit -m "fix parser"
        Allow? [y/N/a(lways)] _
    """
    import sys

    # 判断是否在交互式终端（检测标准输入（stdin）是否连接到了一个真实的终端）
    if not sys.stdin.isatty():
        # 非交互式（pipe / CI）：默认拒绝，避免意外执行
        print(f"\n[confirm] Non-interactive terminal, rejecting: {cmd!r}", flush=True)
        return False

    print(f"\n\033[33m  ⚠  Agent wants to run:\033[0m")
    print(f"     \033[1m$ {cmd}\033[0m")

    while True:
        try:
            ans = input("  Allow? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no", ""):
            print("  \033[31m✗ Rejected\033[0m")
            return False
        print("  Please enter y or n.")


def always_allow(cmd: str) -> bool:
    """跳过确认，直接允许（用于测试或受信任调用）。"""
    return True


def always_deny(cmd: str) -> bool:
    """跳过确认，直接拒绝（用于测试或 CI 模式）。"""
    return False
