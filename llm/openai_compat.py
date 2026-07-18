"""
llm/openai_compat.py

OpenAI-compatible backend。覆盖：
- OpenAI (api.openai.com)
- DeepSeek (api.deepseek.com)
- Groq (api.groq.com)
- Ollama (localhost:11434/v1)

全部用 openai SDK，切换只改 base_url + api_key。
OpenAI-compatible 后端统一走原生 tool/function calling；DeepSeek V4 模型
会额外启用 thinking/reasoning 参数。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.task import Action, ActionType, ToolCall
from llm.base import (
    LLMBackend,
    LLMContentFilteredError,
    LLMMessage,
    LLMModelBehaviorError,
    LLMOutputTruncatedError,
    LLMProviderProtocolError,
    LLMResponse,
    LLMToolSchema,
)

logger = logging.getLogger(__name__)

_DEEPSEEK_V4_MODELS: tuple[str, ...] = (
    "deepseek-v4-pro",
    "deepseek-v4-flash",
)


class OpenAICompatBackend(LLMBackend):
    """
    OpenAI-compatible API backend。

    Args:
        model:    模型名，如 "gpt-4o", "deepseek-chat", "llama3-70b-8192"
        api_key:  API key
        base_url: API base URL，None 时用 OpenAI 官方地址
        max_tokens: 最大输出 token 数
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=api_key, base_url=base_url)
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")

        self._model = model
        self._max_tokens = max_tokens
        self._use_deepseek_thinking = model.lower() in _DEEPSEEK_V4_MODELS

    @property
    def model_name(self) -> str:
        return self._model

    def complete(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        api_messages = _to_openai_messages(messages)

        logger.debug(
            "OpenAI-compat request: model=%s messages=%d tools=%d fc=%s",
            self._model, len(api_messages), len(tools),
        )

        return self._complete_with_tools(api_messages, tools)

    # ------------------------------------------------------------------
    # function calling 路径
    # ------------------------------------------------------------------

    def _complete_with_tools(
        self,
        api_messages: list[dict],
        tools: list[LLMToolSchema],
    ) -> LLMResponse:
        api_tools = [_to_openai_tool(t) for t in tools]

        kwargs = self._chat_completion_kwargs(
            model=self._model,
            max_tokens=self._max_tokens,# 限制模型生成的最大 token 数量，防止回复过长或失控
            messages=api_messages,
            tools=api_tools,
            tool_choice="auto",
        )
        response = self._client.chat.completions.create(**kwargs)

        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens

        if not response.choices:
            raise LLMProviderProtocolError(
                "Response contains no choices",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        choice = response.choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        reasoning = getattr(message, "reasoning_content", None)
        raw_content = content if isinstance(content, str) and content else ""
        if not raw_content and isinstance(reasoning, str): # 推理内容的优雅降级（如果模型没有返回常规的文本内容（not raw_content 为 True），但是返回了推理思考过程（isinstance(reasoning, str)），那么就将思考过程作为最终的文本内容）
            raw_content = reasoning

        logger.debug(
            "OpenAI-compat response: finish_reason=%s input=%d output=%d",
            choice.finish_reason,
            input_tokens,
            output_tokens,
        )

        action = _parse_openai_response(
            choice,
            fallback_reasoning=reasoning if isinstance(reasoning, str) else "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        return LLMResponse(
            action=action,
            raw_content=raw_content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            assistant_message=_assistant_message_from_choice(choice),
        )

    def _chat_completion_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        """Apply provider-specific request options without affecting other providers."""
        if self._use_deepseek_thinking:
            kwargs["reasoning_effort"] = "high"
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body["thinking"] = {"type": "enabled"}
            kwargs["extra_body"] = extra_body
        return kwargs


# ---------------------------------------------------------------------------
# 格式转换
# ---------------------------------------------------------------------------

def _to_openai_messages(messages: list[LLMMessage]) -> list[dict]:
    """把 LLMMessage 列表转为 OpenAI messages 格式。"""
    result = []
    for msg in messages:
        if msg.tool_call_id:
            result.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": msg.content,
            })
        elif msg.role == "assistant" and (msg.tool_calls or msg.reasoning_content):
            item = {"role": "assistant", "content": msg.content}
            if msg.reasoning_content is not None:
                item["reasoning_content"] = msg.reasoning_content
            if msg.tool_calls is not None:
                item["tool_calls"] = msg.tool_calls
            result.append(item)
        else:
            result.append({"role": msg.role, "content": msg.content})
    return result


def _to_openai_tool(schema: LLMToolSchema) -> dict:
    """转换为 OpenAI tool schema 格式。"""
    return {
        "type": "function",
        "function": {
            "name": schema.name,
            "description": schema.description,
            "parameters": schema.parameters,
        },
    }


def _get_attr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _tool_function_parts(tool_call: Any) -> tuple[str, str]:
    function = _get_attr_or_key(tool_call, "function")
    name = _get_attr_or_key(function, "name", "") if function is not None else ""
    arguments = (
        _get_attr_or_key(function, "arguments", "{}")
        if function is not None
        else "{}"
    )
    return str(name or ""), str(arguments or "{}")


def _tool_calls_to_dicts(tool_calls: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not tool_calls:
        return result
    for tool_call in tool_calls:
        name, arguments = _tool_function_parts(tool_call)
        result.append({
            "id": _get_attr_or_key(tool_call, "id"),
            "type": _get_attr_or_key(tool_call, "type", "function") or "function",
            "function": {
                "name": name,
                "arguments": arguments,
            },
        })
    return result


def _assistant_message_from_choice(choice: Any) -> LLMMessage | None:
    message = getattr(choice, "message", None)
    if message is None:
        return None

    content = getattr(message, "content", None)
    reasoning = getattr(message, "reasoning_content", None)
    tool_calls = _tool_calls_to_dicts(getattr(message, "tool_calls", None))

    return LLMMessage(
        role="assistant",
        content=content if isinstance(content, str) else "",
        reasoning_content=reasoning if isinstance(reasoning, str) else None,
        tool_calls=tool_calls or None,
    )


def _parse_openai_response(
    choice: Any,
    fallback_reasoning: str = "",
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> Action:
    """将 OpenAI-compatible API 的 choice 转换为内部 Action。"""
    finish_reason = getattr(choice, "finish_reason", None)
    message = getattr(choice, "message", None)
    error_kwargs = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }

    if message is None:
        raise LLMProviderProtocolError(
            "Response choice contains no message",
            **error_kwargs,
        )

    content = getattr(message, "content", None)
    content = content.strip() if isinstance(content, str) else ""

    reasoning = getattr(message, "reasoning_content", None)
    reasoning = reasoning.strip() if isinstance(reasoning, str) else ""
    if not reasoning:
        reasoning = (
            fallback_reasoning.strip()
            if isinstance(fallback_reasoning, str)
            else ""
        )

    tool_calls = getattr(message, "tool_calls", None)

    if finish_reason == "tool_calls":
        if not tool_calls:
            raise LLMModelBehaviorError(
                "finish_reason was 'tool_calls', but no tool calls were provided",
                **error_kwargs,
            )

        parsed_calls: list[ToolCall] = []
        for tc in tool_calls:
            tool_name, arguments = _tool_function_parts(tc)
            try:
                params = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise LLMModelBehaviorError(
                    f"Invalid tool arguments JSON: {exc}",
                    **error_kwargs,
                )
            if not isinstance(params, dict):
                raise LLMModelBehaviorError(
                    "Tool arguments must be a JSON object",
                    **error_kwargs,
                )

            parsed_calls.append(ToolCall(
                name=tool_name,
                params=params,
                id=_get_attr_or_key(tc, "id"),
            ))

        return Action(
            action_type=ActionType.TOOL_CALL,
            thought=reasoning,
            tool_call=parsed_calls[0],
            tool_calls=parsed_calls,
        )

    if finish_reason == "stop":
        if content:
            return Action(
                action_type=ActionType.FINISH,
                thought=reasoning,
                message=content,
            )
        raise LLMModelBehaviorError(
            "Model stopped with no final content",
            **error_kwargs,
        )

    if finish_reason == "length":
        raise LLMOutputTruncatedError(
            "Model output was truncated by the token limit",
            **error_kwargs,
        )

    if finish_reason == "content_filter":
        raise LLMContentFilteredError(
            "Model output was blocked by the content filter",
            **error_kwargs,
        )

    raise LLMProviderProtocolError(
        f"Unsupported finish_reason: {finish_reason!r}",
        **error_kwargs,
    )


# ---------------------------------------------------------------------------
# 流式支持
# ---------------------------------------------------------------------------

from llm.base import StreamCallback


def _openai_stream(
    self: "OpenAICompatBackend",
    messages: list,
    tools: list,
    on_text: StreamCallback | None = None,
    on_thought: StreamCallback | None = None,
) -> "LLMResponse":
    """
    OpenAI-compatible 流式调用实现。
    on_text:    最终回答（message）的流式回调
    on_thought: 推理过程（reasoning_content）的流式回调，仅推理模型有内容
    """
    api_messages = _to_openai_messages(messages)

    return _stream_with_tools(self, api_messages, tools, on_text, on_thought)


def _stream_with_tools(self, api_messages, tools, on_text, on_thought=None):
    api_tools = [_to_openai_tool(t) for t in tools] if tools else None

    kwargs = self._chat_completion_kwargs(
        model=self._model,
        max_tokens=self._max_tokens,
        messages=api_messages,
        stream=True,
    )
    if api_tools:
        kwargs["tools"] = api_tools
        kwargs["tool_choice"] = "auto"

    # 收集流式 chunks
    full_text = ""
    full_reasoning = ""  # reasoning_content（推理模型专有）
    finish_reason = None
    tool_calls_raw = []      # 收集 tool call deltas

    stream = self._client.chat.completions.create(**kwargs)
    for chunk in stream:
        choice = chunk.choices[0] if chunk.choices else None
        if not choice:
            continue

        delta = choice.delta
        finish_reason = choice.finish_reason or finish_reason

        # reasoning_content delta（DeepSeek/兼容推理模型）
        reasoning_delta = getattr(delta, "reasoning_content", None)
        if reasoning_delta:
            full_reasoning += reasoning_delta
            if on_thought:
                on_thought(reasoning_delta)

        # text delta（最终回答）
        content_delta = getattr(delta, "content", None)
        if content_delta:
            full_text += content_delta
            if on_text:
                on_text(content_delta)

        # tool call delta 拼接
        delta_tool_calls = getattr(delta, "tool_calls", None)
        if delta_tool_calls:
            for tc_delta in delta_tool_calls:
                idx = _get_attr_or_key(tc_delta, "index", 0)
                while len(tool_calls_raw) <= idx:
                    tool_calls_raw.append({
                        "id": None,
                        "type": "function",
                        "name": "",
                        "arguments": "",
                    })
                tool_call_id = _get_attr_or_key(tc_delta, "id")
                if tool_call_id:
                    tool_calls_raw[idx]["id"] = tool_call_id
                tool_call_type = _get_attr_or_key(tc_delta, "type")
                if tool_call_type:
                    tool_calls_raw[idx]["type"] = tool_call_type
                function = _get_attr_or_key(tc_delta, "function")
                name_delta = _get_attr_or_key(function, "name", "") if function is not None else ""
                args_delta = _get_attr_or_key(function, "arguments", "") if function is not None else ""
                if name_delta:
                    tool_calls_raw[idx]["name"] += name_delta
                if args_delta:
                    tool_calls_raw[idx]["arguments"] += args_delta

    # 构造 mock choice 供 _parse_openai_response 复用
    from types import SimpleNamespace

    if tool_calls_raw and finish_reason == "tool_calls":
        tcs = []
        for tc in tool_calls_raw:
            fn = SimpleNamespace(name=tc["name"], arguments=tc["arguments"])
            tcs.append(SimpleNamespace(
                id=tc["id"],
                type=tc["type"],
                function=fn,
            ))
        mock_message = SimpleNamespace(
            content=full_text or None,
            reasoning_content=full_reasoning or None,
            tool_calls=tcs,
        )
    else:
        mock_message = SimpleNamespace(
            content=full_text or None,
            reasoning_content=full_reasoning or None,
            tool_calls=None,
        )

    # 流式模式拿不到精确 token 数，估算
    from context.token_budget import estimate_tokens
    input_tokens = sum(estimate_tokens(m.get("content", "")) for m in api_messages)
    output_tokens = estimate_tokens(full_text + full_reasoning)
    raw_content = full_text or full_reasoning

    mock_choice = SimpleNamespace(
        finish_reason=finish_reason or "stop",
        message=mock_message,
    )
    action = _parse_openai_response(
        mock_choice,
        fallback_reasoning=full_reasoning,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    return LLMResponse(
        action=action,
        raw_content=raw_content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        assistant_message=_assistant_message_from_choice(mock_choice),
    )

# 把 stream() 方法绑定到 OpenAICompatBackend
OpenAICompatBackend.stream = _openai_stream
