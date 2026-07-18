from __future__ import annotations

from types import SimpleNamespace

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import Action, ActionType, RunResult, RunStatus, Task, ToolCall
from agent.telemetry import AgentTracer, LangfuseTracer, build_tracer_from_config
from llm.base import MockBackend
from tools.base import FailingTool, NoopTool, ToolRegistry


class FakeObservation:
    def __init__(self, client, kwargs):
        self.client = client
        self.kwargs = kwargs
        self.updates = []

    def __enter__(self):
        self.client.events.append(("enter", self.kwargs))
        return self

    def __exit__(self, exc_type, exc, tb):
        self.client.events.append(("exit", self.kwargs.get("name"), exc_type))
        return False

    def update(self, **kwargs):
        self.updates.append(kwargs)
        self.client.events.append(("update", self.kwargs.get("name"), kwargs))


class FakeClient:
    def __init__(self):
        self.events = []
        self.observations = []
        self.flush_count = 0

    def start_as_current_observation(self, **kwargs):
        self.events.append(("start", kwargs))
        observation = FakeObservation(self, kwargs)
        self.observations.append(observation)
        return observation

    def flush(self):
        self.flush_count += 1


class FakePropagate:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return FakeObservation(FakeClient(), {"name": "propagate"})


def test_langfuse_tracer_records_nested_observations_and_flushes(tmp_path):
    client = FakeClient()
    propagate = FakePropagate()
    tracer = LangfuseTracer(
        public_key="pk-test",
        secret_key="sk-test",
        client=client,
        propagate_attributes=propagate,
        mode="run",
        provider="openai",
        model="gpt-test",
    )
    task = Task(
        task_id="telemetry1",
        description="use OPENAI_API_KEY=sk-secretsecretsecret",
        repo_path=str(tmp_path),
    )

    with tracer.start_run(
        task=task,
        log_path=str(tmp_path / "logs" / "run.jsonl"),
        model="gpt-test",
        stream=False,
    ):
        with tracer.start_generation(
            step=1,
            messages=[],
            tools=[],
            model="gpt-test",
            stream=False,
        ) as generation:
            generation.update(
                output={"raw_content": "Authorization: Bearer abc123secret"},
                usage_details={"input_tokens": 1, "output_tokens": 2},
            )
        with tracer.start_tool(
            step=1,
            tool_call=ToolCall("shell", {"cmd": "echo ok"}),
        ) as tool:
            tool.update(output={"success": True, "output": "ok"})
        tracer.record_event("reflection.triggered", input={"prompt": "retry"})
        result = RunResult(
            task_id="telemetry1",
            status=RunStatus.SUCCESS,
            summary="done",
            steps_taken=1,
            total_tokens=3,
        )
        tracer.finish_run(result)

    tracer.flush()

    starts = [event[1] for event in client.events if event[0] == "start"]
    assert [s["as_type"] for s in starts] == ["agent", "generation", "tool", "span"]
    assert starts[0]["name"] == "forgeagent.run"
    assert starts[3]["metadata"]["telemetry_kind"] == "event"
    assert "sk-[REDACTED]" in starts[0]["input"]["task"]["description"]
    assert client.flush_count == 1
    assert propagate.calls[0]["trace_name"] == "forgeagent.run"


def test_build_tracer_treats_string_false_as_disabled():
    config = SimpleNamespace(
        observability=SimpleNamespace(
            langfuse=SimpleNamespace(enabled="false")
        )
    )

    tracer = build_tracer_from_config(
        config,
        mode="run",
        provider="openai",
        model="gpt-test",
    )

    assert type(tracer) is AgentTracer


class RecordingObservation:
    def __init__(self, tracer, kind, payload):
        self.tracer = tracer
        self.kind = kind
        self.payload = payload

    def __enter__(self):
        self.tracer.records.append(("enter", self.kind, self.payload))
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self.mark_error(exc)
        self.tracer.records.append(("exit", self.kind, exc_type))
        return False

    def update(self, **kwargs):
        self.tracer.records.append(("update", self.kind, kwargs))

    def mark_error(self, exc):
        self.tracer.records.append(("error", self.kind, str(exc)))


class RecordingTracer(AgentTracer):
    def __init__(self):
        self.records = []

    def start_run(self, **kwargs):
        return RecordingObservation(self, "run", kwargs)

    def start_generation(self, **kwargs):
        return RecordingObservation(self, "generation", kwargs)

    def start_tool(self, **kwargs):
        return RecordingObservation(self, "tool", kwargs)

    def record_event(self, name, **kwargs):
        self.records.append(("event", name, kwargs))

    def finish_run(self, result):
        self.records.append(("finish", result.status.value, result.steps_taken))


def _run_with_tracer(tmp_path, script, registry, max_steps=5):
    tracer = RecordingTracer()
    backend = MockBackend(script)
    agent = Agent(backend, registry, AgentConfig(tracer=tracer))
    task = Task(
        task_id="traceflow",
        description="fix",
        repo_path=str(tmp_path),
        max_steps=max_steps,
    )
    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        result = agent.run(task, log)
        events = log.replay()
    return result, tracer.records, events


def test_agent_tracer_records_generation_tool_and_completion(tmp_path):
    registry = ToolRegistry().register(NoopTool("shell", output="ok"))
    script = [
        Action(ActionType.TOOL_CALL, "run shell", ToolCall("shell", {"cmd": "echo ok"})),
        Action(ActionType.FINISH, "done", message="ok"),
    ]
    result, records, events = _run_with_tracer(tmp_path, script, registry)

    assert result.is_success()
    assert any(r[0] == "enter" and r[1] == "generation" for r in records)
    assert any(r[0] == "enter" and r[1] == "tool" for r in records)
    assert any(r[0] == "event" and r[1] == "task.complete" for r in records)
    assert [e.event_type.value for e in events][-1] == "task_complete"


def test_agent_tracer_records_reflection_event(tmp_path):
    registry = ToolRegistry().register(FailingTool("test"))
    script = [
        Action(ActionType.TOOL_CALL, "run tests", ToolCall("test", {})),
        Action(ActionType.FINISH, "done", message="ok"),
    ]
    result, records, _ = _run_with_tracer(tmp_path, script, registry)

    assert result.is_success()
    assert any(r[0] == "event" and r[1] == "reflection.triggered" for r in records)


def test_agent_tracer_records_max_steps_failure(tmp_path):
    registry = ToolRegistry().register(NoopTool("shell", output="ok"))
    script = [
        Action(ActionType.TOOL_CALL, "run shell", ToolCall("shell", {"cmd": "echo ok"})),
        Action(ActionType.TOOL_CALL, "run shell", ToolCall("shell", {"cmd": "pwd"})),
    ]
    result, records, _ = _run_with_tracer(tmp_path, script, registry, max_steps=2)

    assert result.status == RunStatus.MAX_STEPS
    assert any(
        r[0] == "event"
        and r[1] == "task.failed"
        and r[2]["metadata"]["failure_stage"] == "max_steps"
        for r in records
    )


def test_agent_tracer_records_llm_failure(tmp_path):
    class BrokenBackend(MockBackend):
        def complete(self, messages, tools):
            raise ConnectionError("network down")

    tracer = RecordingTracer()
    registry = ToolRegistry().register(NoopTool("shell", output="ok"))
    agent = Agent(
        BrokenBackend([]),
        registry,
        AgentConfig(tracer=tracer, llm_max_retries=1, llm_retry_delay=0.01),
    )
    task = Task(task_id="llmfail", description="fix", repo_path=str(tmp_path), max_steps=3)

    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        result = agent.run(task, log)

    assert result.status == RunStatus.FAILED
    assert any(r[0] == "event" and r[1] == "task.failed" for r in tracer.records)
    assert any(r[0] == "error" and r[1] == "generation" for r in tracer.records)
