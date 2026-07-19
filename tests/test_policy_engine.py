from __future__ import annotations

from pathlib import Path

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import Action, ActionType, EventType, Task, ToolCall, ToolErrorKind
from llm.base import MockBackend
from policy import (
    PermissionMode,
    PolicyContext,
    PolicyDecisionKind,
    PolicyEngine,
    ToolIntent,
)
from tools.base import NoopTool, ToolRegistry


def _context(tmp_path: Path, *, modified_files=()) -> PolicyContext:
    return PolicyContext(
        repo_root=tmp_path,
        modified_files=frozenset(Path(p) for p in modified_files),
    )


def _run_agent(tmp_path, registry, script, confirm_callback=None):
    backend = MockBackend(script + [Action(ActionType.FINISH, "done", message="done")])
    agent = Agent(
        backend,
        registry,
        AgentConfig(max_steps=10, confirm_callback=confirm_callback),
    )
    task = Task(task_id="pol", description="policy", repo_path=str(tmp_path), max_steps=10)
    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        result = agent.run(task, log)
        events = log.replay()
    return result, events


def _policy_events(events):
    return [
        e
        for e in events
        if e.event_type == EventType.POLICY_DECISION
    ]


def _observations(events):
    return [
        e.payload["observation"]
        for e in events
        if e.event_type == EventType.OBSERVATION
    ]


def test_policy_engine_maps_sensitive_read_to_structured_deny(tmp_path):
    decision = PolicyEngine().evaluate(
        ToolIntent("file_read", {"path": ".env"}),
        _context(tmp_path),
    )

    assert decision.kind == PolicyDecisionKind.DENY
    assert "sensitive" in decision.reason.lower()


def test_policy_engine_maps_git_commit_to_structured_confirm(tmp_path):
    decision = PolicyEngine().evaluate(
        ToolIntent("git_commit", {"message": "test"}),
        _context(tmp_path),
    )

    assert decision.kind == PolicyDecisionKind.REQUIRE_CONFIRM
    assert "confirmation" in decision.reason.lower()


def test_policy_engine_allows_unknown_tools_for_registry_validation(tmp_path):
    decision = PolicyEngine().evaluate(
        ToolIntent("unknown_tool", {}),
        _context(tmp_path),
    )

    assert decision.kind == PolicyDecisionKind.ALLOW
    assert decision.reason == ""


def test_policy_engine_requires_confirm_for_non_readonly_shell(tmp_path):
    decision = PolicyEngine().evaluate(
        ToolIntent("shell", {"cmd": "python scripts/migrate.py"}),
        _context(tmp_path),
    )

    assert decision.kind == PolicyDecisionKind.REQUIRE_CONFIRM
    assert "shell command requires confirmation" in decision.reason.lower()
    assert decision.mode == PermissionMode.FIX.value


def test_fix_mode_denies_package_installs(tmp_path):
    decision = PolicyEngine(mode="fix").evaluate(
        ToolIntent("shell", {"cmd": "pip install requests"}),
        _context(tmp_path),
    )

    assert decision.kind == PolicyDecisionKind.DENY
    assert "maintain mode" in decision.reason.lower()
    assert decision.mode == "fix"


def test_maintain_mode_requires_confirm_for_package_installs(tmp_path):
    decision = PolicyEngine(mode=PermissionMode.MAINTAIN).evaluate(
        ToolIntent("shell", {"cmd": "pip install requests"}),
        _context(tmp_path),
    )

    assert decision.kind == PolicyDecisionKind.REQUIRE_CONFIRM
    assert "package install" in decision.reason.lower()
    assert decision.mode == "maintain"


def test_fix_and_maintain_modes_deny_raw_network_and_git_push(tmp_path):
    for mode in ("fix", "maintain"):
        curl_decision = PolicyEngine(mode=mode).evaluate(
            ToolIntent("shell", {"cmd": "curl https://example.com"}),
            _context(tmp_path),
        )
        push_decision = PolicyEngine(mode=mode).evaluate(
            ToolIntent("shell", {"cmd": "git push origin main"}),
            _context(tmp_path),
        )

        assert curl_decision.kind == PolicyDecisionKind.DENY
        assert push_decision.kind == PolicyDecisionKind.DENY
        assert curl_decision.mode == mode


def test_inspect_mode_rejects_writes_tests_and_commits(tmp_path):
    engine = PolicyEngine(mode="inspect")

    write_decision = engine.evaluate(
        ToolIntent("file_write", {"path": "app.py", "content": "x"}),
        _context(tmp_path),
    )
    test_decision = engine.evaluate(
        ToolIntent("test", {"path": "tests/"}),
        _context(tmp_path),
    )
    commit_decision = engine.evaluate(
        ToolIntent("git_commit", {"message": "test"}),
        _context(tmp_path),
    )

    assert write_decision.kind == PolicyDecisionKind.DENY
    assert test_decision.kind == PolicyDecisionKind.DENY
    assert commit_decision.kind == PolicyDecisionKind.DENY
    assert write_decision.mode == "inspect"


def test_inspect_mode_allows_readonly_shell_but_denies_pytest(tmp_path):
    engine = PolicyEngine(mode="inspect")

    read_decision = engine.evaluate(
        ToolIntent("shell", {"cmd": "git status"}),
        _context(tmp_path),
    )
    pytest_decision = engine.evaluate(
        ToolIntent("shell", {"cmd": "pytest tests/"}),
        _context(tmp_path),
    )

    assert read_decision.kind == PolicyDecisionKind.ALLOW
    assert pytest_decision.kind == PolicyDecisionKind.DENY


def test_policy_engine_allows_readonly_shell(tmp_path):
    decision = PolicyEngine().evaluate(
        ToolIntent("shell", {"cmd": "git status"}),
        _context(tmp_path),
    )

    assert decision.kind == PolicyDecisionKind.ALLOW


def test_policy_engine_denies_blocked_shell_command(tmp_path):
    decision = PolicyEngine().evaluate(
        ToolIntent("shell", {"cmd": "rm -rf /"}),
        _context(tmp_path),
    )

    assert decision.kind == PolicyDecisionKind.DENY
    assert "blocked" in decision.reason.lower()


def test_agent_logs_policy_decision_before_observation(tmp_path):
    noop = NoopTool("file_read", output="read")
    registry = ToolRegistry().register(noop)

    _, events = _run_agent(
        tmp_path,
        registry,
        [Action(ActionType.TOOL_CALL, "read", ToolCall("file_read", {"path": "app.py"}))],
    )

    event_types = [event.event_type for event in events]
    policy_index = event_types.index(EventType.POLICY_DECISION)
    observation_index = event_types.index(EventType.OBSERVATION)
    assert policy_index < observation_index

    policy_event = _policy_events(events)[0]
    assert policy_event.payload["tool_name"] == "file_read"
    assert policy_event.payload["decision"]["kind"] == "allow"
    assert noop.call_count == 1


def test_agent_policy_deny_prevents_tool_execution(tmp_path):
    noop = NoopTool("file_read", output="secret")
    registry = ToolRegistry().register(noop)

    _, events = _run_agent(
        tmp_path,
        registry,
        [Action(ActionType.TOOL_CALL, "read env", ToolCall("file_read", {"path": ".env"}))],
    )

    policy_event = _policy_events(events)[0]
    obs = _observations(events)[0]
    assert policy_event.payload["decision"]["kind"] == "deny"
    assert "sensitive" in policy_event.payload["decision"]["reason"].lower()
    assert obs["status"] == "error"
    assert obs["error_kind"] == ToolErrorKind.POLICY_DENIED.value
    assert noop.call_count == 0


def test_agent_policy_confirm_without_callback_fails_closed(tmp_path):
    commit = NoopTool("git_commit", output="committed")
    registry = ToolRegistry().register(commit)

    _, events = _run_agent(
        tmp_path,
        registry,
        [Action(ActionType.TOOL_CALL, "commit", ToolCall("git_commit", {"message": "test"}))],
    )

    policy_event = _policy_events(events)[0]
    obs = _observations(events)[0]
    assert policy_event.payload["decision"]["kind"] == "require_confirm"
    assert "requires confirmation" in obs["error"].lower()
    assert obs["error_kind"] == ToolErrorKind.CONFIRMATION_REQUIRED.value
    assert commit.call_count == 0


def test_agent_policy_confirm_with_approval_executes_tool(tmp_path):
    commit = NoopTool("git_commit", output="committed")
    registry = ToolRegistry().register(commit)

    _, events = _run_agent(
        tmp_path,
        registry,
        [Action(ActionType.TOOL_CALL, "commit", ToolCall("git_commit", {"message": "test"}))],
        confirm_callback=lambda prompt: "git_commit" in prompt,
    )

    policy_event = _policy_events(events)[0]
    obs = _observations(events)[0]
    assert policy_event.payload["decision"]["kind"] == "require_confirm"
    assert obs["status"] == "success"
    assert commit.call_count == 1
