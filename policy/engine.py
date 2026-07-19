"""Default Policy Engine implementation."""

from __future__ import annotations

from policy.types import (
    PermissionMode,
    PolicyContext,
    PolicyDecision,
    PolicyDecisionKind,
    ToolIntent,
)
from tools.shell_safety import (
    check_blocked,
    check_sensitive_path_references,
    needs_confirm,
)
from tools.security_policy import (
    is_high_risk_write_path,
    is_sensitive_path,
    resolve_repo_path,
)


_TEST_COMMAND_PREFIXES: tuple[str, ...] = (
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
)
_PACKAGE_INSTALL_PREFIXES: tuple[str, ...] = (
    "pip install",
    "pip3 install",
    "python -m pip install",
    "python3 -m pip install",
    "uv pip install",
    "npm install",
    "pnpm install",
    "yarn install",
    "bun install",
    "poetry install",
    "poetry add",
)
_SHELL_DENY_PREFIXES: tuple[str, ...] = (
    "git push",
    "sudo",
    "docker",
    "docker-compose",
    "curl",
    "wget",
)


class PolicyEngine:
    """Evaluate tool calls before execution."""

    def __init__(self, mode: PermissionMode | str = PermissionMode.FIX) -> None:
        self.mode = PermissionMode(mode)

    def evaluate(
        self,
        intent: ToolIntent,
        context: PolicyContext,
    ) -> PolicyDecision:
        """Evaluate a tool intent before execution."""
        if intent.tool_name == "shell":
            return _evaluate_shell_policy(intent, context, self.mode)
        if intent.tool_name in {"file_read", "file_view"}:
            return _evaluate_read_policy(intent, context, self.mode)
        if intent.tool_name == "file_write":
            return _evaluate_write_policy(intent, context, self.mode)
        if intent.tool_name in {"search_text", "find_files", "find_symbol"}:
            return _evaluate_search_policy(intent, context, self.mode)
        if intent.tool_name in {"test", "pytest"}:
            return _evaluate_test_policy(self.mode)
        if intent.tool_name == "git_add":
            return _evaluate_git_add_policy(intent, context, self.mode)
        if intent.tool_name == "git_commit":
            return _evaluate_git_commit_policy(self.mode)
        return _allow(self.mode)


def _allow(mode: PermissionMode | str | None = None) -> PolicyDecision:
    return PolicyDecision(PolicyDecisionKind.ALLOW, mode=_mode_value(mode))


def _deny(reason: str, mode: PermissionMode | str | None = None) -> PolicyDecision:
    return PolicyDecision(
        PolicyDecisionKind.DENY,
        reason=reason,
        mode=_mode_value(mode),
    )


def _require_confirm(
    reason: str,
    prompt: str | None = None,
    mode: PermissionMode | str | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        PolicyDecisionKind.REQUIRE_CONFIRM,
        reason=reason,
        prompt=prompt or reason,
        mode=_mode_value(mode),
    )


def _evaluate_shell_policy(
    intent: ToolIntent,
    context: PolicyContext,
    mode: PermissionMode,
) -> PolicyDecision:
    cmd = str(intent.params.get("cmd", "")).strip()
    if not cmd:
        return _allow(mode)

    blocked = check_blocked(cmd)
    if blocked:
        return _deny(f"Command blocked for safety: matched '{blocked}'", mode)

    cwd = intent.params.get("cwd")
    sensitive_error = check_sensitive_path_references(
        cmd,
        repo_root=context.repo_root,
        cwd=cwd if isinstance(cwd, str) else None,
    )
    if sensitive_error:
        return _deny(sensitive_error, mode)

    if mode == PermissionMode.INSPECT:
        if _uses_shell_prefix(cmd, _TEST_COMMAND_PREFIXES):
            return _deny(
                f"inspect mode does not execute tests: {cmd!r}",
                mode,
            )
        if not needs_confirm(cmd):
            return _allow(mode)
        return _deny(
            f"inspect mode is read-only; shell command rejected: {cmd!r}",
            mode,
        )

    if _uses_shell_prefix(cmd, _SHELL_DENY_PREFIXES):
        return _deny(f"Shell command denied by {mode.value} mode: {cmd!r}", mode)

    if _uses_shell_prefix(cmd, _PACKAGE_INSTALL_PREFIXES):
        if mode == PermissionMode.MAINTAIN:
            return _require_confirm(
                f"Package install requires confirmation in maintain mode: {cmd!r}",
                mode=mode,
            )
        return _deny(
            f"Package install requires maintain mode: {cmd!r}",
            mode,
        )

    if needs_confirm(cmd):
        reason = f"Shell command requires confirmation: {cmd!r}"
        return _require_confirm(reason, mode=mode)

    return _allow(mode)


def _evaluate_read_policy(
    intent: ToolIntent,
    context: PolicyContext,
    mode: PermissionMode,
) -> PolicyDecision:
    raw_path = intent.params.get("path")
    if not raw_path:
        return _allow(mode)
    path = resolve_repo_path(raw_path, context.repo_root)
    if is_sensitive_path(path, repo_root=context.repo_root):
        return _deny(f"Sensitive file read rejected: {path}", mode)
    return _allow(mode)


def _evaluate_write_policy(
    intent: ToolIntent,
    context: PolicyContext,
    mode: PermissionMode,
) -> PolicyDecision:
    raw_path = intent.params.get("path")
    if not raw_path:
        return _allow(mode)
    if mode == PermissionMode.INSPECT:
        return _deny("inspect mode is read-only; file_write rejected", mode)
    path = resolve_repo_path(raw_path, context.repo_root)
    if is_high_risk_write_path(path, repo_root=context.repo_root):
        return _require_confirm(
            f"High-risk file write requires confirmation: {path}",
            mode=mode,
        )
    return _allow(mode)


def _evaluate_search_policy(
    intent: ToolIntent,
    context: PolicyContext,
    mode: PermissionMode,
) -> PolicyDecision:
    raw_path = intent.params.get("path", ".")
    path = resolve_repo_path(raw_path, context.repo_root)
    if is_sensitive_path(path, repo_root=context.repo_root):
        return _deny(f"Sensitive path search rejected: {path}", mode)
    return _allow(mode)


def _evaluate_test_policy(mode: PermissionMode) -> PolicyDecision:
    if mode == PermissionMode.INSPECT:
        return _deny("inspect mode does not execute tests", mode)
    return _allow(mode)


def _evaluate_git_add_policy(
    intent: ToolIntent,
    context: PolicyContext,
    mode: PermissionMode,
) -> PolicyDecision:
    if mode == PermissionMode.INSPECT:
        return _deny("inspect mode is read-only; git_add rejected", mode)

    paths = intent.params.get("paths")
    if not paths or paths == ["."] or paths == ".":
        return _deny(
            "git_add requires explicit paths; refusing implicit git add .",
            mode,
        )
    if not isinstance(paths, list):
        return _allow(mode)

    modified = {path.resolve(strict=False) for path in context.modified_files}
    for raw_path in paths:
        path = resolve_repo_path(raw_path, context.repo_root)
        if is_sensitive_path(path, repo_root=context.repo_root):
            return _deny(f"Refusing to stage sensitive path: {path}", mode)
        if path not in modified:
            return _require_confirm(
                f"git_add path was not modified by this agent run: {path}",
                mode=mode,
            )
    return _allow(mode)


def _evaluate_git_commit_policy(mode: PermissionMode) -> PolicyDecision:
    if mode == PermissionMode.INSPECT:
        return _deny("inspect mode is read-only; git_commit rejected", mode)
    return _require_confirm("git_commit requires user confirmation", mode=mode)


def _matches_shell_prefix(cmd: str, prefixes: tuple[str, ...]) -> bool:
    stripped = " ".join(cmd.strip().lower().split())
    for prefix in prefixes:
        normalized = " ".join(prefix.lower().split())
        if stripped == normalized or stripped.startswith(normalized + " "):
            return True
    return False


def _uses_shell_prefix(cmd: str, prefixes: tuple[str, ...]) -> bool:
    if _matches_shell_prefix(cmd, prefixes):
        return True
    normalized = cmd.replace("&&", ";").replace("||", ";").replace("|", ";")
    for token in "()":
        normalized = normalized.replace(token, ";")
    for segment in normalized.split(";"):
        if segment and _matches_shell_prefix(segment, prefixes):
            return True
    return False


def _mode_value(mode: PermissionMode | str | None) -> str | None:
    if mode is None:
        return None
    if isinstance(mode, PermissionMode):
        return mode.value
    return str(mode)
