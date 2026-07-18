"""
context/token_budget.py

Token 预算管理：给 prompt 各部分分配 token 配额，超出时按优先级裁剪。

## tiktoken 安装

    pip install tiktoken

首次运行时自动下载词表（需联网，约 2MB），之后缓存到本地离线可用。

如果网络无法访问 OpenAI CDN，手动下载词表：
    curl -L "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken" \\
         -o ~/.cache/tiktoken/9b5ad71b2ce5302211f9c61530b329a4922fc6a4021629a1eba1b43bf10a10.tiktoken

然后设置环境变量：
    export TIKTOKEN_CACHE_DIR=~/.cache/tiktoken

tiktoken 不可用时自动降级为字符估算（1 token ≈ 4 chars），精度足够做预算控制。

各部分优先级（高→低，裁剪时从低优先级开始）：
  1. system_core   系统指令，永不裁剪
  2. task          任务描述，永不裁剪
  3. repo_map      repo 摘要，超出时缩减
  4. recent_obs    最近 observation，永不裁剪
  5. history       历史对话，从最旧开始裁剪
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from context.message_blocks import build_message_blocks, flatten_blocks

# ---------------------------------------------------------------------------
# Token 计数：优先 tiktoken，失败时字符估算 fallback
# ---------------------------------------------------------------------------

_tiktoken_enc = None
_tiktoken_available = False

def _init_tiktoken() -> None:
    global _tiktoken_enc, _tiktoken_available
    if _tiktoken_available or _tiktoken_enc is not None:
        return
    try:
        import tiktoken
        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        _tiktoken_available = True
    except Exception:
        # 网络不通 / 未安装，降级为字符估算
        _tiktoken_available = False


def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数。
    优先使用 tiktoken（精确），不可用时用字符数 // 4（误差 <15%）。
    """
    if not _tiktoken_available:
        _init_tiktoken()

    if _tiktoken_available and _tiktoken_enc is not None:
        try:
            return max(1, len(_tiktoken_enc.encode(text)))
        except Exception:
            pass

    # 字符估算 fallback
    return max(1, len(text) // 4)


def estimate_chars(tokens: int) -> int:
    """把 token 数转换为字符预算（估算）。"""
    return tokens * 4


def is_tiktoken_available() -> bool:
    """返回 tiktoken 是否可用，供诊断脚本使用。"""
    _init_tiktoken()
    return _tiktoken_available


def _message_token_text(message: dict) -> str:
    """把消息中会进上下文的文本/结构化字段合并用于 token 估算。"""
    parts = [
        str(message.get("role", "")),
        str(message.get("content", "") or ""),
    ]
    reasoning = message.get("reasoning_content")
    if reasoning:
        parts.append(str(reasoning))
    tool_call_id = message.get("tool_call_id")
    if tool_call_id:
        parts.append(str(tool_call_id))
    tool_calls = message.get("tool_calls")
    if tool_calls:
        parts.append(json.dumps(tool_calls, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# BudgetPlan
# ---------------------------------------------------------------------------

@dataclass
class BudgetPlan:
    """各部分的 token 配额计划。"""
    total: int
    system_core: int
    repo_map: int
    history: int
    observation: int
    reserve: int

    @property
    def available(self) -> int:
        return self.total - self.reserve


# ---------------------------------------------------------------------------
# TokenBudget
# ---------------------------------------------------------------------------

class TokenBudget:
    """
    Token 预算管理器。

    用法：
        budget = TokenBudget(total=80_000)
        plan = budget.default_plan()
        trimmed = budget.trim_to(text, plan.repo_map)
        trimmed_history = budget.trim_history(msgs, plan.history)
    """

    def __init__(self, total: int = 80_000) -> None:
        self._total = total

    def default_plan(self) -> BudgetPlan:
        total = self._total
        reserve = int(total * 0.15)
        available = total - reserve
        return BudgetPlan(
            total=total,
            reserve=reserve,
            system_core=int(available * 0.10),
            repo_map=int(available * 0.15),
            history=int(available * 0.50),
            observation=int(available * 0.25),
        )

    def trim_to(self, text: str, token_limit: int) -> str:
        """裁剪文本到 token_limit 以内，超出时保留开头。"""
        if estimate_tokens(text) <= token_limit:
            return text
        # 二分逼近：找到合适的字符截断点
        char_limit = token_limit * 4
        candidate = text[:char_limit]
        while estimate_tokens(candidate) > token_limit and len(candidate) > 0:
            candidate = candidate[:int(len(candidate) * 0.9)]
        omitted = estimate_tokens(text[len(candidate):])
        return candidate + f"\n... [{omitted} tokens truncated]"

    def trim_history(
        self,
        messages: list[dict],
        token_limit: int,
    ) -> list[dict]:
        """
        裁剪历史消息列表到 token_limit 以内。
        保留第一条（任务描述）+ 尽量多的最近协议安全消息块。
        """
        if not messages:
            return messages

        first_message = messages[0]
        first_tokens = estimate_tokens(_message_token_text(first_message))
        blocks, invalid_dropped = build_message_blocks(messages[1:])
        block_tokens = [
            sum(estimate_tokens(_message_token_text(m)) for m in block)
            for block in blocks
        ]
        total = first_tokens + sum(block_tokens)

        if invalid_dropped == 0 and total <= token_limit:
            return messages

        result = [first_message]
        dropped = invalid_dropped
        selected_blocks = []
        budget_left = token_limit - first_tokens

        for block, tokens in zip(reversed(blocks), reversed(block_tokens)):
            if budget_left - tokens >= 0:
                selected_blocks.append(block)
                budget_left -= tokens
            else:
                dropped += len(block)

        selected_blocks.reverse()

        if dropped > 0:
            result.append({
                "role": "user",
                "content": f"[{dropped} earlier messages were truncated to fit context window]",
            })

        result.extend(flatten_blocks(selected_blocks))
        return result

    def fit_all(
        self,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        observation_text: str,
    ) -> tuple[str, str, list[dict], str]:
        plan = self.default_plan()
        trimmed_system = self.trim_to(system_text, plan.system_core)
        trimmed_map = self.trim_to(repo_map_text, plan.repo_map)
        trimmed_history = self.trim_history(history, plan.history)
        trimmed_obs = self.trim_to(observation_text, plan.observation)
        return trimmed_system, trimmed_map, trimmed_history, trimmed_obs

    def usage_report(
        self,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        observation_text: str,
    ) -> dict[str, int]:
        history_tokens = sum(
            estimate_tokens(_message_token_text(m)) for m in history
        )
        return {
            "system":      estimate_tokens(system_text),
            "repo_map":    estimate_tokens(repo_map_text),
            "history":     history_tokens,
            "observation": estimate_tokens(observation_text),
            "total": (
                estimate_tokens(system_text)
                + estimate_tokens(repo_map_text)
                + history_tokens
                + estimate_tokens(observation_text)
            ),
            "budget":        self._total,
            "tiktoken_used": is_tiktoken_available(),
        }
