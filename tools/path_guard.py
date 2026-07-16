"""
tools/path_guard.py

Workspace path boundary helpers.

The agent is allowed to work inside the target repository by default. Access to
paths that resolve outside that repository is allowed only after an explicit
confirmation callback approves it. Without a callback, outside access is denied.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ConfirmCallback = Callable[[str], bool]


@dataclass(frozen=True)
class PathCheck:
    """Result of resolving and authorizing a path."""

    success: bool
    path: Path | None = None
    error: str | None = None
    outside: bool = False


class WorkspaceBoundary:
    """
    Resolve paths against a repository root and confirm escapes.

    Relative paths are interpreted relative to the repository root unless a
    different base is provided. Existing symlinks are resolved, including parent
    symlinks for writes to not-yet-existing files.
    """

    def __init__(
        self,
        repo_path: str | Path,
        confirm_callback: ConfirmCallback | None = None,
    ) -> None:
        self.root = Path(repo_path).resolve()
        self._confirm_callback = confirm_callback
        self._allowed_outside: set[Path] = set() # 用于记录已经被用户手动允许过的“越界路径”。这样同一个越界路径在同一个会话中只需要确认一次，避免反复弹窗打扰用户。

    def resolve(
        self,
        path: str | Path,
        *,
        operation: str,
        base: str | Path | None = None,
    ) -> PathCheck:
        """
        Resolve a path and confirm it if it points outside the workspace.
        """
        raw = Path(path)
        if raw.is_absolute():
            candidate = raw
        else:
            base_path = Path(base).resolve() if base is not None else self.root
            candidate = base_path / raw

        resolved = candidate.resolve(strict=False)
        if self.is_inside(resolved):
            return PathCheck(success=True, path=resolved)
        if resolved in self._allowed_outside:
            return PathCheck(success=True, path=resolved, outside=True)

        reason = (
            f"{operation} outside workspace: {resolved} "
            f"(workspace: {self.root})"
        )
        if not self.confirm_outside(reason):
            return PathCheck(
                success=False,
                path=resolved,
                error=f"Outside workspace access rejected: {resolved}",
                outside=True,
            )
        self._allowed_outside.add(resolved)
        return PathCheck(success=True, path=resolved, outside=True)

    def confirm_outside(self, reason: str) -> bool:
        """Ask the configured callback to allow an outside-workspace access."""
        if self._confirm_callback is None:
            return False
        try:
            return bool(self._confirm_callback(reason))
        except Exception:
            return False

    def is_inside(self, path: str | Path) -> bool:
        """Return True if path resolves inside the workspace root."""
        resolved = Path(path).resolve(strict=False)
        try:
            resolved.relative_to(self.root)
            return True
        except ValueError:
            return False

    def relative_to_root(self, path: str | Path) -> str:
        """Format a path relative to the workspace root when possible."""
        resolved = Path(path).resolve(strict=False)
        try:
            rel = resolved.relative_to(self.root)
        except ValueError:
            return str(resolved)
        return "." if str(rel) == "." else str(rel)
