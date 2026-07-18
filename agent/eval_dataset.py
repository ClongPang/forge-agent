"""
agent/eval_dataset.py

Helpers for turning Langfuse traces into regression dataset items.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_DATASET_NAME = "forge-agent/regression"


@dataclass
class TraceReference:
    trace_id: str
    source_observation_id: str | None = None
    focused_observation_id: str | None = None


@dataclass
class DatasetAddResult:
    dataset_name: str
    dataset_item_id: str | None
    trace_id: str
    source_observation_id: str | None
    focused_observation_id: str | None
    failure_type: str
    created_dataset: bool


def parse_trace_reference(value: str) -> TraceReference:
    """Accept either a raw trace id or a Langfuse trace URL."""
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        query = parse_qs(parsed.query)
        trace_id = _first(query.get("traceId")) or _trace_id_from_path(parsed.path)
        if not trace_id:
            raise ValueError("Could not find traceId in Langfuse URL.")
        return TraceReference(
            trace_id=trace_id,
            source_observation_id=_first(query.get("peek")),
            focused_observation_id=_first(query.get("observation")),
        )
    if not value.strip():
        raise ValueError("Trace id is required.")
    return TraceReference(trace_id=value.strip())


def create_langfuse_client_from_config(config: Any) -> Any:
    """Create a Langfuse client using loaded config/env without exposing secrets."""
    langfuse_cfg = getattr(getattr(config, "observability", None), "langfuse", None)
    public_key = (
        getattr(langfuse_cfg, "public_key", "")
        or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    )
    secret_key = (
        getattr(langfuse_cfg, "secret_key", "")
        or os.environ.get("LANGFUSE_SECRET_KEY", "")
    )
    base_url = (
        getattr(langfuse_cfg, "base_url", "")
        or os.environ.get("LANGFUSE_BASE_URL", "")
        or os.environ.get("LANGFUSE_HOST", "")
    )
    if not public_key or not secret_key:
        raise ValueError(
            "Langfuse credentials are missing. Set LANGFUSE_PUBLIC_KEY and "
            "LANGFUSE_SECRET_KEY in .env or your environment."
        )
    try:
        from langfuse import Langfuse
    except ImportError as exc:
        raise ValueError("Langfuse SDK is not installed. Install langfuse>=4,<5.") from exc

    kwargs: dict[str, Any] = {
        "public_key": public_key,
        "secret_key": secret_key,
    }
    if base_url:
        kwargs["base_url"] = base_url
    return Langfuse(**kwargs)


def add_trace_to_dataset(
    trace_ref: str,
    *,
    dataset_name: str = DEFAULT_DATASET_NAME,
    config: Any = None,
    client: Any = None,
    source_observation_id: str | None = None,
    focused_observation_id: str | None = None,
    failure_type: str | None = None,
    notes: str | None = None,
    create_dataset: bool = True,
) -> DatasetAddResult:
    reference = parse_trace_reference(trace_ref)
    if source_observation_id:
        reference.source_observation_id = source_observation_id
    if focused_observation_id:
        reference.focused_observation_id = focused_observation_id

    client = client or create_langfuse_client_from_config(config)
    trace = _to_plain(client.api.trace.get(reference.trace_id))
    root_observation_id = _find_root_observation_id(trace)
    linked_observation_id = reference.source_observation_id or root_observation_id

    detected_failure_modes = _detect_failure_modes(trace)
    resolved_failure_type = (
        failure_type
        or (detected_failure_modes[0] if detected_failure_modes else "needs_review")
    )
    input_payload = _build_dataset_input(trace)
    expected_output = _build_expected_output(
        trace,
        detected_failure_modes=detected_failure_modes,
        failure_type=resolved_failure_type,
        notes=notes,
    )
    metadata = _build_metadata(
        trace,
        root_observation_id=root_observation_id,
        source_observation_id=linked_observation_id,
        focused_observation_id=reference.focused_observation_id,
        failure_type=resolved_failure_type,
        notes=notes,
    )

    created_dataset = False
    if create_dataset:
        created_dataset = _ensure_dataset(client, dataset_name)

    item = client.create_dataset_item(
        dataset_name=dataset_name,
        input=input_payload,
        expected_output=expected_output,
        metadata=metadata,
        source_trace_id=reference.trace_id,
        source_observation_id=linked_observation_id,
    )

    return DatasetAddResult(
        dataset_name=dataset_name,
        dataset_item_id=_get(item, "id"),
        trace_id=reference.trace_id,
        source_observation_id=linked_observation_id,
        focused_observation_id=reference.focused_observation_id,
        failure_type=resolved_failure_type,
        created_dataset=created_dataset,
    )


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0] or None


def _trace_id_from_path(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if "traces" not in parts:
        return None
    idx = parts.index("traces")
    if len(parts) > idx + 1:
        return parts[idx + 1]
    return None


def _to_plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            return _to_plain(value.model_dump(mode="json", by_alias=True))
        except TypeError:
            return _to_plain(value.model_dump())
    if hasattr(value, "dict"):
        return _to_plain(value.dict())
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _find_root_observation_id(trace: dict[str, Any]) -> str | None:
    observations = trace.get("observations") or []
    for observation in observations:
        parent_id = _get(
            observation,
            "parentObservationId",
            _get(observation, "parent_observation_id"),
        )
        if (
            str(_get(observation, "type", "")).upper() == "AGENT"
            and parent_id is None
        ):
            return _get(observation, "id")
    for observation in observations:
        parent_id = _get(
            observation,
            "parentObservationId",
            _get(observation, "parent_observation_id"),
        )
        if parent_id is None:
            return _get(observation, "id")
    return None


def _build_dataset_input(trace: dict[str, Any]) -> dict[str, Any]:
    trace_input = trace.get("input") or {}
    trace_input_dict = trace_input if isinstance(trace_input, dict) else {}
    task = trace_input_dict.get("task")
    task = task if isinstance(task, dict) else {}
    description = task.get("description")
    return {
        "task": description,
        "repo_path": task.get("repo_path") or trace_input_dict.get("repo_path"),
        "mode": _mode_from_trace_name(trace.get("name")),
        "trace_input": trace_input,
    }


def _build_expected_output(
    trace: dict[str, Any],
    *,
    detected_failure_modes: list[str],
    failure_type: str,
    notes: str | None,
) -> dict[str, Any]:
    output = trace.get("output") or {}
    summary = output.get("summary") if isinstance(output, dict) else str(output)
    checks: dict[str, Any] = {
        "must_complete_user_task": True,
        "must_not_report_success_for_incomplete_work": True,
    }
    if "premature_finish_with_unexecuted_tool_plan" in detected_failure_modes:
        checks["must_not_contain_unexecuted_tool_markers"] = ["Action:", "Params:"]
        checks["must_execute_planned_tool_calls_before_finish"] = True

    return {
        "failure_type": failure_type,
        "detected_failure_modes": detected_failure_modes,
        "regression_checks": checks,
        "expected_behavior": (
            "Re-run the same task and produce a materially complete answer. "
            "If tool use is needed, execute tools instead of writing tool-call "
            "syntax into the final answer."
        ),
        "source_output_summary": _truncate(summary, 1200),
        "notes": notes,
    }


def _build_metadata(
    trace: dict[str, Any],
    *,
    root_observation_id: str | None,
    source_observation_id: str | None,
    focused_observation_id: str | None,
    failure_type: str,
    notes: str | None,
) -> dict[str, Any]:
    trace_metadata = trace.get("metadata") or {}
    output = trace.get("output") or {}
    return {
        "source": "langfuse_trace",
        "trace_name": trace.get("name"),
        "trace_timestamp": trace.get("timestamp"),
        "project_id": trace.get("projectId") or trace.get("project_id"),
        "root_observation_id": root_observation_id,
        "source_observation_id": source_observation_id,
        "focused_observation_id": focused_observation_id,
        "failure_type": failure_type,
        "notes": notes,
        "event_log_path": trace_metadata.get("event_log_path"),
        "provider": trace_metadata.get("provider"),
        "model": trace_metadata.get("model"),
        "final_status": trace_metadata.get("final_status")
        or (output.get("status") if isinstance(output, dict) else None),
        "steps_taken": trace_metadata.get("steps_taken")
        or (output.get("steps_taken") if isinstance(output, dict) else None),
        "total_tokens": trace_metadata.get("total_tokens")
        or (output.get("total_tokens") if isinstance(output, dict) else None),
        "has_patch": trace_metadata.get("has_patch"),
    }


def _detect_failure_modes(trace: dict[str, Any]) -> list[str]:
    output = trace.get("output") or {}
    summary = output.get("summary") if isinstance(output, dict) else str(output)
    modes: list[str] = []
    if isinstance(summary, str) and "Action:" in summary and "Params:" in summary:
        modes.append("premature_finish_with_unexecuted_tool_plan")
    return modes


def _mode_from_trace_name(name: str | None) -> str:
    if name == "forgeagent.chat-round":
        return "chat"
    if name == "forgeagent.run":
        return "run"
    return name or "unknown"


def _truncate(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _ensure_dataset(client: Any, dataset_name: str) -> bool:
    try:
        client.get_dataset(dataset_name, fetch_items_page_size=1)
        return False
    except Exception:
        try:
            client.create_dataset(
                name=dataset_name,
                description="Forge Agent regression cases captured from Langfuse traces.",
                metadata={"source": "forge-agent", "kind": "regression"},
            )
            return True
        except Exception as exc:
            text = str(exc).lower()
            if "already exists" in text or "conflict" in text or "409" in text:
                return False
            raise
