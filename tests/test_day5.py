"""
tests/test_day5.py

Day 5 测试：RepoMap、TokenBudget、ConversationHistory，以及 core.py 集成。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from context.history import ConversationHistory
from context.repo_map import RepoMap, _extract_python_symbols, _extract_symbols_regex
from context.token_budget import TokenBudget, estimate_tokens
from llm.base import LLMBackend, LLMMessage, LLMResponse, MockBackend
from agent.task import Action, ActionType, Task, ToolCall
from tools.base import FailingTool, NoopTool, ToolRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def py_repo(tmp_path) -> Path:
    """包含几个 Python 文件的示例 repo。"""
    (tmp_path / "main.py").write_text(
        "def run():\n    pass\n\nclass App:\n    def start(self):\n        pass\n"
    )
    (tmp_path / "utils.py").write_text(
        "def helper():\n    return 1\n\ndef another():\n    pass\n"
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "module.py").write_text("class SubModule:\n    pass\n")
    # 应被跳过
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "main.cpython-312.pyc").write_bytes(b"\x00" * 10)
    return tmp_path


def _raw_tool_call(
    call_id: str,
    name: str = "shell",
    arguments: str = '{"cmd": "ls"}',
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


# ===========================================================================
# estimate_tokens
# ===========================================================================

class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") >= 1

    def test_short_string(self):
        assert estimate_tokens("hello") >= 1

    def test_longer_string(self):
        text = "a" * 400
        # 不断言具体值，只断言比短文本多
        short = estimate_tokens("a" * 10)
        assert estimate_tokens(text) > short

    def test_proportional(self):
        # tiktoken 对重复字符有压缩，不严格线性
        # 只断言更长的文本 token 数更多
        t1 = estimate_tokens("hello world " * 10)
        t2 = estimate_tokens("hello world " * 20)
        assert t2 > t1


# ===========================================================================
# RepoMap
# ===========================================================================

class TestRepoMap:
    def test_build_returns_string(self, py_repo):
        rm = RepoMap(py_repo)
        result = rm.build(budget=10_000)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_file_names(self, py_repo):
        rm = RepoMap(py_repo)
        result = rm.build(budget=10_000)
        assert "main.py" in result
        assert "utils.py" in result

    def test_contains_symbol_names(self, py_repo):
        rm = RepoMap(py_repo)
        result = rm.build(budget=10_000)
        assert "run" in result
        assert "App" in result
        assert "helper" in result

    def test_skips_pycache(self, py_repo):
        rm = RepoMap(py_repo)
        result = rm.build(budget=10_000)
        assert "__pycache__" not in result

    def test_budget_limits_output(self, py_repo):
        rm = RepoMap(py_repo)
        # 非常小的预算，输出应该被截断
        result = rm.build(budget=10)
        assert len(result) <= 10 * 4 + 100  # 留一点 truncation message 的余量

    def test_empty_repo(self, tmp_path):
        rm = RepoMap(tmp_path)
        result = rm.build()
        assert "empty" in result.lower()

    def test_nonexistent_path_graceful(self, tmp_path):
        # 不存在的 path 不应崩溃
        rm = RepoMap(tmp_path / "no_such_dir")
        result = rm.build()
        assert isinstance(result, str)

    def test_large_file_skipped(self, tmp_path):
        # 超过 500KB 的文件应被跳过
        big = tmp_path / "big.py"
        big.write_bytes(b"x = 1\n" * 100_000)   # ~600KB
        rm = RepoMap(tmp_path)
        result = rm.build()
        assert "big.py" not in result


class TestExtractPythonSymbols:
    def test_extracts_function(self, tmp_path):
        code = "def foo():\n    pass\n"
        syms = _extract_python_symbols(code, Path("test.py"))
        names = [s.name for s in syms]
        assert "foo" in names

    def test_extracts_class(self, tmp_path):
        code = "class MyClass:\n    pass\n"
        syms = _extract_python_symbols(code, Path("test.py"))
        names = [s.name for s in syms]
        assert "MyClass" in names

    def test_method_has_indent(self, tmp_path):
        code = "class Foo:\n    def bar(self):\n        pass\n"
        syms = _extract_python_symbols(code, Path("test.py"))
        bar = next((s for s in syms if s.name == "bar"), None)
        assert bar is not None
        assert bar.indent > 0
        assert not bar.is_toplevel

    def test_toplevel_function_no_indent(self, tmp_path):
        code = "def baz():\n    pass\n"
        syms = _extract_python_symbols(code, Path("test.py"))
        baz = next((s for s in syms if s.name == "baz"), None)
        assert baz is not None
        assert baz.is_toplevel

    def test_line_numbers_correct(self, tmp_path):
        code = "# comment\ndef foo():\n    pass\n"
        syms = _extract_python_symbols(code, Path("test.py"))
        foo = next((s for s in syms if s.name == "foo"), None)
        assert foo is not None
        assert foo.line == 2

    def test_syntax_error_falls_back_to_regex(self, tmp_path):
        # 语法错误的代码应 fallback 到正则，不崩溃
        code = "def foo(\n    # broken syntax"
        syms = _extract_python_symbols(code, Path("test.py"))
        assert isinstance(syms, list)


class TestExtractSymbolsRegex:
    def test_extracts_def(self):
        code = "def my_func():\n    pass\n"
        syms = _extract_symbols_regex(code, Path("test.py"))
        assert any(s.name == "my_func" for s in syms)

    def test_extracts_class(self):
        code = "class MyClass:\n    pass\n"
        syms = _extract_symbols_regex(code, Path("test.py"))
        assert any(s.name == "MyClass" for s in syms)

    def test_javascript_function(self):
        code = "function myFunc() {\n    return 1;\n}\n"
        syms = _extract_symbols_regex(code, Path("test.js"))
        assert any(s.name == "myFunc" for s in syms)


# ===========================================================================
# TokenBudget
# ===========================================================================

class TestTokenBudget:
    def test_default_plan_sums_to_budget(self):
        budget = TokenBudget(total=80_000)
        plan = budget.default_plan()
        assert plan.total == 80_000
        assert plan.reserve > 0
        assert plan.system_core + plan.repo_map + plan.history + plan.observation <= plan.available

    def test_trim_to_short_text_unchanged(self):
        budget = TokenBudget()
        text = "hello world"
        assert budget.trim_to(text, token_limit=1000) == text

    def test_trim_to_long_text_truncated(self):
        budget = TokenBudget()
        text = "x" * 10_000
        result = budget.trim_to(text, token_limit=100)
        assert len(result) < len(text)
        assert "truncated" in result

    def test_trim_history_short_unchanged(self):
        budget = TokenBudget()
        msgs = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "ok"},
        ]
        result = budget.trim_history(msgs, token_limit=10_000)
        assert len(result) == 2

    def test_trim_history_preserves_first_message(self):
        budget = TokenBudget(total=1000)
        # 第一条很短，后面很多长消息
        msgs = [{"role": "user", "content": "task"}]
        for i in range(20):
            msgs.append({"role": "user", "content": "x" * 200})
        result = budget.trim_history(msgs, token_limit=50)
        assert result[0]["content"] == "task"

    def test_trim_history_keeps_recent_messages(self):
        budget = TokenBudget()
        msgs = [{"role": "user", "content": "task"}]
        for i in range(10):
            msgs.append({"role": "user", "content": f"message {i}"})
        # 预算只够放 3 条
        result = budget.trim_history(msgs, token_limit=15)
        contents = [m["content"] for m in result]
        # 最新的消息（message 9）应该在
        assert any("message 9" in c for c in contents)

    def test_trim_history_adds_truncation_notice(self):
        budget = TokenBudget()
        msgs = [{"role": "user", "content": "task"}]
        for i in range(20):
            msgs.append({"role": "user", "content": "x" * 100})
        result = budget.trim_history(msgs, token_limit=50)
        # 应该有一条截断提示消息
        contents = " ".join(m["content"] for m in result)
        assert "truncated" in contents.lower()

    def test_trim_history_empty(self):
        budget = TokenBudget()
        assert budget.trim_history([], token_limit=1000) == []

    def test_usage_report(self):
        budget = TokenBudget(total=10_000)
        report = budget.usage_report("system", "repo", [], "obs")
        assert "system" in report
        assert "total" in report
        assert report["budget"] == 10_000

    def test_trim_history_preserves_protocol_metadata(self):
        budget = TokenBudget(total=10_000)
        tool_calls = [_raw_tool_call("call_1")]
        msgs = [
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "I should inspect files.",
                "tool_calls": tool_calls,
            },
            {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
        ]

        result = budget.trim_history(msgs, token_limit=10_000)

        assert result[1]["tool_calls"] == tool_calls
        assert result[1]["reasoning_content"] == "I should inspect files."
        assert result[2]["tool_call_id"] == "call_1"

    def test_trim_history_drops_orphan_tool_message(self):
        budget = TokenBudget(total=10_000)
        msgs = [
            {"role": "user", "content": "task"},
            {"role": "tool", "content": "orphan", "tool_call_id": "call_1"},
            {"role": "user", "content": "next"},
        ]

        result = budget.trim_history(msgs, token_limit=10_000)

        assert all(m["role"] != "tool" for m in result)
        assert result[-1]["content"] == "next"

    def test_trim_history_drops_assistant_tool_calls_without_ids(self):
        budget = TokenBudget(total=10_000)
        msgs = [
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }],
            },
            {"role": "user", "content": "next"},
        ]

        result = budget.trim_history(msgs, token_limit=10_000)

        assert all("tool_calls" not in m for m in result)
        assert result[-1]["content"] == "next"

    def test_trim_history_keeps_tool_call_block_atomic(self):
        budget = TokenBudget(total=10_000)
        tool_calls = [_raw_tool_call("call_1")]
        msgs = [
            {"role": "user", "content": "task"},
            {"role": "user", "content": "older"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "I should inspect files.",
                "tool_calls": tool_calls,
            },
            {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
        ]

        result = budget.trim_history(msgs, token_limit=10_000)

        assert result[-2]["tool_calls"] == tool_calls
        assert result[-2]["reasoning_content"] == "I should inspect files."
        assert result[-1]["tool_call_id"] == "call_1"

    def test_trim_history_drops_oversized_tool_call_block_as_unit(self):
        budget = TokenBudget(total=10_000)
        tool_calls = [_raw_tool_call("call_1")]
        msgs = [
            {"role": "user", "content": "task"},
            {"role": "user", "content": "recent"},
            {
                "role": "assistant",
                "content": "x" * 2000,
                "tool_calls": tool_calls,
            },
            {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
        ]

        result = budget.trim_history(msgs, token_limit=20)

        assert all("tool_calls" not in m for m in result)
        assert all(m.get("tool_call_id") != "call_1" for m in result)

    def test_trim_history_keeps_multiple_tool_results_atomic(self):
        budget = TokenBudget(total=10_000)
        tool_calls = [
            _raw_tool_call("call_1"),
            _raw_tool_call("call_2", name="test", arguments="{}"),
        ]
        msgs = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "", "tool_calls": tool_calls},
            {"role": "tool", "content": "files", "tool_call_id": "call_1"},
            {"role": "tool", "content": "tests", "tool_call_id": "call_2"},
        ]

        result = budget.trim_history(msgs, token_limit=10_000)

        assert [m.get("tool_call_id") for m in result if m["role"] == "tool"] == [
            "call_1",
            "call_2",
        ]


# ===========================================================================
# ConversationHistory
# ===========================================================================

class TestConversationHistory:
    def test_add_and_retrieve(self):
        h = ConversationHistory(max_messages=10)
        h.add(LLMMessage(role="user", content="hello"))
        assert h.message_count == 1
        assert h.to_list()[0].content == "hello"

    def test_sliding_window_drops_oldest(self):
        h = ConversationHistory(max_messages=3)
        h.add(LLMMessage(role="user", content="first"))
        h.add(LLMMessage(role="user", content="second"))
        h.add(LLMMessage(role="user", content="third"))
        h.add(LLMMessage(role="user", content="fourth"))
        # 最多 3 条，first 应该被丢弃（但保留 index 0）
        assert h.message_count == 3
        contents = [m.content for m in h.to_list()]
        assert "first" in contents      # index 0 永不丢弃
        assert "second" not in contents  # 被丢弃
        assert "fourth" in contents

    def test_first_message_never_dropped(self):
        h = ConversationHistory(max_messages=2)
        h.add(LLMMessage(role="user", content="task_description"))
        for i in range(10):
            h.add(LLMMessage(role="user", content=f"msg_{i}"))
        assert h.to_list()[0].content == "task_description"

    def test_to_dicts(self):
        h = ConversationHistory()
        h.add(LLMMessage(role="user", content="hello"))
        dicts = h.to_dicts()
        assert dicts == [{"role": "user", "content": "hello"}]

    def test_from_dicts(self):
        dicts = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "ok"}]
        h = ConversationHistory.from_dicts(dicts)
        assert h.message_count == 2
        assert h.to_list()[0].content == "task"

    def test_protocol_metadata_round_trips(self):
        tool_calls = [_raw_tool_call("call_1")]
        h = ConversationHistory()
        h.add(LLMMessage(
            role="assistant",
            content="",
            reasoning_content="I should inspect files.",
            tool_calls=tool_calls,
        ))
        h.add(LLMMessage(
            role="tool",
            content="ok",
            tool_call_id="call_1",
        ))

        restored = ConversationHistory.from_dicts(h.to_dicts())
        messages = restored.to_list()
        assert messages[0].reasoning_content == "I should inspect files."
        assert messages[0].tool_calls == tool_calls
        assert messages[1].tool_call_id == "call_1"

    def test_trim_keeps_tool_call_block_atomic(self):
        tool_calls = [_raw_tool_call("call_1")]
        h = ConversationHistory(max_messages=4)
        h.add(LLMMessage(role="user", content="task"))
        h.add(LLMMessage(role="user", content="older"))
        h.add_many([
            LLMMessage(
                role="assistant",
                content="",
                reasoning_content="inspect",
                tool_calls=tool_calls,
            ),
            LLMMessage(role="tool", content="ok", tool_call_id="call_1"),
        ])
        h.add(LLMMessage(role="user", content="newest"))

        messages = h.to_list()

        assert messages[0].content == "task"
        assert all(m.content != "older" for m in messages)
        assert [m.tool_call_id for m in messages if m.role == "tool"] == ["call_1"]
        assert any(m.role == "assistant" and m.tool_calls for m in messages)

    def test_trim_drops_tool_call_block_instead_of_splitting_it(self):
        tool_calls = [_raw_tool_call("call_1")]
        h = ConversationHistory(max_messages=3)
        h.add(LLMMessage(role="user", content="task"))
        h.add(LLMMessage(role="user", content="older"))
        h.add_many([
            LLMMessage(role="assistant", content="", tool_calls=tool_calls),
            LLMMessage(role="tool", content="ok", tool_call_id="call_1"),
        ])
        h.add(LLMMessage(role="user", content="newest"))

        messages = h.to_list()

        assert all(m.tool_call_id != "call_1" for m in messages)
        assert all(not m.tool_calls for m in messages)

    def test_add_many(self):
        h = ConversationHistory(max_messages=5)
        msgs = [LLMMessage(role="user", content=f"m{i}") for i in range(3)]
        h.add_many(msgs)
        assert h.message_count == 3

    def test_clear_except_first(self):
        h = ConversationHistory()
        h.add(LLMMessage(role="user", content="task"))
        h.add(LLMMessage(role="assistant", content="ok"))
        h.add(LLMMessage(role="user", content="more"))
        h.clear_except_first()
        assert h.message_count == 1
        assert h.to_list()[0].content == "task"

    def test_last_message(self):
        h = ConversationHistory()
        h.add(LLMMessage(role="user", content="first"))
        h.add(LLMMessage(role="assistant", content="last"))
        assert h.last_message.content == "last"

    def test_empty_history_last_message_is_none(self):
        h = ConversationHistory()
        assert h.last_message is None

    def test_len(self):
        h = ConversationHistory()
        h.add(LLMMessage(role="user", content="x"))
        assert len(h) == 1


# ===========================================================================
# core.py 集成：context 模块接入后仍能正常运行
# ===========================================================================

class TestCoreWithContext:
    def _make_task(self, tmp_path) -> Task:
        return Task(
            task_id="ctx001",
            description="Fix the bug",
            repo_path=str(tmp_path),
            max_steps=5,
            budget_tokens=80_000,
        )

    def test_run_with_context_succeeds(self, tmp_path):
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog

        task = self._make_task(tmp_path)
        registry = ToolRegistry().register(NoopTool("shell"))
        script = [
            Action(ActionType.TOOL_CALL, "explore", ToolCall("shell", {"cmd": "ls"})),
            Action(ActionType.FINISH, "done", message="Task complete"),
        ]
        backend = MockBackend(script)
        config = AgentConfig(budget_tokens=80_000, history_max_messages=20)
        agent = Agent(backend, registry, config)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()

    def test_native_tool_call_history_is_passed_to_next_turn(self, tmp_path):
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog

        class NativeToolBackend(LLMBackend):
            def __init__(self):
                self.received_messages = []
                self.call_count = 0

            @property
            def model_name(self) -> str:
                return "deepseek-v4-pro"

            def complete(self, messages, tools):
                self.received_messages.append(messages)
                self.call_count += 1
                if self.call_count == 1:
                    tool_calls = [_raw_tool_call("call_1")]
                    return LLMResponse(
                        action=Action(
                            ActionType.TOOL_CALL,
                            "I should inspect files.",
                            ToolCall("shell", {"cmd": "ls"}, id="call_1"),
                        ),
                        raw_content="I should inspect files.",
                        assistant_message=LLMMessage(
                            role="assistant",
                            content="",
                            reasoning_content="I should inspect files.",
                            tool_calls=tool_calls,
                        ),
                    )
                return LLMResponse(
                    action=Action(ActionType.FINISH, "done", message="ok"),
                    raw_content="ok",
                )

        task = self._make_task(tmp_path)
        registry = ToolRegistry().register(NoopTool("shell"))
        backend = NativeToolBackend()
        config = AgentConfig(budget_tokens=80_000, history_max_messages=20)
        agent = Agent(backend, registry, config)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()
        assert backend.call_count == 2
        second_messages = backend.received_messages[1]
        assistant = next(m for m in second_messages if m.role == "assistant" and m.tool_calls)
        tool = next(m for m in second_messages if m.role == "tool")
        assert assistant.reasoning_content == "I should inspect files."
        assert assistant.tool_calls[0]["id"] == "call_1"
        assert tool.tool_call_id == "call_1"
        assert "[UNTRUSTED TOOL OUTPUT BEGIN]" in tool.content

    def test_native_tool_call_history_trim_stays_protocol_safe(self, tmp_path):
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog

        def assert_protocol_safe(messages):
            pending = set()
            for message in messages:
                if message.role == "assistant":
                    pending = {
                        item["id"]
                        for item in (message.tool_calls or [])
                        if item.get("id")
                    }
                    continue
                if message.role == "tool":
                    assert message.tool_call_id in pending
                    pending.remove(message.tool_call_id)
                    continue
                assert not pending

        class TwoToolTurnsBackend(LLMBackend):
            def __init__(self):
                self.received_messages = []
                self.call_count = 0

            @property
            def model_name(self) -> str:
                return "deepseek-v4-pro"

            def complete(self, messages, tools):
                assert_protocol_safe(messages)
                self.received_messages.append(messages)
                self.call_count += 1
                if self.call_count in (1, 2):
                    call_id = f"call_{self.call_count}"
                    raw_tool_calls = [_raw_tool_call(call_id)]
                    return LLMResponse(
                        action=Action(
                            ActionType.TOOL_CALL,
                            "inspect",
                            ToolCall("shell", {"cmd": "ls"}, id=call_id),
                        ),
                        raw_content="inspect",
                        assistant_message=LLMMessage(
                            role="assistant",
                            content="",
                            reasoning_content="inspect",
                            tool_calls=raw_tool_calls,
                        ),
                    )
                return LLMResponse(
                    action=Action(ActionType.FINISH, "done", message="ok"),
                    raw_content="ok",
                )

        task = self._make_task(tmp_path)
        registry = ToolRegistry().register(NoopTool("shell"))
        backend = TwoToolTurnsBackend()
        agent = Agent(
            backend,
            registry,
            AgentConfig(budget_tokens=80_000, history_max_messages=4),
        )

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()
        assert backend.call_count == 3
        final_history = backend.received_messages[-1]
        assert any(
            m.role == "tool" and m.tool_call_id == "call_2"
            for m in final_history
        )
        assert not any(
            m.role == "tool" and m.tool_call_id == "call_1"
            for m in final_history
        )

    def test_multiple_native_tool_calls_append_matching_tool_messages(self, tmp_path):
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog

        class MultiToolBackend(LLMBackend):
            def __init__(self):
                self.received_messages = []
                self.call_count = 0

            @property
            def model_name(self) -> str:
                return "deepseek-v4-pro"

            def complete(self, messages, tools):
                self.received_messages.append(messages)
                self.call_count += 1
                if self.call_count == 1:
                    raw_tool_calls = [
                        _raw_tool_call("call_1"),
                        _raw_tool_call(
                            "call_2",
                            name="git_commit",
                            arguments='{"message": "test"}',
                        ),
                    ]
                    calls = [
                        ToolCall("shell", {"cmd": "ls"}, id="call_1"),
                        ToolCall("git_commit", {"message": "test"}, id="call_2"),
                    ]
                    return LLMResponse(
                        action=Action(
                            action_type=ActionType.TOOL_CALL,
                            thought="I should run two independent actions.",
                            tool_call=calls[0],
                            tool_calls=calls,
                        ),
                        raw_content="I should run two independent actions.",
                        assistant_message=LLMMessage(
                            role="assistant",
                            content="",
                            reasoning_content="I should run two independent actions.",
                            tool_calls=raw_tool_calls,
                        ),
                    )
                return LLMResponse(
                    action=Action(ActionType.FINISH, "done", message="ok"),
                    raw_content="ok",
                )

        task = self._make_task(tmp_path)
        registry = ToolRegistry().register(NoopTool("shell"))
        backend = MultiToolBackend()
        agent = Agent(backend, registry, AgentConfig(budget_tokens=80_000))

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()
        second_messages = backend.received_messages[1]
        tool_messages = [m for m in second_messages if m.role == "tool"]
        assert [m.tool_call_id for m in tool_messages] == ["call_1", "call_2"]
        assert "Status: SUCCESS" in tool_messages[0].content
        assert "Unknown tool 'git_commit'" in tool_messages[1].content

    def test_multiple_tool_calls_trigger_test_reflection_once(self, tmp_path):
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog
        from agent.task import EventType

        task = self._make_task(tmp_path)
        registry = (
            ToolRegistry()
            .register(FailingTool("test"))
            .register(NoopTool("shell"))
        )
        calls = [
            ToolCall("test", {}, id="call_1"),
            ToolCall("shell", {"cmd": "ls"}, id="call_2"),
        ]
        script = [
            Action(
                action_type=ActionType.TOOL_CALL,
                thought="run checks",
                tool_call=calls[0],
                tool_calls=calls,
            ),
            Action(ActionType.FINISH, "done", message="ok"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry, AgentConfig(budget_tokens=80_000))

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)
            events = log.replay()

        assert result.is_success()
        reflections = [e for e in events if e.event_type == EventType.REFLECTION]
        assert len(reflections) == 1
        observations = [e for e in events if e.event_type == EventType.OBSERVATION]
        assert len(observations) == 2

    def test_repo_map_injected_in_system_prompt(self, tmp_path):
        """repo_map 生成的内容应出现在发给 LLM 的 system prompt 里。"""
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog

        # 在 repo 里放一个 Python 文件，让 repo_map 有内容
        (tmp_path / "mymodule.py").write_text("def my_function():\n    pass\n")

        task = self._make_task(tmp_path)
        registry = ToolRegistry().register(NoopTool("shell"))
        script = [Action(ActionType.FINISH, "done", message="ok")]
        backend = MockBackend(script)
        config = AgentConfig(budget_tokens=80_000)
        agent = Agent(backend, registry, config)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            agent.run(task, log)

        # 检查 LLM 收到的第一条 system 消息里有 repo 内容
        assert backend.call_count >= 1
        first_messages = backend.received_messages[0]
        system_content = next(
            (m.content for m in first_messages if m.role == "system"), ""
        )
        assert "mymodule.py" in system_content or "my_function" in system_content

    def test_large_history_trimmed(self, tmp_path):
        """历史很长时，TokenBudget 应裁剪而不是崩溃。"""
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog

        task = self._make_task(tmp_path)
        registry = ToolRegistry().register(NoopTool("shell"))

        # 造 40 步 tool_call，每步 observation 很长
        class BigOutputTool(NoopTool):
            def execute(self, params):
                from tools.base import ToolResult
                return ToolResult(success=True, output="x" * 2000)

        registry2 = ToolRegistry().register(BigOutputTool("shell"))
        script = [
            Action(ActionType.TOOL_CALL, f"step {i}", ToolCall("shell", {"cmd": f"echo {i}"}))
            for i in range(4)
        ] + [Action(ActionType.FINISH, "done", message="ok")]

        backend = MockBackend(script)
        config = AgentConfig(budget_tokens=10_000, history_max_messages=10)
        agent = Agent(backend, registry2, config)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()
