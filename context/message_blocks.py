"""
Protocol-aware message grouping for LLM chat history.

Tool/function calling turns are protocol transactions, not independent chat
messages. A provider transcript must keep an assistant tool-call message
together with its matching tool result messages.
"""

from __future__ import annotations

from typing import Any


def build_message_blocks(messages: list[Any]) -> tuple[list[list[Any]], int]:
    """
    Split messages into protocol-safe blocks.

    Returns (blocks, dropped_count). Invalid tool-call fragments are dropped:
    orphan tool messages, assistant tool_calls without all matching tool
    results, and tool results with unknown ids.
    """
    blocks: list[list[Any]] = []
    dropped = 0
    i = 0
    total = len(messages)

    while i < total:
        message = messages[i]
        role = _message_role(message)

        if role == "tool":
            dropped += 1
            i += 1
            continue

        tool_calls = _message_tool_calls(message)
        tool_call_ids = _tool_call_ids(tool_calls)
        if role == "assistant" and tool_calls:
            if not tool_call_ids:
                dropped += 1
                i += 1
                continue
            block, next_index = _consume_tool_transaction(messages, i, tool_call_ids)
            if block is None:
                dropped += max(1, next_index - i)
            else:
                blocks.append(block)
            i = next_index
            continue

        blocks.append([message])
        i += 1

    return blocks, dropped


def flatten_blocks(blocks: list[list[Any]]) -> list[Any]:
    """Flatten message blocks back into a message list."""
    flattened: list[Any] = []
    for block in blocks:
        flattened.extend(block)
    return flattened


def _consume_tool_transaction(
    messages: list[Any],
    start: int,
    tool_call_ids: list[str],
) -> tuple[list[Any] | None, int]:
    pending = set(tool_call_ids)
    block_messages = [messages[start]]
    i = start + 1

    while i < len(messages) and _message_role(messages[i]) == "tool":
        tool_call_id = _message_tool_call_id(messages[i])
        if tool_call_id not in pending:
            break
        pending.remove(tool_call_id)
        block_messages.append(messages[i])
        i += 1
        if not pending:
            return block_messages, i

    return None, i


def _message_role(message: Any) -> str | None:
    if isinstance(message, dict):
        role = message.get("role")
    else:
        role = getattr(message, "role", None)
    return str(role) if role is not None else None


def _message_tool_call_id(message: Any) -> str | None:
    if isinstance(message, dict):
        tool_call_id = message.get("tool_call_id")
    else:
        tool_call_id = getattr(message, "tool_call_id", None)
    return str(tool_call_id) if tool_call_id else None


def _message_tool_calls(message: Any) -> list[Any] | None:
    if isinstance(message, dict):
        return message.get("tool_calls")
    return getattr(message, "tool_calls", None)


def _tool_call_ids(tool_calls: list[Any] | None) -> list[str]:
    ids: list[str] = []
    for tool_call in tool_calls or []:
        if isinstance(tool_call, dict):
            tool_call_id = tool_call.get("id")
        else:
            tool_call_id = getattr(tool_call, "id", None)
        if tool_call_id:
            ids.append(str(tool_call_id))
    return ids
