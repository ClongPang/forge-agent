"""
agent/telemetry.py

Optional observability hooks for agent runs.

The local EventLog remains the deterministic source of truth. Telemetry is a
best-effort remote view for trace UI, latency, token use, and debugging.
"""

from __future__ import annotations

import logging
import re
from contextlib import ExitStack, nullcontext
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from agent.task import RunResult, Task, ToolCall
from llm.base import LLMMessage, LLMToolSchema

logger = logging.getLogger(__name__)


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(authorization\s*:\s*bearer\s+)([A-Za-z0-9._~+/=-]+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|"
            r"refresh[_-]?token|password|passwd|pwd|token)"
            r"(\s*[:=]\s*)([^\s,;'\"]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"\b(sk|pk)-[A-Za-z0-9][A-Za-z0-9._-]{10,}\b"),
        r"\1-[REDACTED]",
    ),
    (
        re.compile(r"\b[A-Za-z0-9_]*SECRET[A-Za-z0-9_]*(\s*=\s*)([^\s]+)"),
        r"SECRET\1[REDACTED]",
    ),
)


def _redact_string(value: str) -> str:
    redacted = value
    for pattern, repl in _SECRET_PATTERNS:
        redacted = pattern.sub(repl, redacted)
    return redacted


def _safe_value(value: Any, trace_content: str = "full") -> Any:
    """
    Convert application objects into Langfuse-safe data.

    trace_content="full" still applies hard redaction for obvious credentials.
    trace_content="off" preserves structure while omitting large content fields.
    """
    if trace_content == "off":
        if isinstance(value, str):
            return "[omitted]"
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): _safe_value(v, trace_content) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_safe_value(v, trace_content) for v in value]
        return "[omitted]"

    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if is_dataclass(value):
        return _safe_value(asdict(value), trace_content)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _safe_value(value.to_dict(), trace_content)
        except Exception:
            return _redact_string(repr(value))
    if isinstance(value, dict):
        return {str(k): _safe_value(v, trace_content) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(v, trace_content) for v in value]
    return _redact_string(repr(value))


def _metadata_value(value: Any) -> str:
    text = str(value)
    if len(text) > 200:
        return text[:197] + "..."
    return text


def _metadata(data: dict[str, Any] | None) -> dict[str, str]:
    if not data:
        return {}
    return {str(k): _metadata_value(v) for k, v in data.items() if v is not None}


def _config_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


class TelemetryObservation:
    """Small wrapper around a Langfuse observation context."""

    def __init__(
        self,
        context: Any = None,
        *,
        trace_content: str = "full",
    ) -> None:
        self._context = context
        self._observation: Any = None
        self._trace_content = trace_content

    def __enter__(self) -> "TelemetryObservation":
        if self._context is not None:
            self._observation = self._context.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.mark_error(exc)
        if self._context is not None:
            return bool(self._context.__exit__(exc_type, exc, tb))
        return False

    def update(
        self,
        *,
        input: Any = None,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        usage_details: dict[str, Any] | None = None,
    ) -> None:
        if self._observation is None:
            return
        payload: dict[str, Any] = {}
        if input is not None:
            payload["input"] = _safe_value(input, self._trace_content)
        if output is not None:
            payload["output"] = _safe_value(output, self._trace_content)
        if metadata:
            payload["metadata"] = _safe_value(metadata, self._trace_content)
        if model is not None:
            payload["model"] = model
        if usage_details is not None:
            payload["usage_details"] = usage_details
        if not payload:
            return
        try:
            self._observation.update(**payload)
        except Exception as exc:
            logger.warning("Telemetry update failed: %s", exc)

    def mark_error(self, exc: BaseException | str) -> None:
        self.update(
            metadata={
                "telemetry_status": "error",
                "error_type": type(exc).__name__ if isinstance(exc, BaseException) else "error",
                "error": str(exc),
            }
        )


class _NoopContext(TelemetryObservation):
    def __init__(self) -> None:
        super().__init__(None)


class _RunTelemetryObservation(TelemetryObservation):
    """Root observation that opens Langfuse contexts when the caller enters it."""

    def __init__(
        self,
        *,
        client: Any,
        propagate_attributes: Any,
        trace_kwargs: dict[str, Any],
        observation_kwargs: dict[str, Any],
        trace_content: str,
    ) -> None:
        super().__init__(None, trace_content=trace_content)
        self._client = client
        self._propagate_attributes = propagate_attributes
        self._trace_kwargs = trace_kwargs
        self._observation_kwargs = observation_kwargs
        self._stack: ExitStack | None = None

    def __enter__(self) -> "TelemetryObservation":
        stack = ExitStack()
        try:
            stack.enter_context(self._propagate_attributes(**self._trace_kwargs))
            context = self._client.start_as_current_observation(**self._observation_kwargs)
            self._observation = stack.enter_context(context)
            self._stack = stack
        except Exception as exc:
            stack.close()
            self._observation = None
            self._stack = None
            logger.warning("Failed to start Langfuse run observation: %s", exc)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.mark_error(exc)
        if self._stack is None:
            return False
        return bool(self._stack.__exit__(exc_type, exc, tb))


class AgentTracer:
    """No-op tracer interface used by Agent core."""

    trace_content = "full"
    flush_on_exit = False

    def start_run(
        self,
        *,
        task: Task,
        log_path: str,
        model: str,
        stream: bool,
    ) -> TelemetryObservation:
        return _NoopContext()

    def start_generation(
        self,
        *,
        step: int,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
        model: str,
        stream: bool,
    ) -> TelemetryObservation:
        return _NoopContext()

    def start_tool(
        self,
        *,
        step: int,
        tool_call: ToolCall,
        policy_decision: Any = None,
    ) -> TelemetryObservation:
        return _NoopContext()

    def record_event(
        self,
        name: str,
        *,
        input: Any = None,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None

    def finish_run(self, result: RunResult) -> None:
        return None

    def flush(self) -> None:
        return None


class LangfuseTracer(AgentTracer):
    """Langfuse SDK v4 implementation of AgentTracer."""

    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        base_url: str = "",
        trace_content: str = "full",
        debug: bool = False,
        flush_on_exit: bool = True,
        mode: str = "run",
        provider: str = "",
        model: str = "",
        client: Any = None,
        propagate_attributes: Any = None,
    ) -> None:
        if client is None:
            try:
                from langfuse import Langfuse, propagate_attributes as _propagate
            except ImportError as exc:
                raise RuntimeError(
                    "Langfuse tracing requires langfuse>=4,<5. "
                    "Install project dependencies with Python >=3.11."
                ) from exc

            kwargs: dict[str, Any] = {
                "public_key": public_key,
                "secret_key": secret_key,
                "debug": debug,
            }
            if base_url:
                kwargs["base_url"] = base_url
            client = Langfuse(**kwargs)
            propagate_attributes = _propagate

        self._client = client
        self._propagate_attributes = propagate_attributes or (lambda **_: nullcontext())
        self.trace_content = trace_content
        self.flush_on_exit = flush_on_exit
        self._mode = mode
        self._provider = provider
        self._model = model
        self._root: TelemetryObservation | None = None

    def start_run(
        self,
        *,
        task: Task,
        log_path: str,
        model: str,
        stream: bool,
    ) -> TelemetryObservation:
        tags = ["forge-agent", f"mode:{self._mode}"]
        if self._provider:
            tags.append(f"provider:{self._provider}")
        metadata = _metadata({
            "task_id": task.task_id,
            "event_log_path": log_path,
            "provider": self._provider,
            "model": model or self._model,
            "stream": stream,
            "entrypoint": self._mode,
            "max_steps": task.max_steps,
            "budget_tokens": task.budget_tokens,
        })
        input_payload = {
            "task": task.to_dict(),
            "repo_path": task.repo_path,
            "issue_url": task.issue_url,
            "max_steps": task.max_steps,
            "budget_tokens": task.budget_tokens,
        }
        name = "forgeagent.chat-round" if self._mode == "chat" else "forgeagent.run"
        try:
            obs = _RunTelemetryObservation(
                client=self._client,
                propagate_attributes=self._propagate_attributes,
                trace_kwargs={
                    "trace_name": name,
                    "tags": tags,
                    "metadata": metadata,
                },
                observation_kwargs={
                    "as_type": "agent",
                    "name": name,
                    "input": _safe_value(input_payload, self.trace_content),
                    "metadata": metadata,
                },
                trace_content=self.trace_content,
            )
            self._root = obs
            return obs
        except Exception as exc:
            logger.warning("Failed to prepare Langfuse run observation: %s", exc)
            return _NoopContext()

    def start_generation(
        self,
        *,
        step: int,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
        model: str,
        stream: bool,
    ) -> TelemetryObservation:
        input_payload = {
            "step": step,
            "messages": [m.__dict__ for m in messages],
            "tools": [t.__dict__ for t in tools],
            "tool_count": len(tools),
            "stream": stream,
        }
        metadata = {
            "step": step,
            "tool_count": len(tools),
            "stream": stream,
        }
        try:
            context = self._client.start_as_current_observation(
                as_type="generation",
                name="llm.decide_action",
                model=model,
                input=_safe_value(input_payload, self.trace_content),
                metadata=_safe_value(metadata, self.trace_content),
            )
            return TelemetryObservation(context, trace_content=self.trace_content)
        except Exception as exc:
            logger.warning("Failed to start Langfuse generation observation: %s", exc)
            return _NoopContext()

    def start_tool(
        self,
        *,
        step: int,
        tool_call: ToolCall,
        policy_decision: Any = None,
    ) -> TelemetryObservation:
        decision_kind = getattr(policy_decision, "kind", None)
        decision_reason = getattr(policy_decision, "reason", None)
        metadata = {
            "step": step,
            "tool": tool_call.name,
            "policy_decision": decision_kind,
            "policy_reason": decision_reason,
        }
        input_payload = {
            "step": step,
            "tool_call": tool_call.to_dict(),
            "policy": metadata,
        }
        try:
            context = self._client.start_as_current_observation(
                as_type="tool",
                name=f"tool.{tool_call.name}",
                input=_safe_value(input_payload, self.trace_content),
                metadata=_safe_value(metadata, self.trace_content),
            )
            return TelemetryObservation(context, trace_content=self.trace_content)
        except Exception as exc:
            logger.warning("Failed to start Langfuse tool observation: %s", exc)
            return _NoopContext()

    def record_event(
        self,
        name: str,
        *,
        input: Any = None,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            event_metadata = {
                "telemetry_kind": "event",
                **(metadata or {}),
            }
            with self._client.start_as_current_observation(
                as_type="span",
                name=name,
                input=_safe_value(input, self.trace_content) if input is not None else None,
                metadata=_safe_value(event_metadata, self.trace_content),
            ) as event:
                if output is not None:
                    event.update(output=_safe_value(output, self.trace_content))
        except Exception as exc:
            logger.warning("Failed to record Langfuse event %s: %s", name, exc)

    def finish_run(self, result: RunResult) -> None:
        if self._root is None:
            return
        output = result.to_dict()
        metadata = {
            "final_status": result.status.value,
            "steps_taken": result.steps_taken,
            "total_tokens": result.total_tokens,
            "has_patch": result.patch is not None,
            "error": result.error,
        }
        self._root.update(output=output, metadata=metadata)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception as exc:
            logger.warning("Langfuse flush failed: %s", exc)


def build_tracer_from_config(
    config: Any,
    *,
    mode: str,
    provider: str,
    model: str,
) -> AgentTracer:
    """Create the configured tracer for CLI/chat entrypoints."""
    langfuse_cfg = getattr(getattr(config, "observability", None), "langfuse", None)
    if langfuse_cfg is None or not _config_bool(getattr(langfuse_cfg, "enabled", False)):
        return AgentTracer()

    public_key = getattr(langfuse_cfg, "public_key", "")
    secret_key = getattr(langfuse_cfg, "secret_key", "")
    if not public_key or not secret_key:
        raise ValueError(
            "Langfuse is enabled but LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY "
            "are not configured."
        )

    try:
        return LangfuseTracer(
            public_key=public_key,
            secret_key=secret_key,
            base_url=getattr(langfuse_cfg, "base_url", ""),
            trace_content=getattr(langfuse_cfg, "trace_content", "full"),
            flush_on_exit=_config_bool(getattr(langfuse_cfg, "flush_on_exit", True), True),
            debug=_config_bool(getattr(langfuse_cfg, "debug", False), False),
            mode=mode,
            provider=provider,
            model=model,
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
