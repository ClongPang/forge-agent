"""Default Policy Engine implementation."""

from __future__ import annotations

from policy.types import (
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


class PolicyEngine:
    """Evaluate tool calls before execution."""

    def evaluate(
        self,
        intent: ToolIntent,
        context: PolicyContext,
    ) -> PolicyDecision:
        """Evaluate a tool intent before execution."""
        if intent.tool_name == "shell":
            return _evaluate_shell_policy(intent, context)
        if intent.tool_name in {"file_read", "file_view"}:
            return _evaluate_read_policy(intent, context)
        if intent.tool_name == "file_write":
            return _evaluate_write_policy(intent, context)
        if intent.tool_name in {"search_text", "find_files", "find_symbol"}:
            return _evaluate_search_policy(intent, context)
        if intent.tool_name == "git_add":
            return _evaluate_git_add_policy(intent, context)
        if intent.tool_name == "git_commit":
            return _require_confirm("git_commit requires user confirmation")
        return PolicyDecision(PolicyDecisionKind.ALLOW)


def _allow() -> PolicyDecision:
    return PolicyDecision(PolicyDecisionKind.ALLOW)


def _deny(reason: str) -> PolicyDecision:
    return PolicyDecision(PolicyDecisionKind.DENY, reason=reason)


def _require_confirm(reason: str, prompt: str | None = None) -> PolicyDecision:
    return PolicyDecision(
        PolicyDecisionKind.REQUIRE_CONFIRM,
        reason=reason,
        prompt=prompt or reason,
    )


def _evaluate_shell_policy(
    intent: ToolIntent,
    context: PolicyContext,
) -> PolicyDecision:
    cmd = str(intent.params.get("cmd", "")).strip()
    if not cmd:
        return _allow()

    blocked = check_blocked(cmd)
    if blocked:
        return _deny(f"Command blocked for safety: matched '{blocked}'")

    cwd = intent.params.get("cwd")
    sensitive_error = check_sensitive_path_references(
        cmd,
        repo_root=context.repo_root,
        cwd=cwd if isinstance(cwd, str) else None,
    )
    if sensitive_error:
        return _deny(sensitive_error)

    if needs_confirm(cmd):
        reason = f"Shell command requires confirmation: {cmd!r}"
        return _require_confirm(reason)

    return _allow()


def _evaluate_read_policy(
    intent: ToolIntent,
    context: PolicyContext,
) -> PolicyDecision:
    raw_path = intent.params.get("path")
    if not raw_path:
        return _allow()
    path = resolve_repo_path(raw_path, context.repo_root)
    if is_sensitive_path(path, repo_root=context.repo_root):
        return _deny(f"Sensitive file read rejected: {path}")
    return _allow()


def _evaluate_write_policy(
    intent: ToolIntent,
    context: PolicyContext,
) -> PolicyDecision:
    raw_path = intent.params.get("path")
    if not raw_path:
        return _allow()
    path = resolve_repo_path(raw_path, context.repo_root)
    if is_high_risk_write_path(path, repo_root=context.repo_root):
        return _require_confirm(f"High-risk file write requires confirmation: {path}")
    return _allow()


def _evaluate_search_policy(
    intent: ToolIntent,
    context: PolicyContext,
) -> PolicyDecision:
    raw_path = intent.params.get("path", ".")
    path = resolve_repo_path(raw_path, context.repo_root)
    if is_sensitive_path(path, repo_root=context.repo_root):
        return _deny(f"Sensitive path search rejected: {path}")
    return _allow()


def _evaluate_git_add_policy(
    intent: ToolIntent,
    context: PolicyContext,
) -> PolicyDecision:
    paths = intent.params.get("paths")
    if not paths or paths == ["."] or paths == ".":
        return _deny("git_add requires explicit paths; refusing implicit git add .")
    if not isinstance(paths, list):
        return _allow()

    modified = {path.resolve(strict=False) for path in context.modified_files}
    for raw_path in paths:
        path = resolve_repo_path(raw_path, context.repo_root)
        if is_sensitive_path(path, repo_root=context.repo_root):
            return _deny(f"Refusing to stage sensitive path: {path}")
        if path not in modified:
            return _require_confirm(
                f"git_add path was not modified by this agent run: {path}"
            )
    return _allow()
