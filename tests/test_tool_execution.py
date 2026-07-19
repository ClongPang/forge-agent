from __future__ import annotations

from pathlib import Path

from agent.event_log import EventLog
from agent.task import EventType, Task, ToolCall, ToolErrorKind
from agent.telemetry import AgentTracer
from agent.tool_execution import ToolExecutionRequest, ToolExecutionService
from policy import PolicyDecision, PolicyDecisionKind
from tools.base import NoopTool, ToolRegistry
from tools.file_tool import FileWriteTool
from tools.path_guard import WorkspaceBoundary


class CountingPolicy:
    def __init__(self, decision: PolicyDecision | None = None) -> None:
        self.calls = 0
        self.decision = decision or PolicyDecision(PolicyDecisionKind.ALLOW)

    def evaluate(self, intent, context):
        self.calls += 1
        return self.decision


def _request(tmp_path, *, tool_call, registry, policy=None, confirm_callback=None):
    task = Task(task_id="execsvc", description="tool", repo_path=str(tmp_path))
    log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
    service = ToolExecutionService(
        registry,
        policy or CountingPolicy(),
        confirm_callback=confirm_callback,
    )
    request = ToolExecutionRequest(
        step=1,
        tool_call=tool_call,
        repo_root=Path(tmp_path),
        modified_files=frozenset(),
        log=log,
        tracer=AgentTracer(),
    )
    return service, request, log


def test_prepare_failure_skips_policy_and_logs_observation(tmp_path):
    policy = CountingPolicy()
    registry = ToolRegistry()
    service, request, log = _request(
        tmp_path,
        tool_call=ToolCall("missing", {}),
        registry=registry,
        policy=policy,
    )

    try:
        outcome = service.execute(request)
        events = log.replay()
    finally:
        log.close()

    assert outcome.observation.status.value == "error"
    assert "Unknown tool 'missing'" in outcome.observation.error
    assert outcome.observation.error_kind == ToolErrorKind.UNKNOWN_TOOL
    assert "registered tool names" in outcome.observation.recovery_hint
    assert policy.calls == 0
    assert [event.event_type for event in events] == [EventType.OBSERVATION]
    assert events[0].payload["observation"]["status"] == "error"
    assert events[0].payload["observation"]["error_kind"] == "unknown_tool"


def test_invalid_params_skip_policy_and_do_not_execute(tmp_path):
    tool = FileWriteTool(boundary=WorkspaceBoundary(tmp_path))
    policy = CountingPolicy()
    registry = ToolRegistry().register(tool)
    service, request, log = _request(
        tmp_path,
        tool_call=ToolCall("file_write", {"path": "app.py"}),
        registry=registry,
        policy=policy,
    )

    try:
        outcome = service.execute(request)
        events = log.replay()
    finally:
        log.close()

    assert outcome.observation.status.value == "error"
    assert "Invalid params for file_write" in outcome.observation.error
    assert outcome.observation.error_kind == ToolErrorKind.INVALID_PARAMS
    assert "tool schema" in outcome.observation.recovery_hint
    assert policy.calls == 0
    assert not (tmp_path / "app.py").exists()
    assert [event.event_type for event in events] == [EventType.OBSERVATION]


def test_confirm_callback_exception_rejects_without_execution(tmp_path):
    commit = NoopTool("git_commit", output="committed")
    policy = CountingPolicy(
        PolicyDecision(
            PolicyDecisionKind.REQUIRE_CONFIRM,
            reason="git_commit requires user confirmation",
            prompt="confirm git_commit",
        )
    )
    registry = ToolRegistry().register(commit)

    def broken_confirm(_prompt):
        raise RuntimeError("ui unavailable")

    service, request, log = _request(
        tmp_path,
        tool_call=ToolCall("git_commit", {"message": "test"}),
        registry=registry,
        policy=policy,
        confirm_callback=broken_confirm,
    )

    try:
        outcome = service.execute(request)
        events = log.replay()
    finally:
        log.close()

    assert outcome.observation.status.value == "error"
    assert "rejected by user" in outcome.observation.error
    assert outcome.observation.error_kind == ToolErrorKind.CONFIRMATION_REJECTED
    assert commit.call_count == 0
    assert policy.calls == 1
    assert [event.event_type for event in events] == [
        EventType.POLICY_DECISION,
        EventType.OBSERVATION,
    ]


def test_successful_file_write_returns_modified_path_delta(tmp_path):
    registry = ToolRegistry().register(
        FileWriteTool(boundary=WorkspaceBoundary(tmp_path))
    )
    service, request, log = _request(
        tmp_path,
        tool_call=ToolCall("file_write", {"path": "app.py", "content": "x = 1\n"}),
        registry=registry,
    )

    try:
        outcome = service.execute(request)
    finally:
        log.close()

    expected = Path(tmp_path / "app.py").resolve(strict=False)
    assert outcome.observation.status.value == "success"
    assert outcome.modified_path == expected
    assert expected.read_text() == "x = 1\n"
