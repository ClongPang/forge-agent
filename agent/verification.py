"""
agent/verification.py

Explicit post-run verification commands.
"""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import Any, Sequence

from tools.path_guard import WorkspaceBoundary
from tools.runtime import LocalRuntime, Runtime
from tools.shell_safety import (
    check_blocked,
    check_outside_path_references,
    check_sensitive_path_references,
)


MAX_VERIFY_OUTPUT_CHARS = 12_000
VERIFY_DEFAULT_TIMEOUT = 300

_SHELL_CONTROL_RE = re.compile(r"(\|\||&&|[|;<>`]|[$]\(|[$]\{)")
_DENIED_PREFIXES: tuple[str, ...] = (
    "rm",
    "mv",
    "chmod",
    "chown",
    "sudo",
    "docker",
    "docker-compose",
    "curl",
    "wget",
    "git add",
    "git commit",
    "git push",
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


def empty_verification_report() -> dict[str, Any]:
    """Report shape used when no explicit verification was requested."""
    return {
        "requested": False,
        "status": "not_requested",
        "passed": None,
        "commands": [],
        "passed_count": 0,
        "failed_count": 0,
        "blocked_count": 0,
    }


def run_verifications(
    commands: Sequence[str],
    *,
    repo_path: str | Path,
    runtime: Runtime | None = None,
    timeout: int = VERIFY_DEFAULT_TIMEOUT,
) -> dict:
    """Run explicit verification commands and return a report-ready summary."""
    command_list = [cmd.strip() for cmd in commands if cmd and cmd.strip()]
    if not command_list:
        return empty_verification_report()

    repo_root = Path(repo_path).resolve(strict=False)
    boundary = WorkspaceBoundary(repo_root)
    verifier_runtime = runtime or LocalRuntime(boundary=boundary)

    results = [
        _run_one(
            command,
            repo_root=repo_root,
            boundary=boundary,
            runtime=verifier_runtime,
            timeout=timeout,
        )
        for command in command_list
    ]
    passed_count = sum(item["status"] == "passed" for item in results)
    failed_count = sum(item["status"] in {"failed", "timeout"} for item in results)
    blocked_count = sum(item["status"] == "blocked" for item in results)
    passed = passed_count == len(results)

    return {
        "requested": True,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "commands": results,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "blocked_count": blocked_count,
    }


def _run_one(
    command: str,
    *,
    repo_root: Path,
    boundary: WorkspaceBoundary,
    runtime: Runtime,
    timeout: int,
) -> dict[str, Any]:
    validation_error = _validate_command(
        command,
        repo_root=repo_root,
        boundary=boundary,
    )
    if validation_error is not None:
        return _command_report(
            command=command,
            status="blocked",
            returncode=None,
            duration_seconds=0,
            output="",
            error=validation_error,
        )

    t0 = time.time()
    result = runtime.exec(command, cwd=str(repo_root), timeout=timeout)
    duration = round(time.time() - t0, 3)
    output = _truncate(result.output, MAX_VERIFY_OUTPUT_CHARS)
    timed_out = result.returncode == -1 and "timed out" in result.stderr.lower()
    status = "timeout" if timed_out else ("passed" if result.success else "failed")
    error = None if result.success else result.stderr.strip() or f"Exit code: {result.returncode}"

    return _command_report(
        command=command,
        status=status,
        returncode=result.returncode,
        duration_seconds=duration,
        output=output,
        error=error,
    )


def _command_report(
    *,
    command: str,
    status: str,
    returncode: int | None,
    duration_seconds: float,
    output: str,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "command": command,
        "status": status,
        "returncode": returncode,
        "duration_seconds": duration_seconds,
        "output": output,
        "error": error,
    }


def _validate_command(
    command: str,
    *,
    repo_root: str | Path,
    boundary: WorkspaceBoundary,
) -> str | None:
    """Return a rejection reason for commands that should not be verification."""
    cmd = command.strip()
    if not cmd:
        return "verification command is empty"

    blocked = check_blocked(cmd)
    if blocked:
        return f"verification command blocked for safety: matched '{blocked}'"

    if _SHELL_CONTROL_RE.search(cmd):
        return (
            "verification command must be a single command; "
            "pass multiple --verify options instead of shell control operators"
        )

    outside_error = check_outside_path_references(cmd, boundary)
    if outside_error:
        return outside_error

    sensitive_error = check_sensitive_path_references(cmd, repo_root)
    if sensitive_error:
        return sensitive_error

    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError as exc:
        return f"invalid verification command: {exc}"
    lowered = " ".join(token.lower() for token in tokens)

    for denied_prefix in _DENIED_PREFIXES:
        if lowered == denied_prefix or lowered.startswith(denied_prefix + " "):
            return (
                "verification command rejected because it is not read/test-like: "
                f"{denied_prefix}"
            )
    return None


def _truncate(text: str, max_chars: int) -> str:
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
