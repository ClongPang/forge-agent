---
title: Tool Execution Pipeline Refactor
version: 1.0
date_created: 2026-07-19
last_updated: 2026-07-19
owner: Forge Agent
tags: [architecture, tool-system, policy, security, observability, refactor]
---

# Introduction

This specification defines a stable refactor for Forge Agent's tool execution pipeline. The goal is to extract tool-call preparation and execution orchestration out of `agent/core.py` while preserving current runtime behavior.

The refactor introduces `ToolRegistry.prepare_call()` and a `ToolExecutionService`. It is intended to support the existing unified Policy Engine by giving policy evaluation a canonical, schema-validated tool intent instead of raw model output.

## 1. Purpose & Scope

The purpose of this specification is to define the requirements, constraints, interfaces, and validation criteria for a small, testable tool execution pipeline.

In scope:

- Add a preparation step to `ToolRegistry` that resolves a tool, normalizes parameters where supported, and validates schema constraints without executing the tool.
- Add a `ToolExecutionService` that coordinates tool preparation, policy evaluation, confirmation, tool execution, policy logging, observation logging, telemetry, and modified-file delta reporting.
- Update `Agent` so the main ReAct loop delegates one tool call to the service instead of directly coordinating policy and registry execution.
- Preserve current command-line, chat, GitHub Issue, and SWE-bench behavior.
- Preserve current tool schemas, tool names, error status semantics, and security behavior.

Out of scope for this phase:

- Full `AgentLoop` and `AgentRunner` separation.
- Async tool execution or read-only tool batching.
- Plugin-based tool discovery.
- Tool capability metadata such as `read_only`, `exclusive`, or `network`.
- Dynamic tool exposure based on policy.
- New policy modes, risk levels, or rule identifiers.
- Full migration of `ShellTool` internal confirmation logic into the Policy Engine.
- Changes to `ObservationStatus`.
- New user-facing CLI commands.

Intended audience:

- Agent core maintainers.
- Tool system maintainers.
- Policy Engine maintainers.
- Future coding agents implementing or reviewing this refactor.

Assumptions:

- Forge Agent remains a synchronous Python 3.11+ codebase in this phase.
- Existing tests for tools, policy, path guards, confirmation, streaming, chat, telemetry, and SWE-bench must continue to pass.
- The existing EventLog remains the local source of truth for execution audit.

## 2. Definitions

- **Agent**: The ReAct loop currently implemented in `agent/core.py`.
- **Tool Call**: A model-requested invocation represented by `agent.task.ToolCall`.
- **Raw Tool Call**: A tool call before registry lookup and parameter validation.
- **Prepared Tool Call**: A registry-resolved and schema-validated tool call that is safe to pass to the Policy Engine and tool execution.
- **Tool Registry**: The component that stores tools, exposes tool schemas, validates tool parameters, and dispatches tool execution.
- **Tool Execution Service**: The orchestration component that runs the complete lifecycle for one tool call.
- **Policy Engine**: The component that evaluates a prepared tool intent and returns `allow`, `deny`, or `require_confirm`.
- **Confirmation Callback**: A callable that asks the user whether a confirmation-required action may proceed.
- **Observation**: The normalized tool result written to EventLog and injected into model history.
- **Modified-File Delta**: A path reported by the execution service when a successful tool call modifies a repository file during the current run.
- **Fail Closed**: A behavior where a risky action is rejected if confirmation is required but unavailable.

## 3. Requirements, Constraints & Guidelines

- **REQ-001**: `ToolRegistry` shall expose `prepare_call(name: str, params: Any) -> PreparedToolCall`.
- **REQ-002**: `prepare_call()` shall resolve tool names exactly. It shall not execute tools.
- **REQ-003**: `prepare_call()` shall return a structured preparation result for unknown tools, non-object parameters, and schema validation failures.
- **REQ-004**: `prepare_call()` shall reuse the existing JSON Schema validation semantics currently used by `ToolRegistry.execute_tool()`.
- **REQ-005**: `ToolRegistry` shall expose an execution path for already prepared calls so validation is not duplicated in the service.
- **REQ-006**: `ToolRegistry.execute_tool()` shall remain available and shall preserve its existing public behavior by internally using preparation plus prepared execution.
- **REQ-007**: A new `ToolExecutionService` shall own the lifecycle for one tool call after the Agent has decided to execute tools for a step.
- **REQ-008**: `ToolExecutionService` shall evaluate policy only after successful tool preparation.
- **REQ-009**: Policy evaluation shall receive canonical prepared parameters, not raw model output.
- **REQ-010**: Prepare failures shall not call the Policy Engine and shall not execute tools.
- **REQ-011**: A denied policy decision shall prevent tool execution.
- **REQ-012**: A confirmation-required policy decision without a confirmation callback shall fail closed.
- **REQ-013**: A confirmation-required policy decision shall execute the prepared tool only when the confirmation callback returns true.
- **REQ-014**: A confirmation callback exception shall be treated as rejection.
- **REQ-015**: The service shall write `policy_decision` events for calls that reach policy evaluation.
- **REQ-016**: The service shall preserve observation logging order relative to policy decisions and tool execution.
- **REQ-017**: The service shall return a `ToolExecutionOutcome` containing the `ToolResult`, `Observation`, optional `PolicyDecision`, preparation error state, and optional modified-file delta.
- **REQ-018**: The Agent shall remain responsible for ReAct loop control, history updates, reflection triggers, and accumulated `modified_files` state.
- **REQ-019**: The service shall report modified-file deltas instead of mutating the Agent's accumulated state directly.
- **REQ-020**: Telemetry tool spans shall continue to include policy decision kind and reason when a policy decision exists.
- **REQ-021**: Tool execution behavior shall remain sequential in this phase.

- **SEC-001**: The refactor shall not weaken existing sensitive-path, workspace-boundary, shell confirmation, git staging, or high-risk write behavior.
- **SEC-002**: A prepared call failure shall not be converted into an allow decision.
- **SEC-003**: Policy allow shall remain pre-authorization only. Individual tools shall retain local validation and safety checks.
- **SEC-004**: The service shall not log full tool parameters unless an existing logging path already does so.
- **SEC-005**: The service shall not catch and suppress security errors in a way that makes the observation look successful.

- **OBS-001**: Policy decision events shall be emitted before the corresponding observation for prepared calls.
- **OBS-002**: Prepare failures shall remain visible through the action event and error observation.
- **OBS-003**: Telemetry failures shall not affect tool execution results.
- **OBS-004**: Existing EventLog replay and summary behavior shall continue to work with policy and observation events.

- **CON-001**: Do not introduce an async runner in this phase.
- **CON-002**: Do not introduce plugin discovery or dynamic tool capabilities in this phase.
- **CON-003**: Do not introduce new `ObservationStatus` values in this phase.
- **CON-004**: Do not add future-only fields to `PreparedToolCall`, `ToolExecutionRequest`, or `ToolExecutionOutcome`.
- **CON-005**: Keep `ToolExecutionService` in the agent execution layer, not inside the generic tool registry.
- **CON-006**: Keep `PolicyEngine` free of logging, confirmation, telemetry, and tool execution side effects.

- **GUD-001**: Prefer explicit small dataclasses over dictionaries for internal execution contracts.
- **GUD-002**: Preserve existing public methods first, then route them through new internals.
- **GUD-003**: Treat preparation errors as malformed or invalid tool calls, not authorization decisions.
- **GUD-004**: Keep Agent core readable by making the tool execution call site a single service invocation.
- **GUD-005**: Use nanobot's separation of registry preparation and runner execution as a design reference, but do not copy unrelated async, plugin, or channel abstractions.

## 4. Interfaces & Data Contracts

### 4.1 Package and Module Layout

Required first-phase layout:

```text
agent/
  tool_execution.py

tools/
  base.py

policy/
  types.py
  engine.py
```

No new top-level package is required for the execution service. The service belongs in `agent/` because it coordinates agent runtime concerns such as EventLog, telemetry, task context, and confirmation.

### 4.2 PreparedToolCall

`PreparedToolCall` shall be defined in `tools/base.py` or another tools-layer module imported by `tools/base.py`.

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class PreparedToolCall:
    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
    tool: BaseTool | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.tool is not None and self.error is None
```

Rules:

- `tool_name` shall be the raw requested tool name.
- `params` shall be the canonical dictionary that passed validation when `ok` is true.
- `tool` shall be the resolved `BaseTool` when `ok` is true.
- `error` shall contain the user-facing error message when preparation fails.
- `ok` shall be true only when a tool exists and params are valid.
- The class shall not include raw params, policy fields, rule identifiers, risk levels, telemetry metadata, or execution output.

### 4.3 ToolRegistry Interface

Required methods:

```python
class ToolRegistry:
    def prepare_call(self, name: str, params: Any) -> PreparedToolCall:
        ...

    def execute_prepared(self, prepared: PreparedToolCall) -> ToolResult:
        ...

    def execute_tool(self, name: str, params: Any) -> ToolResult:
        prepared = self.prepare_call(name, params)
        if not prepared.ok:
            return ToolResult(success=False, output="", error=prepared.error)
        return self.execute_prepared(prepared)
```

Rules:

- `execute_prepared()` shall require `prepared.ok`.
- If `execute_prepared()` receives a failed preparation result, it shall return an error `ToolResult` rather than executing.
- `execute_tool()` shall preserve existing unknown-tool, invalid-params, and unexpected-exception behavior as closely as possible.
- `get_schemas()`, `tool_names`, `register()`, and existing registry semantics shall remain unchanged.

### 4.4 ToolExecutionRequest

`ToolExecutionRequest` shall be defined in `agent/tool_execution.py`.

```python
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class ToolExecutionRequest:
    step: int
    tool_call: ToolCall
    task: Task
    modified_files: frozenset[Path]
    log: EventLog
    tracer: AgentTracer
```

Rules:

- `modified_files` shall be an immutable snapshot from Agent state.
- `log` and `tracer` are per-run dependencies passed by the Agent.
- The request shall not include entrypoint, model name, policy mode, risk level, or future-only fields.

### 4.5 ToolExecutionOutcome

`ToolExecutionOutcome` shall be defined in `agent/tool_execution.py`.

```python
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class ToolExecutionOutcome:
    result: ToolResult
    observation: Observation
    prepared: PreparedToolCall
    policy_decision: PolicyDecision | None = None
    modified_path: Path | None = None
```

Rules:

- `policy_decision` shall be `None` when preparation fails before policy evaluation.
- `modified_path` shall be set only for successful file-modifying tools whose target path resolves inside the task repository.
- The Agent shall add `modified_path` to its accumulated modified-file set when present.

### 4.6 ToolExecutionService

```python
class ToolExecutionService:
    def __init__(
        self,
        registry: ToolRegistry,
        policy_engine: PolicyEngine,
        confirm_callback: object = None,
    ) -> None:
        ...

    def execute(self, request: ToolExecutionRequest) -> ToolExecutionOutcome:
        ...
```

Required execution order:

```text
ToolExecutionService.execute()
  -> ToolRegistry.prepare_call()
  -> if prepare error: build error ToolResult and Observation
  -> build ToolIntent from PreparedToolCall
  -> build PolicyContext from Task and modified_files snapshot
  -> PolicyEngine.evaluate()
  -> EventLog.log_policy_decision()
  -> tracer.start_tool(...)
  -> handle deny / require_confirm / allow
  -> ToolRegistry.execute_prepared()
  -> convert ToolResult to Observation
  -> EventLog.log_observation()
  -> update telemetry span
  -> derive modified_path delta
  -> return ToolExecutionOutcome
```

Preparation failure order:

```text
ToolExecutionService.execute()
  -> ToolRegistry.prepare_call()
  -> build error ToolResult and Observation
  -> EventLog.log_observation()
  -> return ToolExecutionOutcome(policy_decision=None)
```

### 4.7 Agent Core Integration

Current flow:

```text
Agent.run()
  -> for each tool call:
       Agent._execute_tool_call()
       Agent updates modified_files
       Agent logs observation
```

Required flow:

```text
Agent.run()
  -> for each tool call:
       ToolExecutionService.execute()
       Agent records outcome.modified_path into modified_files
       Agent appends outcome.observation to history workflow
```

Rules:

- Agent shall no longer instantiate or directly call `PolicyEngine.evaluate()` inside `_execute_tool_call()`.
- Agent shall no longer call `ToolRegistry.execute_tool()` directly for normal tool calls.
- Agent may keep a thin helper if it improves readability, but that helper shall delegate to `ToolExecutionService`.
- Agent shall keep responsibility for model history, reflection checks, loop detection, and run completion.

### 4.8 Policy Interaction

Policy input shall be constructed after preparation:

```python
intent = ToolIntent(
    tool_name=prepared.tool_name,
    params=prepared.params,
)
context = PolicyContext(
    repo_root=Path(request.task.repo_path),
    modified_files=request.modified_files,
)
decision = policy_engine.evaluate(intent, context)
```

Rules:

- Policy shall not evaluate unknown tools when the registry does not contain them.
- Policy shall not evaluate non-object params.
- Policy shall not evaluate schema-invalid params.
- The Policy Engine shall remain deterministic and side-effect free.

## 5. Acceptance Criteria

- **AC-001**: Given a valid allowed tool call, When `ToolExecutionService.execute()` runs, Then it shall prepare the call, evaluate policy, execute the prepared tool, log a policy decision before the observation, and return a successful outcome.
- **AC-002**: Given an unknown tool call, When `ToolExecutionService.execute()` runs, Then it shall not call `PolicyEngine.evaluate()`, shall not execute any tool, and shall return an error observation.
- **AC-003**: Given schema-invalid params, When `ToolExecutionService.execute()` runs, Then it shall not call `PolicyEngine.evaluate()`, shall not execute the tool, and shall return an invalid-params error observation.
- **AC-004**: Given a policy deny decision, When the service handles the decision, Then it shall not call `ToolRegistry.execute_prepared()` and shall return an error observation containing the policy reason.
- **AC-005**: Given a require-confirm decision and no callback, When the service handles the decision, Then it shall fail closed and return an error observation.
- **AC-006**: Given a require-confirm decision and a callback returning false, When the service handles the decision, Then it shall not execute the tool and shall return a rejection error observation.
- **AC-007**: Given a require-confirm decision and a callback returning true, When the service handles the decision, Then it shall execute the prepared tool.
- **AC-008**: Given a confirmation callback raises an exception, When the service handles the decision, Then it shall treat the action as rejected.
- **AC-009**: Given a successful `file_write`, `file_edit`, or `edit` call inside the repository, When the service returns, Then `ToolExecutionOutcome.modified_path` shall contain the resolved path.
- **AC-010**: Given a failed file-modifying call, When the service returns, Then `modified_path` shall be `None`.
- **AC-011**: Given `ToolRegistry.execute_tool()` is called directly by existing tests or callers, When the call is valid, Then behavior shall match the pre-refactor behavior.
- **AC-012**: Given `ToolRegistry.execute_tool()` is called directly with invalid input, When the call returns, Then the error text shall remain compatible with existing tests.
- **AC-013**: Given telemetry is enabled, When a prepared call reaches policy evaluation, Then the tool span shall include policy decision kind and reason.
- **AC-014**: Given the full test suite, When it runs after implementation, Then existing behavior shall not regress.

## 6. Test Automation Strategy

- **Test Levels**: Unit tests for registry preparation and service decision handling; integration tests for Agent core delegation; regression tests for existing tool and policy behavior.
- **Frameworks**: pytest.
- **Test Data Management**: Use `tmp_path`, `NoopTool`, `FailingTool`, `MockBackend`, fake policy engines, and fake confirmation callbacks. Do not require network access.
- **CI/CD Integration**: The implementation shall be validated by running relevant pytest files and preferably the full test suite.
- **Coverage Requirements**: Cover valid preparation, unknown tool, non-object params, schema validation error, policy deny, confirmation unavailable, confirmation rejection, confirmation approval, callback exception, tool exception, and modified-path delta.
- **Performance Testing**: Not required. The refactor shall add no network calls and only constant-time orchestration overhead around existing tool execution.

Required test files or additions:

- Add or update `tests/test_tool_registry.py` or `tests/test_day2.py` for `prepare_call()` and `execute_prepared()`.
- Add `tests/test_tool_execution_service.py` for service lifecycle cases.
- Update `tests/test_policy_engine.py` for Agent integration through the service.
- Keep `tests/test_security_policy.py`, `tests/test_confirm.py`, `tests/test_path_guard.py`, `tests/test_telemetry.py`, and full `pytest` passing.

## 7. Rationale & Context

The current Agent core coordinates tool execution, policy evaluation, confirmation, telemetry, logging, and state tracking in one method. This makes `agent/core.py` a growing control-plane module and makes future policy work likely to add more branching inside the main ReAct loop.

The refactor is motivated by three design goals:

- **Cleaner dependency direction**: The Agent should decide when to execute tools, but it should not own every detail of preparing, authorizing, confirming, logging, and tracing a tool call.
- **Canonical policy input**: The Policy Engine should authorize a validated tool intent, not raw model output. Unknown tools and malformed params are preparation failures, not policy decisions.
- **Stable migration path**: Existing public registry behavior and existing security checks remain intact while creating a focused location for future tool gating, repeated-bypass throttling, and audit improvements.

nanobot provides a useful reference for this boundary. Its registry separates preparation from execution, and its runner owns the lifecycle around tool calls. Forge Agent should adopt the separation of concerns without adopting nanobot's full async runner, plugin discovery, channel system, or distributed safety logic in this phase.

## 8. Dependencies & External Integrations

### External Systems

- **EXT-001**: Local filesystem - Required for path resolution and modified-file delta detection.
- **EXT-002**: Git repository - Required indirectly because existing policy uses modified-file tracking for git staging decisions.

### Third-Party Services

- **SVC-001**: Langfuse - Optional telemetry service. Tool span metadata must remain compatible when Langfuse tracing is enabled.

### Infrastructure Dependencies

- **INF-001**: EventLog JSONL storage - Required for policy and observation audit.
- **INF-002**: Existing ToolRegistry - Required as the source of tool definitions, schema validation, and execution dispatch.
- **INF-003**: Existing Policy Engine - Required as the authorization decision component.

### Data Dependencies

- **DAT-001**: `ToolCall` - Required raw model tool-call input.
- **DAT-002**: Tool JSON Schema - Required for parameter validation.
- **DAT-003**: Agent modified-file set - Required for git staging policy compatibility.
- **DAT-004**: `Task.repo_path` - Required for path-based policy and modified-file delta resolution.

### Technology Platform Dependencies

- **PLT-001**: Python 3.11+ - Required by the project.
- **PLT-002**: pytest - Required for automated validation.

### Compliance Dependencies

- **COM-001**: Secret protection - Existing sensitive-file read and search behavior must not weaken.
- **COM-002**: Workspace boundary safety - Existing path guard and shell guard behavior must not weaken.
- **COM-003**: Auditability - Tool authorization decisions and observations must remain replayable from EventLog.

## 9. Examples & Edge Cases

### 9.1 Valid Allowed Tool Call

```python
prepared = registry.prepare_call("file_read", {"path": "agent/core.py"})
assert prepared.ok

outcome = service.execute(ToolExecutionRequest(
    step=1,
    tool_call=ToolCall("file_read", {"path": "agent/core.py"}),
    task=task,
    modified_files=frozenset(),
    log=log,
    tracer=tracer,
))
assert outcome.observation.status == ObservationStatus.SUCCESS
assert outcome.policy_decision.kind == PolicyDecisionKind.ALLOW
```

Expected behavior:

- Policy receives `{"path": "agent/core.py"}` from the prepared call.
- The tool executes once.
- EventLog contains `policy_decision` followed by `observation`.

### 9.2 Unknown Tool

```python
prepared = registry.prepare_call("readFile", {"path": "README.md"})
assert not prepared.ok
assert "Unknown tool" in prepared.error
```

Expected behavior:

- Policy is not evaluated.
- No tool executes.
- The observation contains the unknown-tool error.

### 9.3 Invalid Params

```python
prepared = registry.prepare_call("file_read", "README.md")
assert not prepared.ok
assert "params" in prepared.error
```

Expected behavior:

- Policy is not evaluated because the call is not executable.
- No tool executes.
- Existing invalid-params wording remains compatible with current tests.

### 9.4 Policy Deny

```python
outcome = service.execute(ToolExecutionRequest(
    step=1,
    tool_call=ToolCall("file_read", {"path": ".env"}),
    task=task,
    modified_files=frozenset(),
    log=log,
    tracer=tracer,
))
assert outcome.policy_decision.kind == PolicyDecisionKind.DENY
assert outcome.result.success is False
```

Expected behavior:

- Policy decision is logged.
- Tool execution is skipped.
- Observation contains the policy denial reason.

### 9.5 Confirmation Required

```python
outcome = service.execute(ToolExecutionRequest(
    step=1,
    tool_call=ToolCall("git_commit", {"message": "checkpoint"}),
    task=task,
    modified_files=frozenset(),
    log=log,
    tracer=tracer,
))
```

Expected behavior:

- Without callback, outcome is an error and tool execution is skipped.
- With callback returning false, outcome is an error and tool execution is skipped.
- With callback returning true, the prepared tool executes.

### 9.6 Modified-File Delta

```python
outcome = service.execute(ToolExecutionRequest(
    step=1,
    tool_call=ToolCall("file_write", {"path": "src/app.py", "content": "..."}),
    task=task,
    modified_files=frozenset(),
    log=log,
    tracer=tracer,
))
if outcome.modified_path is not None:
    modified_files.add(outcome.modified_path)
```

Expected behavior:

- The service detects successful file modification.
- The Agent owns the accumulated modified-file set.

## 10. Validation Criteria

- **VAL-001**: `ToolRegistry.prepare_call()` exists and has unit tests for valid, unknown, non-object, and schema-invalid calls.
- **VAL-002**: `ToolRegistry.execute_tool()` remains backward compatible and reuses preparation internally.
- **VAL-003**: `ToolExecutionService` exists and is covered by focused tests.
- **VAL-004**: Agent core delegates tool-call execution to `ToolExecutionService`.
- **VAL-005**: Policy is evaluated only for prepared calls.
- **VAL-006**: Denied and confirmation-rejected calls do not execute tools.
- **VAL-007**: Policy decision events precede observations for prepared calls.
- **VAL-008**: Prepare failures produce error observations and do not produce policy decisions.
- **VAL-009**: Modified-file deltas preserve existing git staging policy behavior.
- **VAL-010**: Existing security policy, confirmation, path guard, telemetry, chat, streaming, and SWE-bench tests pass.
- **VAL-011**: Full `pytest` passes.

## 11. Related Specifications / Further Reading

- [Unified Policy Engine Stable Refactor](./spec-architecture-policy-engine-stable-refactor.md)
- [Tool-call History Trimming](../docs/problem-analyses/tool-call-history-trimming.md)
- [LLM Tool Parameter Error Handling](../docs/problem-analyses/llm-tool-parameter-error-handling.md)
- [Agent Harness Failure Taxonomy](../docs/problem-analyses/agent-harness-failure-taxonomy.md)
- nanobot architecture reference: `/Users/pclong/projects_dev/agent_learn/nanobot/docs/architecture.md`
- nanobot tool registry reference: `/Users/pclong/projects_dev/agent_learn/nanobot/nanobot/agent/tools/registry.py`
- nanobot runner reference: `/Users/pclong/projects_dev/agent_learn/nanobot/nanobot/agent/runner.py`
