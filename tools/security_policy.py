"""
tools/security_policy.py

Small deterministic policy layer for tool-call authorization.

The policy intentionally stays above individual tools: tools still validate and
execute their own inputs, while this module decides whether a requested action
is allowed, needs confirmation, or must be denied before execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class PolicyDecision:
    """Authorization decision for a tool call."""

    kind: str
    reason: str = ""
    prompt: str = ""

    @classmethod
    def allow(cls) -> "PolicyDecision":
        return cls("allow")

    @classmethod
    def deny(cls, reason: str) -> "PolicyDecision":
        return cls("deny", reason=reason)

    @classmethod
    def require_confirm(cls, reason: str, prompt: str | None = None) -> "PolicyDecision":
        return cls("require_confirm", reason=reason, prompt=prompt or reason)


_ENV_TEMPLATE_NAMES = {".env.example", ".env.sample", ".env.template"}
_DEPENDENCY_CONFIG_NAMES = {
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements_test.txt",
}


def resolve_repo_path(path: str | Path, repo_root: str | Path, base: str | Path | None = None) -> Path:
    """Resolve a path against the repo root without requiring it to exist."""
    raw = Path(path)
    if raw.is_absolute():
        return raw.resolve(strict=False)
    base_path = Path(base).resolve(strict=False) if base is not None else Path(repo_root).resolve(strict=False)
    return (base_path / raw).resolve(strict=False)


def is_inside(path: str | Path, repo_root: str | Path) -> bool:
    """Return True if path resolves under repo_root."""
    resolved = Path(path).resolve(strict=False)
    root = Path(repo_root).resolve(strict=False)
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def is_sensitive_path(path: str | Path, repo_root: str | Path | None = None) -> bool:
    """
    Return True for paths that should not be read or exposed to the model.

    Template env files are intentionally allowed so agents can inspect expected
    variable names without seeing real secrets.
    """
    resolved = Path(path)
    name = resolved.name
    parts = resolved.parts

    if name == ".env" or (name.startswith(".env.") and name not in _ENV_TEMPLATE_NAMES):
        return True
    if name.endswith((".pem", ".key")) or name.startswith("id_rsa"):
        return True
    if name == ".git-credentials":
        return True
    for index, part in enumerate(parts[:-1]):
        next_part = parts[index + 1]
        if part == ".git" and next_part == "config":
            return True
        if part == "logs" and resolved.suffix == ".jsonl":
            return True
    if repo_root is not None:
        root = Path(repo_root).resolve(strict=False)
        try:
            rel = resolved.resolve(strict=False).relative_to(root)
        except ValueError:
            rel = resolved
        if len(rel.parts) >= 2 and rel.parts[0] == "logs" and resolved.suffix == ".jsonl":
            return True
    return False


def should_skip_search_path(path: str | Path, repo_root: str | Path | None = None) -> bool:
    """Return True if search/find tools should omit this path from results."""
    candidate = Path(path)
    return "logs" in candidate.parts or is_sensitive_path(candidate, repo_root=repo_root)


def is_high_risk_write_path(path: str | Path, repo_root: str | Path | None = None) -> bool:
    """Return True for paths whose writes should require explicit confirmation."""
    resolved = Path(path).resolve(strict=False)
    rel_parts = resolved.parts
    if repo_root is not None:
        try:
            rel_parts = resolved.relative_to(Path(repo_root).resolve(strict=False)).parts
        except ValueError:
            rel_parts = resolved.parts

    name = resolved.name
    if is_sensitive_path(resolved, repo_root=repo_root):
        return True
    if len(rel_parts) >= 3 and rel_parts[0] == ".github" and rel_parts[1] == "workflows":
        return True
    if len(rel_parts) >= 3 and rel_parts[0] == ".git" and rel_parts[1] == "hooks":
        return True
    if name.startswith(("deploy", "release")):
        return True
    if name in _DEPENDENCY_CONFIG_NAMES:
        return True
    if name.startswith("requirements") and name.endswith(".txt"):
        return True
    if name.startswith("package") and name.endswith(".json"):
        return True
    return False


class ToolPolicy:
    """Authorize tool calls before they reach ToolRegistry."""

    def check(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        repo_root: str | Path,
        modified_files: Iterable[Path] = (),
    ) -> PolicyDecision:
        if tool_name in {"file_read", "file_view"}:
            return self._check_read(params, repo_root)
        if tool_name == "file_write":
            return self._check_write(params, repo_root)
        if tool_name in {"search_text", "find_files", "find_symbol"}:
            return self._check_search(params, repo_root)
        if tool_name == "git_add":
            return self._check_git_add(params, repo_root, modified_files)
        if tool_name == "git_commit":
            return PolicyDecision.require_confirm("git_commit requires user confirmation")
        return PolicyDecision.allow()

    def _check_read(self, params: dict[str, Any], repo_root: str | Path) -> PolicyDecision:
        raw_path = params.get("path")
        if not raw_path:
            return PolicyDecision.allow()
        path = resolve_repo_path(raw_path, repo_root)
        if is_sensitive_path(path, repo_root=repo_root):
            return PolicyDecision.deny(f"Sensitive file read rejected: {path}")
        return PolicyDecision.allow()

    def _check_write(self, params: dict[str, Any], repo_root: str | Path) -> PolicyDecision:
        raw_path = params.get("path")
        if not raw_path:
            return PolicyDecision.allow()
        path = resolve_repo_path(raw_path, repo_root)
        if is_high_risk_write_path(path, repo_root=repo_root):
            return PolicyDecision.require_confirm(f"High-risk file write requires confirmation: {path}")
        return PolicyDecision.allow()

    def _check_search(self, params: dict[str, Any], repo_root: str | Path) -> PolicyDecision:
        raw_path = params.get("path", ".")
        path = resolve_repo_path(raw_path, repo_root)
        if is_sensitive_path(path, repo_root=repo_root):
            return PolicyDecision.deny(f"Sensitive path search rejected: {path}")
        return PolicyDecision.allow()

    def _check_git_add(
        self,
        params: dict[str, Any],
        repo_root: str | Path,
        modified_files: Iterable[Path],
    ) -> PolicyDecision:
        paths = params.get("paths")
        if not paths or paths == ["."] or paths == ".":
            return PolicyDecision.deny("git_add requires explicit paths; refusing implicit git add .")
        if not isinstance(paths, list):
            return PolicyDecision.allow()

        modified = {Path(p).resolve(strict=False) for p in modified_files}
        for raw_path in paths:
            path = resolve_repo_path(raw_path, repo_root)
            if is_sensitive_path(path, repo_root=repo_root):
                return PolicyDecision.deny(f"Refusing to stage sensitive path: {path}")
            if path not in modified:
                return PolicyDecision.require_confirm(
                    f"git_add path was not modified by this agent run: {path}"
                )
        return PolicyDecision.allow()
