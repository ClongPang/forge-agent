"""Tool-call execution orchestration for Agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent.event_log import EventLog
from agent.task import Observation, ToolCall, ToolErrorKind
from agent.telemetry import AgentTracer
from policy import (
    PolicyContext,
    PolicyDecision,
    PolicyDecisionKind,
    PolicyEngine,
    ToolIntent,
)
from tools.base import PreparedToolCall, ToolRegistry, ToolResult
from tools.security_policy import is_inside, resolve_repo_path


@dataclass(frozen=True)
class ToolExecutionRequest:
    """Input needed to execute one tool call within an agent step."""

    step: int
    tool_call: ToolCall
    repo_root: Path
    modified_files: frozenset[Path]
    log: EventLog
    tracer: AgentTracer


@dataclass(frozen=True)
class ToolExecutionOutcome:
    """Result of one tool-call execution."""

    observation: Observation
    modified_path: Path | None = None


class ToolExecutionService:
    """Coordinate preparation, policy, confirmation, execution, and logging."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy_engine: PolicyEngine,
        confirm_callback: Callable[[str], bool] | None = None,
    ) -> None:
        self._registry = registry
        self._policy_engine = policy_engine
        self._confirm_callback = confirm_callback

    def execute(self, request: ToolExecutionRequest) -> ToolExecutionOutcome:
        prepared = self._registry.prepare_call(
            request.tool_call.name,
            request.tool_call.params,
        )
        if not prepared.ok:
            result = ToolResult(
                success=False,
                output="",
                error=prepared.error,
                error_kind=prepared.error_kind,
            )
            observation = result.to_observation(request.tool_call.name)
            self._record_tool_span(
                request=request,
                result=result,
                observation=observation,
                policy_decision=None,
            )
            request.log.log_observation(step=request.step, observation=observation)
            return ToolExecutionOutcome(
                observation=observation,
            )

        decision = self._evaluate_policy(request, prepared)
        request.log.log_policy_decision(
            step=request.step,
            tool_name=prepared.tool_name,
            decision=decision.to_dict(),
            call_id=request.tool_call.id,
        )

        result = self._execute_after_policy(prepared, decision)
        observation = result.to_observation(prepared.tool_name)
        self._record_tool_span(
            request=request,
            result=result,
            observation=observation,
            policy_decision=decision,
        )
        request.log.log_observation(step=request.step, observation=observation)

        return ToolExecutionOutcome(
            observation=observation,
            modified_path=self._modified_path(request, prepared, result),
        )

    def _evaluate_policy(
        self,
        request: ToolExecutionRequest,
        prepared: PreparedToolCall,
    ) -> PolicyDecision:
        context = PolicyContext(
            repo_root=request.repo_root,
            modified_files=request.modified_files,
        )
        intent = ToolIntent(
            tool_name=prepared.tool_name,
            params=prepared.params,
        )
        return self._policy_engine.evaluate(intent, context)

    def _execute_after_policy(
        self,
        prepared: PreparedToolCall,
        decision: PolicyDecision,
    ) -> ToolResult:
        if decision.kind == PolicyDecisionKind.DENY:
            return ToolResult(
                success=False,
                output="",
                error=decision.reason,
                error_kind=ToolErrorKind.POLICY_DENIED,
            )

        if decision.kind == PolicyDecisionKind.REQUIRE_CONFIRM:
            if self._confirm_callback is None:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Action requires confirmation: {decision.reason}",
                    error_kind=ToolErrorKind.CONFIRMATION_REQUIRED,
                )

            try:
                allowed = bool(self._confirm_callback(decision.prompt or decision.reason))
            except Exception:
                allowed = False
            if not allowed:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Action rejected by user: {decision.reason}",
                    error_kind=ToolErrorKind.CONFIRMATION_REJECTED,
                )

        return self._registry.execute_prepared(prepared)

    def _record_tool_span(
        self,
        *,
        request: ToolExecutionRequest,
        result: ToolResult,
        observation: Observation,
        policy_decision: PolicyDecision | None,
    ) -> None:
        with request.tracer.start_tool(
            step=request.step,
            tool_call=request.tool_call,
            policy_decision=policy_decision,
        ) as tool_span:
            tool_span.update(
                output={
                    "result": {
                        "success": result.success,
                        "output": result.output,
                        "error": result.error,
                    },
                    "observation": observation.to_dict(),
                },
                metadata={
                    "status": observation.status.value,
                    "error": observation.error,
                    "error_kind": (
                        observation.error_kind.value
                        if observation.error_kind is not None
                        else None
                    ),
                },
            )

    def _modified_path(
        self,
        request: ToolExecutionRequest,
        prepared: PreparedToolCall,
        result: ToolResult,
    ) -> Path | None:
        if not result.success or prepared.tool_name not in {"file_write", "file_edit", "edit"}:
            return None
        raw_path = prepared.params.get("path")
        if not raw_path:
            return None
        resolved = resolve_repo_path(raw_path, request.repo_root)
        if is_inside(resolved, request.repo_root):
            return resolved
        return None
