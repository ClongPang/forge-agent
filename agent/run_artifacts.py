"""
agent/run_artifacts.py

Build stable, machine-readable artifacts for a completed run.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.event_log import EventLog, summarize_run
from agent.task import EventType, RunResult, Task
from agent.verification import empty_verification_report


@dataclass(frozen=True)
class RunArtifactPaths:
    """Filesystem paths for the artifacts emitted by one run."""

    directory: Path
    events: Path
    report: Path
    diff: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "directory": str(self.directory),
            "events": str(self.events),
            "report": str(self.report),
            "diff": str(self.diff),
        }


def write_run_artifacts(
    *,
    task: Task,
    result: RunResult,
    log: EventLog,
    duration_seconds: float,
    permission_mode: str | None = None,
    verification: dict[str, Any] | None = None,
) -> RunArtifactPaths:
    """
    Write the stable artifact set for a completed run.

    The original event log remains at its existing JSONL path. A copy named
    ``events.jsonl`` is placed next to ``report.json`` and ``diff.patch`` so
    automation can consume fixed filenames.
    """
    artifact_dir = log.path.with_suffix("")
    artifact_dir.mkdir(parents=True, exist_ok=True)

    paths = RunArtifactPaths(
        directory=artifact_dir,
        events=artifact_dir / "events.jsonl",
        report=artifact_dir / "report.json",
        diff=artifact_dir / "diff.patch",
    )

    shutil.copyfile(log.path, paths.events)
    patch = result.patch or ""
    paths.diff.write_text(patch, encoding="utf-8")

    events = log.replay()
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task": task.to_dict(),
        "result": _result_summary(result),
        "permission_mode": permission_mode,
        "verification": verification or empty_verification_report(),
        "stats": summarize_run(log),
        "duration_seconds": round(duration_seconds, 3),
        "changed_files": extract_changed_files_from_patch(patch),
        "tool_calls": extract_tool_calls(events),
        "policy": summarize_policy_decisions(events),
        "artifacts": {
            **paths.to_dict(),
            "source_event_log": str(log.path),
        },
    }
    paths.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return paths


def _result_summary(result: RunResult) -> dict[str, Any]:
    data = result.to_dict()
    patch = data.pop("patch", None) or ""
    data["has_patch"] = bool(patch)
    data["patch_chars"] = len(patch)
    return data


def extract_changed_files_from_patch(patch: str) -> list[str]:
    """Extract changed file paths from git diff text."""
    changed: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        for raw in parts[2:4]:
            path = raw[2:] if raw.startswith(("a/", "b/")) else raw
            if path != "/dev/null":
                changed.add(path)
    return sorted(changed)


def extract_tool_calls(events) -> list[dict[str, Any]]:
    """Return a compact list of tool calls requested during a run."""
    calls: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != EventType.ACTION:
            continue
        action = event.payload.get("action", {})
        tool_calls = action.get("tool_calls") or []
        if not tool_calls and action.get("tool_call"):
            tool_calls = [action["tool_call"]]
        for tool_call in tool_calls:
            calls.append({
                "step": event.payload.get("step"),
                "tool": tool_call.get("name"),
                "params": tool_call.get("params") or {},
            })
    return calls


def summarize_policy_decisions(events) -> dict[str, Any]:
    """Summarize policy decisions without hiding the detailed JSONL log."""
    summary: dict[str, Any] = {
        "total": 0,
        "by_decision": {},
        "denied": 0,
        "confirmation_required": 0,
    }
    for event in events:
        if event.event_type != EventType.POLICY_DECISION:
            continue
        summary["total"] += 1
        decision = event.payload.get("decision", {})
        outcome = _decision_outcome(decision)
        summary["by_decision"][outcome] = (
            summary["by_decision"].get(outcome, 0) + 1
        )
        lowered = outcome.lower()
        if "deny" in lowered or "reject" in lowered:
            summary["denied"] += 1
        if "confirm" in lowered:
            summary["confirmation_required"] += 1
    return summary


def _decision_outcome(decision: dict[str, Any]) -> str:
    for key in ("kind", "decision", "action", "outcome", "status"):
        value = decision.get(key)
        if value is not None:
            return str(value)
    allowed = decision.get("allowed")
    if allowed is not None:
        return "allow" if allowed else "deny"
    return "unknown"
