"""Shared shell-command safety classification helpers."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from tools.path_guard import WorkspaceBoundary
from tools.security_policy import is_sensitive_path, resolve_repo_path


_BLOCKED_PATTERNS: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "chmod -R 777 /",
    "chown -R",
    "> /dev/sda",
)

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


def check_blocked(cmd: str) -> str | None:
    """Return the matched blocked pattern, or None."""
    cmd_lower = cmd.lower()
    for pattern in _BLOCKED_PATTERNS:
        if pattern.lower() in cmd_lower:
            return pattern
    return None


def is_readonly(cmd: str) -> bool:
    """Return True if a command is on the strict read-only allowlist."""
    if _has_shell_metachar(cmd):
        return False
    stripped = cmd.strip().lower()
    try:
        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        return False
    if _uses_recursive_grep(tokens) or _uses_rg_ignore_bypass(tokens):
        return False
    if stripped.startswith("find ") and re.search(r"\s-(exec|delete)\b", stripped):
        return False
    for prefix in _READONLY_PREFIXES:
        if stripped == prefix or stripped.startswith(prefix + " "):
            return True
    return False


def needs_confirm(cmd: str) -> bool:
    """Return True for every command outside the strict read-only allowlist."""
    return not is_readonly(cmd)


def check_outside_path_references(
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


def check_sensitive_path_references(
    cmd: str,
    repo_root: str | Path,
    cwd: str | Path | None = None,
) -> str | None:
    """Reject shell commands that explicitly reference sensitive repo files."""
    root = Path(repo_root).resolve(strict=False)
    base = Path(cwd).resolve(strict=False) if cwd else root

    for candidate in _extract_sensitive_path_candidates(cmd):
        path = resolve_repo_path(candidate, root, base=base)
        if is_sensitive_path(path, repo_root=root):
            return f"Sensitive file reference rejected: {path}"
    return None


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
