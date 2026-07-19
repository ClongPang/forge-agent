"""Shared security helpers for paths used by tools and policy checks."""

from __future__ import annotations

from pathlib import Path


_ENV_TEMPLATE_NAMES = {".env.example", ".env.sample", ".env.template"}
_DEPENDENCY_CONFIG_NAMES = {
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements_test.txt",
}


def resolve_repo_path(
    path: str | Path,
    repo_root: str | Path,
    base: str | Path | None = None,
) -> Path:
    """Resolve a path against the repo root without requiring it to exist."""
    raw = Path(path)
    if raw.is_absolute():
        return raw.resolve(strict=False)
    base_path = (
        Path(base).resolve(strict=False)
        if base is not None
        else Path(repo_root).resolve(strict=False)
    )
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

    if name == ".env" or (
        name.startswith(".env.") and name not in _ENV_TEMPLATE_NAMES
    ):
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


def should_skip_search_path(
    path: str | Path,
    repo_root: str | Path | None = None,
) -> bool:
    """Return True if search/find tools should omit this path from results."""
    candidate = Path(path)
    return "logs" in candidate.parts or is_sensitive_path(candidate, repo_root=repo_root)


def is_high_risk_write_path(
    path: str | Path,
    repo_root: str | Path | None = None,
) -> bool:
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
    if (
        len(rel_parts) >= 3
        and rel_parts[0] == ".github"
        and rel_parts[1] == "workflows"
    ):
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
