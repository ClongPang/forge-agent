"""Data contracts for tool-call policy decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PolicyDecisionKind(str, Enum):
    """First-phase authorization decision kinds."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRM = "require_confirm"


class PermissionMode(str, Enum):
    """Built-in permission bundles for a run."""

    INSPECT = "inspect"
    FIX = "fix"
    MAINTAIN = "maintain"


@dataclass(frozen=True)
class ToolIntent:
    """A model-requested tool call before authorization and execution."""

    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyContext:
    """Runtime metadata required to evaluate a tool intent."""

    repo_root: Path
    modified_files: frozenset[Path] = frozenset()


@dataclass(frozen=True)
class PolicyDecision:
    """Structured authorization result for a tool intent."""

    kind: PolicyDecisionKind
    reason: str = ""
    prompt: str = ""
    mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "prompt": self.prompt,
            "mode": self.mode,
        }
