"""
tools/test_tool.py

pytest 执行工具，返回结构化的测试结果。

关键设计：
- 不只返回原始 stdout，而是解析出"哪些测试失败了 + 错误信息"
- 失败时 output 包含精简的 failure summary，避免把整个 traceback 塞进上下文
- 通过 exit code 判断成功/失败，不依赖字符串匹配
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolResult
from tools.path_guard import WorkspaceBoundary
from tools.runtime import LocalRuntime, Runtime


PYTEST_TIMEOUT = 120        # pytest 默认超时，比 shell 工具更长
MAX_OUTPUT_CHARS = 6_000    # 测试输出比普通 shell 输出更容易很长


class PytestTool(BaseTool):
    """
    运行 pytest 并返回结构化结果。

    params:
        path (str):  测试文件或目录（默认 "tests/"，不存在则用 "."）
        args (str):  额外的 pytest 参数（如 "-x -v --tb=short"）
        cwd (str):   工作目录（默认当前目录）
    """

    def __init__(
        self,
        runtime: Runtime | None = None,
        boundary: WorkspaceBoundary | None = None,
    ) -> None:
        self._boundary = boundary
        self._runtime = runtime or LocalRuntime(boundary=boundary)

    @property
    def name(self) -> str:
        return "test"

    @property
    def description(self) -> str:
        return (
            "Run pytest and return a structured summary of results. "
            "Shows which tests failed and their error messages. "
            "Use path to run specific test files or directories."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Test file or directory to run (default: 'tests/' or '.')",
                },
                "args": {
                    "type": "string",
                    "description": "Extra pytest arguments (e.g. '-x -v --tb=short')",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory",
                },
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cwd = params.get("cwd", None)
        if self._boundary is not None:
            cwd_check = self._boundary.resolve(cwd or ".", operation="run pytest in cwd")
            if not cwd_check.success:
                return ToolResult(success=False, output="", error=cwd_check.error)
            cwd_path = cwd_check.path or self._boundary.root
            cwd = str(cwd_path)
        else:
            cwd_path = Path(cwd) if cwd else Path.cwd()

        # 决定测试路径
        test_path = params.get("path", "")
        if not test_path:
            if (cwd_path / "tests").exists():
                test_path = "tests/"
            else:
                test_path = "."
        elif self._boundary is not None:
            path_check = self._boundary.resolve(
                test_path,
                operation="run pytest path",
                base=cwd_path,
            )
            if not path_check.success:
                return ToolResult(success=False, output="", error=path_check.error)
            test_path = _format_path_for_cwd(path_check.path or Path(test_path), cwd_path)

        extra_args = params.get("args", "")
        try:
            extra_parts = shlex.split(extra_args) if extra_args else []
        except ValueError as e:
            return ToolResult(success=False, output="", error=f"Invalid pytest args: {e}")

        # 组装命令：--tb=short 足够 agent 理解，--no-header 减少噪音。
        # 本地 runtime 使用当前解释器，避免依赖 PATH 中存在 "python"。
        python_bin = sys.executable if isinstance(self._runtime, LocalRuntime) else "python"
        cmd_parts = [
            python_bin, "-m", "pytest",
            test_path,
            "--tb=short",
            "--no-header",
            "-q",               # 安静模式：只输出失败详情和最终统计
        ]
        cmd_parts.extend(extra_parts)

        cmd_str = shlex.join(cmd_parts)
        run_result = self._runtime.exec(cmd_str, cwd=cwd, timeout=PYTEST_TIMEOUT)
        if "timed out" in run_result.stderr.lower():
            return ToolResult(
                success=False,
                output="",
                error=f"pytest timed out after {PYTEST_TIMEOUT}s",
            )
        raw = run_result.output
        success = run_result.returncode == 0

        # 解析并格式化输出
        output = _format_pytest_output(raw, success)

        return ToolResult(
            success=success,
            output=output,
            error=None if success else f"pytest exited with code {run_result.returncode}",
        )


def _format_path_for_cwd(path: Path, cwd_path: Path) -> str:
    """Use a cwd-relative path when possible."""
    try:
        rel = path.resolve(strict=False).relative_to(cwd_path.resolve(strict=False))
    except ValueError:
        return str(path)
    return "." if str(rel) == "." else rel.as_posix()


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------

def _format_pytest_output(raw: str, success: bool) -> str:
    """
    把 pytest 原始输出格式化为 agent 友好的摘要。

    成功时：返回通过统计行（如 "5 passed in 0.12s"）
    失败时：提取 FAILED 测试列表 + 每个失败的 short traceback
    """
    if len(raw) > MAX_OUTPUT_CHARS:
        # 失败时 agent 最需要看尾部（错误摘要），头部（收集信息）不重要
        raw = "...[output truncated]...\n" + raw[-MAX_OUTPUT_CHARS:]

    if success:
        # 只返回最后的统计行
        lines = raw.strip().splitlines()
        summary_lines = [l for l in lines if re.search(r"passed|no tests", l)]
        if summary_lines:
            return summary_lines[-1]
        return raw.strip()

    # 失败时：提取 FAILED 列表
    failed_lines = [l for l in raw.splitlines() if l.startswith("FAILED")]
    failed_section = "\n".join(failed_lines) if failed_lines else ""

    # 提取 short test summary info 之后的内容（pytest -q 会输出这块）
    short_summary_match = re.search(
        r"=+ short test summary info =+(.*?)(?:=+|\Z)",
        raw,
        re.DOTALL,
    )
    short_summary = short_summary_match.group(1).strip() if short_summary_match else ""

    # 最终统计行（如 "2 failed, 3 passed in 0.45s"）
    stat_match = re.search(r"\d+ (failed|error).*in \d+\.\d+s", raw)
    stat_line = stat_match.group(0) if stat_match else ""

    parts = []
    if failed_section:
        parts.append(f"Failed tests:\n{failed_section}")
    if short_summary and short_summary != failed_section:
        parts.append(f"Summary:\n{short_summary}")
    if stat_line:
        parts.append(stat_line)

    return "\n\n".join(parts) if parts else raw.strip()
