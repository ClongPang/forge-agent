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
- chat 模式 / 交互模式传入真实的终端确认函数
- 测试时传入 mock，不需要真实终端
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Callable

from tools.base import BaseTool, ToolResult
from tools.path_guard import WorkspaceBoundary
from tools.runtime import LocalRuntime, Runtime
from tools.security_policy import is_sensitive_path, resolve_repo_path


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

MAX_OUTPUT_CHARS = 8_000

# 硬拦截黑名单（永不执行，不问用户）
_BLOCKED_PATTERNS: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",       # fork bomb
    "chmod -R 777 /",
    "chown -R",
    "> /dev/sda",
)

# 只读命令前缀白名单（直接执行，不询问）
_READONLY_PREFIXES: tuple[str, ...] = (
    "ls", "ll", "la",
    "cat", "head", "tail", "less", "more",
    "echo", "printf",
    "pwd", "whoami", "which", "type",
    "find", "locate",
    "grep", "egrep", "fgrep", "rg", "ag",
    "wc", "sort", "uniq", "cut", "awk", "sed -n",
    "diff", "diff3",
    "file", "stat",
    "python -m pytest", "python3 -m pytest", "pytest",
    "git status", "git diff", "git log", "git show",
    "git branch", "git tag", "git remote",
    "git stash list",
    "tree",
    "ps", "top", "htop",
    "df", "du",
    "uname", "hostname",
    "date", "cal",
    "man", "help",
)

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
    """

    def __init__(
        self,
        confirm_callback: ConfirmCallback | None = None,
        runtime: Runtime | None = None,
        boundary: WorkspaceBoundary | None = None,
    ) -> None:
        self._confirm_callback = confirm_callback
        self._boundary = boundary
        self._runtime = runtime or LocalRuntime(boundary=boundary)

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
        blocked = _check_blocked(cmd)
        if blocked:
            return ToolResult(
                success=False,
                output="",
                error=f"Command blocked for safety: matched '{blocked}'",
            )

        outside_error = _check_outside_path_references(cmd, self._boundary)
        if outside_error:
            return ToolResult(success=False, output="", error=outside_error)

        sensitive_error = _check_sensitive_path_references(cmd, self._boundary, cwd)
        if sensitive_error:
            return ToolResult(success=False, output="", error=sensitive_error)

        # 层 2：白名单免确认
        if not _needs_confirm(cmd):
            return self._run(cmd, timeout, cwd)

        # 层 3：权限确认
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


# ---------------------------------------------------------------------------
# 辅助函数（对外暴露供测试）
# ---------------------------------------------------------------------------

def _check_blocked(cmd: str) -> str | None:
    """返回匹配到的黑名单 pattern，没有匹配返回 None。"""
    cmd_lower = cmd.lower()
    for pattern in _BLOCKED_PATTERNS:
        if pattern.lower() in cmd_lower:
            return pattern
    return None


def _is_readonly(cmd: str) -> bool:
    """
    判断命令是否在只读白名单里。
    包含 > 写重定向的命令不算只读（即使命令名在白名单里）。
    """
    import re as _re
    if _has_shell_metachar(cmd):
        return False
    stripped = cmd.strip().lower()
    try:
        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        return False
    if _uses_recursive_grep(tokens) or _uses_rg_ignore_bypass(tokens):
        return False
    if stripped.startswith("find ") and _re.search(r"\s-(exec|delete)\b", stripped):
        return False
    for prefix in _READONLY_PREFIXES:
        if stripped == prefix or stripped.startswith(prefix + " "):
            return True
    return False


def _needs_confirm(cmd: str) -> bool:
    """
    判断命令是否需要用户确认。
    只有严格只读白名单命令免确认，其余命令都需要确认。
    """
    return not _is_readonly(cmd)


def _has_shell_metachar(cmd: str) -> bool:
    """Return True if a command uses shell syntax that can hide side effects."""
    return bool(re.search(r"(\|\||&&|[|;<>`]\s*|\$\(|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)", cmd))


def _uses_recursive_grep(tokens: list[str]) -> bool:
    """Recursive grep can read ignored secrets; use search_text for repo search."""
    if not tokens or tokens[0] not in {"grep", "egrep", "fgrep"}:
        return False
    for token in tokens[1:]:
        if token in {"--recursive", "--dereference-recursive"}:
            return True
        if token.startswith("--"):
            continue
        if token.startswith("-") and any(flag in token[1:] for flag in ("r", "R")):
            return True
    return False


def _uses_rg_ignore_bypass(tokens: list[str]) -> bool:
    """Reject rg/ag flags that bypass hidden-file or ignore-file protections."""
    if not tokens or tokens[0] not in {"rg", "ag"}:
        return False
    for token in tokens[1:]:
        if token in {"--hidden", "--no-ignore", "--no-ignore-vcs", "--no-ignore-parent"}:
            return True
        if token.startswith("--no-ignore") or token.startswith("-u"):
            return True
    return False


_PATH_CANDIDATE_RE = re.compile(
    r"(?<![\w:])(/[^ \t\r\n'\";|&<>]+|(?:\.\./)[^ \t\r\n'\";|&<>]+)"
)


def _check_outside_path_references(
    cmd: str,
    boundary: WorkspaceBoundary | None,
) -> str | None:
    """Best-effort confirmation for explicit paths outside the workspace."""
    if boundary is None:
        return None

    for candidate in _extract_path_candidates(cmd):
        check = boundary.resolve(
            candidate,
            operation=f"shell command references {candidate!r}",
        )
        if not check.success:
            return check.error
    return None


def _extract_path_candidates(cmd: str) -> list[str]:
    """Extract absolute and parent-traversal path references from a shell string."""
    candidates: set[str] = set()

    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        tokens = []

    for token in tokens:
        parts = [token]
        if "=" in token:
            parts.append(token.split("=", 1)[1])
        for part in parts:
            cleaned = part.strip()
            if cleaned.startswith(("/", "../")) or cleaned in {".."}:
                candidates.add(cleaned)

    candidates.update(m.group(1) for m in _PATH_CANDIDATE_RE.finditer(cmd))
    return sorted(candidates)


def _check_sensitive_path_references(
    cmd: str,
    boundary: WorkspaceBoundary | None,
    cwd: str | None = None,
) -> str | None:
    """Reject shell commands that explicitly reference sensitive repo files."""
    repo_root = boundary.root if boundary is not None else Path(cwd or Path.cwd()).resolve(strict=False)
    base = Path(cwd).resolve(strict=False) if cwd else repo_root

    for candidate in _extract_sensitive_path_candidates(cmd):
        path = resolve_repo_path(candidate, repo_root, base=base)
        if is_sensitive_path(path, repo_root=repo_root):
            return f"Sensitive file reference rejected: {path}"
    return None


def _extract_sensitive_path_candidates(cmd: str) -> list[str]:
    """Extract explicit path-like arguments that could target sensitive files."""
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return []

    candidates: set[str] = set()
    for token in tokens[1:]:
        values = [token]
        if "=" in token:
            values.append(token.split("=", 1)[1])
        for value in values:
            cleaned = value.strip()
            if not cleaned or cleaned.startswith("-"):
                continue
            if _looks_like_sensitive_path_arg(cleaned):
                candidates.add(cleaned)
    return sorted(candidates)


def _looks_like_sensitive_path_arg(value: str) -> bool:
    """Return True for path arguments worth checking against sensitive patterns."""
    name = Path(value).name
    return (
        value.startswith(("/", "./", "../", ".git/", "logs/"))
        or "/" in value
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith((".pem", ".key"))
        or name.startswith("id_rsa")
        or name == ".git-credentials"
    )


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
