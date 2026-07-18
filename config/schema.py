"""
config/schema.py

配置文件加载与校验。把 config/default.yaml 解析成类型安全的 dataclass。

支持：
- 环境变量展开：${VAR} 语法
- 多层配置合并：default.yaml < 用户指定 yaml < CLI 参数
- 缺失必填项时给出清晰错误信息
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# 配置 dataclass
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-pro"
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 4096


@dataclass
class AgentCfg:
    max_steps: int = 40
    budget_tokens: int = 80_000
    log_dir: str = "./logs"


@dataclass
class ShellToolConfig:
    timeout: int = 30
    max_output_tokens: int = 8_000


@dataclass
class FileToolConfig:
    max_view_lines: int = 100


@dataclass
class ToolsConfig:
    shell: ShellToolConfig = field(default_factory=ShellToolConfig)
    file: FileToolConfig = field(default_factory=FileToolConfig)


@dataclass
class ContextConfig:
    repo_map_budget: int = 8_000
    history_window: int = 20


@dataclass
class LangfuseConfig:
    enabled: bool = False
    base_url: str = ""
    public_key: str = ""
    secret_key: str = ""
    trace_content: str = "full"
    flush_on_exit: bool = True
    debug: bool = False


@dataclass
class ObservabilityConfig:
    langfuse: LangfuseConfig = field(default_factory=LangfuseConfig)


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentCfg = field(default_factory=AgentCfg)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)


# ---------------------------------------------------------------------------
# 加载函数
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{(\w+)\}")
_ENV_LINE_RE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")


def _dotenv_candidates(config_path: Path | None = None) -> list[Path]:
    candidates = [Path.cwd() / ".env"]
    if config_path is not None:
        candidates.append(config_path.parent / ".env")
    candidates.append(Path(__file__).parent.parent / ".env")

    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _parse_dotenv_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        end = value.find(quote, 1)
        if end == -1:
            inner = value[1:]
        else:
            inner = value[1:end]
        if quote == '"':
            inner = (
                inner
                .replace(r"\n", "\n")
                .replace(r"\r", "\r")
                .replace(r"\t", "\t")
                .replace(r'\"', '"')
                .replace(r"\\", "\\")
            )
        return inner
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _load_dotenv(config_path: Path | None = None) -> None:
    for env_path in _dotenv_candidates(config_path):
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _ENV_LINE_RE.fullmatch(stripped)
            if not match:
                continue
            key, raw_value = match.groups()
            if key not in os.environ:
                os.environ[key] = _parse_dotenv_value(raw_value)


def _expand_env(text: str) -> str:
    """展开 ${VAR} 形式的环境变量占位符。"""
    def replace(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")
    return _ENV_RE.sub(replace, text)


def _parse_bool(value: Any, default: bool = False) -> bool:
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


def load_config(path: str | Path | None = None) -> AppConfig:
    """
    加载配置文件，返回 AppConfig。

    Args:
        path: YAML 文件路径，None 时自动查找 config/default.yaml

    Returns:
        AppConfig 实例
    """
    config_path: Path | None = Path(path) if path is not None else None
    _load_dotenv(config_path)

    if path is None:
        # 自动查找：当前目录 → 项目根目录
        candidates = [
            Path("config/default.yaml"),
            Path(__file__).parent / "default.yaml",
        ]
        for p in candidates:
            if p.exists():
                path = p
                break
        else:
            return AppConfig()   # 找不到配置文件，用全默认值

    config_path = Path(path)
    if not config_path.exists():
        return AppConfig()
    raw = config_path.read_text(encoding="utf-8")
    raw = _expand_env(raw)
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    return _parse(data)


def _parse(data: dict[str, Any]) -> AppConfig:
    """把 yaml dict 解析为 AppConfig。"""
    llm_raw = data.get("llm", {})
    agent_raw = data.get("agent", {})
    tools_raw = data.get("tools", {})
    context_raw = data.get("context", {})
    observability_raw = data.get("observability", {})

    llm = LLMConfig(
        provider=llm_raw.get("provider", "deepseek"),
        model=llm_raw.get("model", "deepseek-v4-pro"),
        api_key=llm_raw.get("api_key", ""),
        base_url=llm_raw.get("base_url", "") or "",
        max_tokens=int(llm_raw.get("max_tokens", 4096)),
    )

    agent = AgentCfg(
        max_steps=int(agent_raw.get("max_steps", 40)),
        budget_tokens=int(agent_raw.get("budget_tokens", 80_000)),
        log_dir=agent_raw.get("log_dir", "./logs"),
    )

    shell_raw = tools_raw.get("shell", {})
    file_raw = tools_raw.get("file", {})
    tools = ToolsConfig(
        shell=ShellToolConfig(
            timeout=int(shell_raw.get("timeout", 30)),
            max_output_tokens=int(shell_raw.get("max_output_tokens", 8_000)),
        ),
        file=FileToolConfig(
            max_view_lines=int(file_raw.get("max_view_lines", 100)),
        ),
    )

    context = ContextConfig(
        repo_map_budget=int(context_raw.get("repo_map_budget", 8_000)),
        history_window=int(context_raw.get("history_window", 20)),
    )

    langfuse_raw = observability_raw.get("langfuse", {})
    observability = ObservabilityConfig(
        langfuse=LangfuseConfig(
            enabled=_parse_bool(langfuse_raw.get("enabled"), False),
            base_url=langfuse_raw.get("base_url", "") or "",
            public_key=langfuse_raw.get("public_key", "") or "",
            secret_key=langfuse_raw.get("secret_key", "") or "",
            trace_content=langfuse_raw.get("trace_content", "full") or "full",
            flush_on_exit=_parse_bool(langfuse_raw.get("flush_on_exit"), True),
            debug=_parse_bool(langfuse_raw.get("debug"), False),
        )
    )

    return AppConfig(
        llm=llm,
        agent=agent,
        tools=tools,
        context=context,
        observability=observability,
    )


def merge_cli_overrides(
    config: AppConfig,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_steps: int | None = None,
) -> AppConfig:
    """
    把 CLI 参数覆盖到已加载的 config 上。
    CLI 参数优先级最高。
    """
    if provider:
        config.llm.provider = provider
    if model:
        config.llm.model = model
    if api_key:
        config.llm.api_key = api_key
    if max_steps is not None:
        config.agent.max_steps = max_steps
    return config