"""
tools/base.py

工具层基础设施：
- ToolResult     工具执行结果
- BaseTool       所有工具的抽象基类
- ToolRegistry   工具注册表，core.py 通过它执行工具、生成 schema

新增工具只需：
    1. 继承 BaseTool，实现 execute() 和 schema 属性
    2. 调用 registry.register(MyTool())
    不需要改任何其他代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from agent.task import Observation, ObservationStatus
from llm.base import LLMToolSchema


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """
    工具执行的原始结果，由各 Tool.execute() 返回。
    core.py 把它转换为 Observation 后写入 EventLog。
    """
    success: bool
    output: str                         # 工具的文本输出，已做截断处理
    error: str | None = None            # 失败时的错误信息

    def to_observation(self, tool_name: str) -> Observation:
        """转换为 Observation，供 core.py 写入 EventLog 和注入上下文。"""
        return Observation(
            status=ObservationStatus.SUCCESS if self.success else ObservationStatus.ERROR,
            output=self.output,
            tool_name=tool_name,
            error=self.error,
        )


# ---------------------------------------------------------------------------
# BaseTool
# ---------------------------------------------------------------------------

class BaseTool(ABC):
    """
    所有工具的抽象基类。

    子类必须实现：
    - name:     工具名称（与 LLM function calling 的函数名对应）
    - schema:   JSON Schema 描述，告诉 LLM 这个工具怎么用
    - execute(): 实际执行逻辑
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，如 "shell", "file_read"。必须全局唯一。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具功能描述，注入 LLM 的 system prompt 和 tool schema。"""
        ...

    @property
    @abstractmethod
    def parameters_schema(self) -> dict[str, Any]:
        """
        参数的 JSON Schema。示例：
        {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to run"},
            },
            "required": ["cmd"],
        }
        """
        ...

    @abstractmethod
    def execute(self, params: dict[str, Any]) -> ToolResult:
        """执行工具，返回 ToolResult。不抛异常，错误封装在 ToolResult.error 里。"""
        ...

    def to_llm_schema(self) -> LLMToolSchema:
        """生成供 LLM 使用的 schema，由 ToolRegistry 调用。"""
        return LLMToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.parameters_schema,
        )


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """
    工具注册表。core.py 持有一个 registry 实例，通过它：
    1. 查找工具并执行（execute_tool）
    2. 生成所有工具的 schema 列表注入 LLM（get_schemas）

    线程安全：当前 v1 单线程，不加锁。
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> "ToolRegistry":
        """
        注册一个工具。支持链式调用：
            registry.register(ShellTool()).register(FileTool())
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool
        return self

    def execute_tool(self, name: str, params: Any) -> ToolResult:
        """
        按名称查找工具并执行。
        工具不存在时返回 error ToolResult（不抛异常，让 agent 继续运行）。
        """
        if name not in self._tools:
            available = ", ".join(self._tools.keys()) or "none"
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown tool '{name}'. Available tools: {available}",
            )

        tool = self._tools[name]
        validation_error = _validate_params(tool.parameters_schema, params)
        if validation_error:
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid params for {name}: {validation_error}",
            )

        try:
            return tool.execute(params)
        except Exception as exc:
            # 工具内部未捕获的异常，降级为 error 结果
            return ToolResult(
                success=False,
                output="",
                error=f"Tool '{name}' raised an unexpected error: {exc}",
            )

    def get_schemas(self) -> list[LLMToolSchema]:
        """返回所有已注册工具的 schema，供注入 LLM。"""
        return [tool.to_llm_schema() for tool in self._tools.values()]

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry(tools={self.tool_names})"


# ---------------------------------------------------------------------------
# 参数 schema 校验
# ---------------------------------------------------------------------------

def _validate_params(schema: dict[str, Any], params: Any) -> str | None:
    """
    校验 LLM 返回的工具参数。

    只实现项目当前工具 schema 使用到的 JSON Schema 子集：
    object / string / integer / boolean / array / required / properties / items。
    返回 None 表示通过，返回字符串表示错误原因。
    """
    return _validate_value(schema, params, path="params")


def _validate_value(schema: dict[str, Any], value: Any, path: str) -> str | None:
    expected_type = schema.get("type")

    if expected_type == "object":
        if not isinstance(value, dict):
            return f"{path} must be object"

        properties = schema.get("properties", {})
        allow_extra = schema.get("additionalProperties") is True

        if not allow_extra:
            for key in value:
                if key not in properties:
                    return f"unknown property '{key}'"

        for key in schema.get("required", []):
            if key not in value:
                return f"missing required property '{key}'"

        for key, child_schema in properties.items():
            if key not in value:
                continue
            error = _validate_value(child_schema, value[key], path=key)
            if error:
                return error
        return None

    if expected_type == "string":
        if not isinstance(value, str):
            return f"{path} must be string"
        return None

    if expected_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{path} must be integer"
        return None

    if expected_type == "boolean":
        if not isinstance(value, bool):
            return f"{path} must be boolean"
        return None

    if expected_type == "array":
        if not isinstance(value, list):
            return f"{path} must be array"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                error = _validate_value(item_schema, item, path=f"{path}[{index}]")
                if error:
                    return error
        return None

    # 未使用到的 schema 关键字不在 v1 中强行解释，避免误伤未来扩展。
    return None


# ---------------------------------------------------------------------------
# NoopTool — 测试辅助
# ---------------------------------------------------------------------------

class NoopTool(BaseTool):
    """
    测试专用工具，execute() 直接返回成功，不做任何实际操作。
    用于在不依赖真实文件系统/shell 的情况下测试 core.py 流程。
    """

    def __init__(self, tool_name: str = "noop", output: str = "ok") -> None:
        self._name = tool_name
        self._output = output
        self.call_count = 0
        self.last_params: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"No-op tool '{self._name}' for testing."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Anything"},
            },
            "required": [],
            "additionalProperties": True,
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        self.call_count += 1
        self.last_params = params
        return ToolResult(success=True, output=self._output)


class FailingTool(BaseTool):
    """
    测试专用工具，execute() 始终返回失败。
    用于测试 Reflection 触发（测试失败路径）。
    """

    def __init__(self, tool_name: str = "test", error_msg: str = "AssertionError: 1 != 2") -> None:
        self._name = tool_name
        self._error_msg = error_msg
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Always-failing tool '{self._name}' for testing."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        self.call_count += 1
        return ToolResult(
            success=False,
            output=self._error_msg,
            error=self._error_msg,
        )
