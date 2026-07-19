---
title: Unified Policy Engine Stable Refactor
version: 1.0
date_created: 2026-07-19
last_updated: 2026-07-19
owner: Forge Agent
tags: [architecture, policy, security, observability, agent-harness]
---

# Introduction

This specification defines the stable refactor for introducing a unified Policy Engine into Forge Agent. The goal is to centralize policy decision modeling and audit logging while preserving the current runtime behavior of tool authorization as much as possible.

This is the first implementation phase for policy governance. It intentionally avoids broad behavior changes such as dynamic tool exposure, full security-mode configuration, and complete ShellTool policy migration.

## 1. Purpose & Scope

The purpose of this specification is to define requirements, constraints, interfaces, and validation criteria for a stable Policy Engine refactor.

The Policy Engine shall provide a single control-plane interface for evaluating tool-call authorization decisions before tools are executed. It shall preserve the existing authorization behavior through built-in deterministic rules, integrate with the tool execution pipeline, and emit structured policy decision events.

The intended audience includes:

- Agent core implementers.
- Tooling and runtime implementers.
- Evaluation and observability implementers.
- Future agents or automated coders consuming this specification.

Assumptions:

- Forge Agent is a Python 3.11 project.
- Existing behavior in `tools/security_policy.py`, `tools/shell_tool.py`, and `tools/path_guard.py` must remain functionally compatible in the first phase.
- The first phase is a stable refactor, not a product feature expansion.
- The existing test suite must continue passing after implementation.

Out of scope for this first phase:

- Dynamic tool exposure through `visible_tools()`.
- Full `policy:` configuration in `config/default.yaml`.
- New security modes such as `readonly`, `interactive`, `autonomous`, `benchmark`, or `ci`.
- Full migration of ShellTool confirmation logic into the Policy Engine.
- Output redaction beyond current observation formatting.
- New CLI commands such as `forgeagent eval policy-stats`.

## 2. Definitions

- **Agent Core**: The central ReAct loop implemented by `agent/core.py`.
- **Tool Call**: A model-requested invocation of a named tool with structured parameters.
- **Tool Intent**: A normalized representation of a tool call before authorization and execution.
- **Policy Engine**: A deterministic component that evaluates tool intents against context and returns a structured decision.
- **Policy Decision**: The result of evaluating a tool intent, including decision kind, reason, and confirmation prompt.
- **Policy Context**: Runtime metadata required to make a policy decision. The first phase includes repository root and files modified during the current run.
- **Fail Closed**: A policy behavior where unknown or non-confirmable risky actions are rejected instead of allowed.
- **Observation**: The result returned from a tool execution and fed back into the agent history.
- **EventLog**: The append-only JSONL log for agent actions, observations, reflections, and terminal outcomes.

## 3. Requirements, Constraints & Guidelines

- **REQ-001**: The implementation shall introduce a dedicated `policy/` package for Policy Engine types and orchestration.
- **REQ-002**: The Policy Engine shall expose a single pre-tool-call evaluation method that accepts a `ToolIntent` and `PolicyContext`.
- **REQ-003**: The first-phase Policy Engine shall preserve existing authorization behavior from the previous security policy rules unless this specification explicitly requires a change.
- **REQ-004**: The tool execution pipeline shall use the Policy Engine instead of directly applying policy checks inside Agent Core.
- **REQ-005**: The Policy Engine shall return structured decisions rather than plain strings.
- **REQ-006**: The EventLog shall include a structured policy decision event before each tool execution attempt.
- **REQ-007**: A denied policy decision shall prevent `ToolRegistry.execute_tool()` from being called.
- **REQ-008**: A confirmation-required decision without an available confirmation callback shall fail closed and return an error observation.
- **REQ-009**: A confirmation-required decision with an available callback shall execute the tool only when the callback returns true.
- **REQ-010**: Policy decision data shall be included in telemetry tool-span metadata where telemetry is active.
- **REQ-011**: The implementation shall maintain compatibility with existing tests for security policy, shell tools, path guard behavior, chat, run, and SWE-bench.

- **SEC-001**: Sensitive file read rules shall continue to deny reads of `.env`, private key files, `.git-credentials`, `.git/config`, and JSONL logs under `logs/`.
- **SEC-002**: High-risk write rules shall continue to require confirmation for sensitive files, workflow files, git hooks, dependency configuration, and deployment or release scripts.
- **SEC-003**: `git_add` without explicit paths, with `"."`, or with `["."]` shall continue to be denied.
- **SEC-004**: `git_commit` shall continue to require confirmation.
- **SEC-005**: The first-phase Policy Engine shall not reduce existing ShellTool hard blocking, sensitive path checks, workspace boundary enforcement, timeout behavior, or output truncation.
- **SEC-006**: Policy logs shall not require full tool parameters to be persisted. If parameters are logged, they shall be sanitized or explicitly enabled by a local debug setting in a later phase.

- **OBS-001**: Every pre-tool-call policy evaluation shall have a stable decision payload suitable for JSONL logging.
- **OBS-002**: Policy decision events shall include at minimum `kind`, `reason`, `prompt`, `tool_name`, and `step`.
- **OBS-003**: Policy decision events shall be distinguishable from action and observation events.
- **OBS-004**: Policy decision telemetry shall expose decision kind and reason. Aggregation by rule ID or runtime mode is reserved until explicit rules and modes exist.

- **CON-001**: The first implementation shall avoid broad changes to LLM prompts, tool schemas, CLI behavior, and runtime behavior.
- **CON-002**: The first implementation shall not remove defensive checks from `ShellTool` or `WorkspaceBoundary`.
- **CON-003**: The implementation shall avoid a complex policy domain-specific language in the first phase.
- **CON-004**: New policy types shall be serializable without custom non-standard encoders.
- **CON-005**: Policy logic shall not depend on network access.

- **GUD-001**: Prefer small Python rule classes or adapter functions over YAML-driven rule expressions in the first phase.
- **GUD-002**: Do not infer policy categories from free-form reason strings in the first phase. Add stable rule identifiers only when rules are represented explicitly.
- **GUD-003**: Treat allow decisions as pre-authorization only. Tool and runtime layers must retain local validation and safety checks.
- **GUD-004**: Use explicit decision kinds instead of overloading errors returned by tools.
- **GUD-005**: Design the data contracts so later phases can add dynamic tool exposure, output redaction, security modes, and policy statistics without breaking first-phase logs.

## 4. Interfaces & Data Contracts

### 4.1 Package Layout

The first phase should introduce this package layout:

```text
policy/
  __init__.py
  types.py
  engine.py
```

Optional future files, not required in the first phase:

```text
policy/
  config.py
  redaction.py
  rules/
    file_rules.py
    shell_rules.py
    git_rules.py
    runtime_rules.py
```

### 4.2 Decision Kinds

```python
from enum import Enum

class PolicyDecisionKind(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRM = "require_confirm"
```

### 4.3 ToolIntent

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class ToolIntent:
    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
```

Rules:

- `tool_name` shall be the name requested by the model.
- `params` shall be a dictionary. If the model provides non-dictionary params, Agent Core may pass an empty dictionary to policy evaluation while ToolRegistry still performs schema validation on the original value.

### 4.4 PolicyContext

```python
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class PolicyContext:
    repo_root: Path
    modified_files: frozenset[Path] = frozenset()
```

Required first-phase values:

| Field | Required source |
| --- | --- |
| `repo_root` | `Task.repo_path` |
| `modified_files` | Agent run state |

### 4.5 PolicyDecision

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class PolicyDecision:
    kind: PolicyDecisionKind
    reason: str = ""
    prompt: str = ""
```

Serialization contract:

```json
{
  "kind": "require_confirm",
  "reason": "git_commit requires user confirmation",
  "prompt": "git_commit requires user confirmation"
}
```

### 4.6 PolicyEngine

```python
class PolicyEngine:
    def evaluate(
        self,
        intent: ToolIntent,
        context: PolicyContext,
    ) -> PolicyDecision:
        ...
```

First-phase implementation requirements:

- It shall implement equivalent built-in rules for the existing read, write, search, git, and shell authorization behavior.
- It shall return `ALLOW` for tool calls not covered by built-in rules, preserving existing behavior.

### 4.7 AgentConfig Addition

```python
@dataclass
class AgentConfig:
    policy_engine: PolicyEngine | None = None
```

Rules:

- If `policy_engine` is `None`, Agent Core shall create a default `PolicyEngine`.
- Existing AgentConfig fields shall retain existing behavior.
- Adding this field shall not require immediate config YAML changes.

### 4.8 EventLog Event

Add event type:

```python
class EventType(str, Enum):
    POLICY_DECISION = "policy_decision"
```

Add log method:

```python
def log_policy_decision(
    self,
    step: int,
    tool_name: str,
    decision: PolicyDecision,
    call_id: str | None = None,
) -> None:
    ...
```

Payload shape:

```json
{
  "step": 3,
  "tool_name": "file_write",
  "tool_call_id": "call_123",
  "decision": {
    "kind": "require_confirm",
    "reason": "High-risk file write requires confirmation: /repo/pyproject.toml",
    "prompt": "High-risk file write requires confirmation: /repo/pyproject.toml"
  }
}
```

### 4.9 Agent Core Integration

Current conceptual flow:

```text
Agent tool execution path
  -> built-in security policy checks
  -> maybe confirm
  -> ToolRegistry.execute_tool()
  -> Observation
```

Required first-phase flow:

```text
Agent tool execution path
  -> build ToolIntent
  -> build PolicyContext
  -> PolicyEngine.evaluate()
  -> EventLog.log_policy_decision()
  -> maybe confirm
  -> ToolRegistry.execute_tool()
  -> Observation
```

Decision handling:

| Decision kind | Required behavior |
| --- | --- |
| `allow` | Execute the tool through ToolRegistry. |
| `deny` | Do not execute the tool. Return an error ToolResult with the decision reason. |
| `require_confirm` with callback and approval | Execute the tool through ToolRegistry. |
| `require_confirm` with callback and rejection | Do not execute. Return an error ToolResult indicating user rejection. |
| `require_confirm` without callback | Do not execute. Return an error ToolResult indicating confirmation was required but unavailable. |

## 5. Acceptance Criteria

- **AC-001**: Given a tool call that the previous security rules would allow, When the Policy Engine evaluates it, Then the decision shall be `allow` and the tool shall execute.
- **AC-002**: Given a sensitive file read, When the Policy Engine evaluates it, Then the decision shall be `deny` and `ToolRegistry.execute_tool()` shall not be called.
- **AC-003**: Given a high-risk file write and no confirmation callback, When the Policy Engine evaluates it, Then the decision shall be `require_confirm`, the tool shall not execute, and the observation shall indicate unavailable confirmation.
- **AC-004**: Given a high-risk file write and a confirmation callback returning true, When the Policy Engine evaluates it, Then the tool shall execute.
- **AC-005**: Given `git_add` with no explicit paths, `"."`, or `["."]`, When the Policy Engine evaluates it, Then the decision shall be `deny`.
- **AC-006**: Given `git_commit`, When the Policy Engine evaluates it, Then the decision shall be `require_confirm`.
- **AC-007**: Given any tool call, When Agent Core evaluates policy, Then EventLog shall contain one `policy_decision` event before the corresponding observation event.
- **AC-008**: Given telemetry is enabled, When a tool call is evaluated, Then the tool span metadata shall include the policy decision kind and reason.
- **AC-009**: Given existing security policy tests, When the test suite runs, Then no existing behavior shall regress.
- **AC-010**: Given the first-phase implementation, When CLI `run`, `chat`, GitHub Issue, or SWE-bench entrypoints run, Then no user-visible command-line behavior shall change except additional policy events in logs and telemetry.

## 6. Test Automation Strategy

- **Test Levels**: Unit and integration tests are required for the first phase. End-to-end CLI tests are recommended but not required if existing CLI coverage remains green.
- **Frameworks**: Use pytest, following the existing test suite.
- **Test Data Management**: Use `tmp_path`, `NoopTool`, `MockBackend`, and local fake repositories. Do not require network access.
- **CI/CD Integration**: The implementation shall be validated by running the relevant pytest files and preferably the full test suite.
- **Coverage Requirements**: New policy types, decision mapping, EventLog policy events, and Agent Core integration shall have focused tests.
- **Performance Testing**: Not required for the first phase. Policy evaluation shall be deterministic and lightweight.

Required test cases:

- Policy Engine maps legacy allow, deny, and require-confirm decisions.
- Denied policy decisions prevent tool execution.
- Require-confirm without callback fails closed.
- Require-confirm with rejection prevents tool execution.
- Require-confirm with approval executes the tool.
- Policy decision events serialize to JSONL and replay correctly.
- Telemetry metadata includes policy decision kind and reason without requiring full params.
- Existing `tests/test_security_policy.py` semantics continue to pass.

Recommended regression tests:

- Sensitive paths: `.env`, `.env.local`, `.git/config`, `logs/run.jsonl`, `id_rsa`, `secret.pem`.
- High-risk write paths: `.github/workflows/ci.yml`, `.git/hooks/pre-commit`, `pyproject.toml`, `requirements.txt`, `package.json`, `deploy.sh`.
- Git rules: `git_add` missing paths, explicit modified path, unmodified path, sensitive path, `git_commit`.

## 7. Rationale & Context

Forge Agent is positioned as a multi-model coding-agent harness with observability, evaluation, and governance capabilities. A unified Policy Engine supports this positioning by making tool authorization decisions explicit, structured, and comparable across models and entrypoints.

The current implementation already contains important safety components, but decision logic is distributed across multiple layers. This distribution makes it harder to answer:

- Which policy decision caused a tool call to be denied?
- Did a benchmark fail because of model behavior, tool failure, environment failure, or policy denial?
- Did chat, run, GitHub Issue, and SWE-bench use equivalent policy semantics?
- Can the run be replayed with the same policy decisions?

The first-phase refactor shall prioritize stability. It shall create the control-plane data model and logging surface without removing existing tool-level defenses. This reduces implementation risk and creates a foundation for later phases.

Later phases may add:

- Dynamic tool exposure.
- Configurable policy modes.
- Runtime capability enforcement.
- Output redaction.
- Policy statistics and evaluation reports.
- Replayable policy snapshots.

## 8. Dependencies & External Integrations

### External Systems

- **EXT-001**: Local filesystem - Required for path-based policy decisions and workspace boundary checks.
- **EXT-002**: Git repository - Required for git staging and commit policy decisions where git tools are used.

### Third-Party Services

- **SVC-001**: Langfuse - Optional observability service. Policy decision metadata shall be included when Langfuse tracing is enabled.

### Infrastructure Dependencies

- **INF-001**: EventLog JSONL storage - Required as the local source of truth for policy decisions.
- **INF-002**: Runtime abstraction - Reserved for future phases if sandbox or network state becomes an active policy input.

### Data Dependencies

- **DAT-001**: Existing tool-call parameters - Required as input to `ToolIntent`.
- **DAT-002**: Modified file set - Required for git staging policy.
- **DAT-003**: Existing security rule behavior - Required for first-phase compatibility.

### Technology Platform Dependencies

- **PLT-001**: Python 3.11 or newer - Required by the project runtime.
- **PLT-002**: pytest - Required for automated validation.

### Compliance Dependencies

- **COM-001**: Secret protection - The Policy Engine shall preserve existing rules that prevent reading known secret-bearing paths.
- **COM-002**: Auditability - Policy decisions shall be written to local JSONL logs for later review.

## 9. Examples & Edge Cases

### 9.1 Allowed File Read

```python
intent = ToolIntent(tool_name="file_read", params={"path": "agent/core.py"})
decision = policy_engine.evaluate(intent, context)
assert decision.kind == PolicyDecisionKind.ALLOW
```

Expected behavior:

- The file read proceeds through `ToolRegistry.execute_tool()`.
- A `policy_decision` event is written with `kind=allow`.

### 9.2 Denied Sensitive File Read

```python
intent = ToolIntent(tool_name="file_read", params={"path": ".env"})
decision = policy_engine.evaluate(intent, context)
assert decision.kind == PolicyDecisionKind.DENY
```

Expected behavior:

- The file read does not execute.
- The observation contains an error derived from the policy reason.
- The policy decision includes a clear reason string from the underlying policy.

### 9.3 Confirmation Required Without Callback

```python
intent = ToolIntent(tool_name="git_commit", params={"message": "fix bug"})
decision = policy_engine.evaluate(intent, context)
assert decision.kind == PolicyDecisionKind.REQUIRE_CONFIRM
```

Expected behavior:

- The git commit does not execute.
- The observation states that confirmation is required but unavailable.

### 9.4 Confirmation Required With Approval

```python
intent = ToolIntent(tool_name="file_write", params={"path": "pyproject.toml", "content": "..."})
decision = policy_engine.evaluate(intent, context)
assert decision.kind == PolicyDecisionKind.REQUIRE_CONFIRM
```

Expected behavior:

- Agent Core invokes the existing confirmation callback.
- If the callback returns true, the tool executes.
- If the callback returns false, the tool does not execute.

### 9.5 Unknown Tool

```python
intent = ToolIntent(tool_name="unknown_tool", params={})
decision = policy_engine.evaluate(intent, context)
assert decision.kind == PolicyDecisionKind.ALLOW
```

Expected behavior:

- The Policy Engine does not reject unknown tools in the first phase.
- `ToolRegistry` returns the existing unknown-tool error.
- This preserves current behavior.

## 10. Validation Criteria

- **VAL-001**: The project contains a well-typed `policy/` package with core policy data contracts and a default Policy Engine.
- **VAL-002**: Agent Core uses Policy Engine decisions before every tool execution.
- **VAL-003**: Existing security behavior remains compatible with the pre-refactor behavior.
- **VAL-004**: Policy decisions are written to EventLog and can be replayed.
- **VAL-005**: Denied decisions do not execute tools.
- **VAL-006**: Confirmation-required decisions preserve existing callback behavior.
- **VAL-007**: Policy decision telemetry is available when tracing is enabled.
- **VAL-008**: New tests cover policy data contracts, policy decision mapping, EventLog integration, and Agent Core integration.
- **VAL-009**: The relevant test suite passes after implementation.

## 11. Related Specifications / Further Reading

- [Prompt Injection Defense Strategy](../docs/problem-analyses/prompt-injection-defense-strategy.md)
- [Tool-call History Trimming](../docs/problem-analyses/tool-call-history-trimming.md)
- [LLM Tool Parameter Error Handling](../docs/problem-analyses/llm-tool-parameter-error-handling.md)
- [Agent Harness Failure Taxonomy](../docs/problem-analyses/agent-harness-failure-taxonomy.md)
- Claude Code permissions documentation: https://code.claude.com/docs/en/permissions
- Claude Code hooks documentation: https://code.claude.com/docs/en/hooks
- OpenAI Codex CLI documentation: https://help.openai.com/en/articles/11096431
- OpenHands runtime documentation: https://docs.openhands.dev/openhands/usage/architecture/runtime
- mini-swe-agent documentation: https://mini-swe-agent.com/latest/
