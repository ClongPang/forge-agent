from __future__ import annotations

from types import SimpleNamespace

from agent.eval_dataset import (
    DEFAULT_DATASET_NAME,
    add_trace_to_dataset,
    parse_trace_reference,
)


class FakeTraceApi:
    def __init__(self, trace):
        self.trace = trace
        self.requested_trace_id = None

    def get(self, trace_id):
        self.requested_trace_id = trace_id
        return self.trace


class FakeApi:
    def __init__(self, trace):
        self.trace = FakeTraceApi(trace)


class FakeLangfuseClient:
    def __init__(self, trace, dataset_exists=False):
        self.api = FakeApi(trace)
        self.dataset_exists = dataset_exists
        self.created_datasets = []
        self.created_items = []

    def get_dataset(self, name, *, fetch_items_page_size=50):
        if not self.dataset_exists:
            raise RuntimeError("dataset not found")
        return SimpleNamespace(name=name)

    def create_dataset(self, **kwargs):
        self.dataset_exists = True
        self.created_datasets.append(kwargs)
        return SimpleNamespace(name=kwargs["name"])

    def create_dataset_item(self, **kwargs):
        self.created_items.append(kwargs)
        return SimpleNamespace(id="item-1")


def _trace():
    return {
        "id": "trace-1",
        "name": "forgeagent.chat-round",
        "timestamp": "2026-07-17T10:02:35.899Z",
        "projectId": "project-1",
        "input": {
            "task": {
                "description": "你觉得当前项目存在哪些问题",
                "repo_path": "/repo",
            },
            "repo_path": "/repo",
        },
        "output": {
            "status": "success",
            "summary": 'Action: file_read\nParams: {"path": "agent/core.py"}',
            "steps_taken": 4,
            "total_tokens": 52557,
        },
        "metadata": {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "event_log_path": "logs/run.jsonl",
            "has_patch": False,
        },
        "observations": [
            {
                "id": "root-observation",
                "type": "AGENT",
                "parent_observation_id": None,
            },
            {
                "id": "generation-observation",
                "type": "GENERATION",
                "parent_observation_id": "root-observation",
            },
        ],
    }


def test_parse_trace_reference_accepts_langfuse_trace_url():
    reference = parse_trace_reference(
        "https://cloud.langfuse.com/project/proj/traces"
        "?peek=root-observation"
        "&observation=generation-observation"
        "&traceId=trace-1"
    )

    assert reference.trace_id == "trace-1"
    assert reference.source_observation_id == "root-observation"
    assert reference.focused_observation_id == "generation-observation"


def test_parse_trace_reference_accepts_raw_trace_id():
    reference = parse_trace_reference("trace-1")

    assert reference.trace_id == "trace-1"
    assert reference.source_observation_id is None
    assert reference.focused_observation_id is None


def test_add_trace_to_dataset_creates_item_from_trace_url():
    client = FakeLangfuseClient(_trace())

    result = add_trace_to_dataset(
        "https://cloud.langfuse.com/project/proj/traces"
        "?peek=root-observation"
        "&observation=generation-observation"
        "&traceId=trace-1",
        client=client,
        notes="The final answer contained unexecuted tool syntax.",
    )

    assert client.api.trace.requested_trace_id == "trace-1"
    assert client.created_datasets[0]["name"] == DEFAULT_DATASET_NAME
    assert result.created_dataset is True
    assert result.dataset_item_id == "item-1"
    assert result.failure_type == "premature_finish_with_unexecuted_tool_plan"
    assert result.source_observation_id == "root-observation"
    assert result.focused_observation_id == "generation-observation"

    item = client.created_items[0]
    assert item["dataset_name"] == DEFAULT_DATASET_NAME
    assert item["source_trace_id"] == "trace-1"
    assert item["source_observation_id"] == "root-observation"
    assert item["input"]["task"] == "你觉得当前项目存在哪些问题"
    assert item["input"]["mode"] == "chat"
    assert item["expected_output"]["failure_type"] == (
        "premature_finish_with_unexecuted_tool_plan"
    )
    assert item["expected_output"]["regression_checks"][
        "must_execute_planned_tool_calls_before_finish"
    ] is True
    assert item["metadata"]["root_observation_id"] == "root-observation"
    assert item["metadata"]["focused_observation_id"] == "generation-observation"
    assert item["metadata"]["provider"] == "deepseek"
    assert item["metadata"]["event_log_path"] == "logs/run.jsonl"


def test_add_trace_to_dataset_uses_root_observation_for_raw_trace_id():
    client = FakeLangfuseClient(_trace(), dataset_exists=True)

    result = add_trace_to_dataset(
        "trace-1",
        client=client,
        failure_type="manual_review",
    )

    assert client.created_datasets == []
    assert result.created_dataset is False
    assert result.failure_type == "manual_review"
    assert result.source_observation_id == "root-observation"
    assert client.created_items[0]["source_observation_id"] == "root-observation"
