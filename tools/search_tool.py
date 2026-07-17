"""
tools/search_tool.py

代码搜索工具，三个 action：
- search_text:   在文件内容中搜索字符串（grep 风格）
- find_files:    按文件名 pattern 查找文件
- find_symbol:   在 Python 文件中查找函数/类定义（不依赖 tree-sitter，用正则）

设计说明：
- 不依赖外部工具（grep 不一定存在），用 Python 原生实现
- find_symbol 用正则匹配 def/class，Day 5 接入 tree-sitter 后可替换
- 结果数量上限防止返回太多内容爆上下文
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolResult
from tools.path_guard import WorkspaceBoundary
from tools.security_policy import is_sensitive_path, should_skip_search_path


MAX_RESULTS = 50        # 单次搜索最多返回的结果数
MAX_LINE_LENGTH = 200   # 单行超长时截断显示

# 搜索时跳过的目录
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", "*.egg-info",
    "logs",
})


class SearchTextTool(BaseTool):
    """
    在 repo 文件中搜索文本，返回匹配行及其上下文。

    params:
        pattern (str):    搜索字符串（支持正则）
        path (str):       搜索范围（文件或目录，默认当前目录）
        file_pattern (str): 只搜索匹配的文件名（如 "*.py"，默认所有文件）
        case_sensitive (bool): 是否区分大小写（默认 True）
    """

    def __init__(self, boundary: WorkspaceBoundary | None = None) -> None:
        self._boundary = boundary

    @property
    def name(self) -> str:
        return "search_text"

    @property
    def description(self) -> str:
        return (
            "Search for a text pattern (regex supported) in files. "
            "Returns matching lines with file path and line number. "
            f"Returns at most {MAX_RESULTS} matches."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default: current directory)",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py'). Default: all files",
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case-sensitive search (default true)",
                },
            },
            "required": ["pattern"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        raw_pattern = params.get("pattern", "")
        raw_path = params.get("path", ".")
        search_path = Path(raw_path)
        if self._boundary is not None:
            check = self._boundary.resolve(raw_path, operation="search files")
            if not check.success:
                return ToolResult(success=False, output="", error=check.error)
            search_path = check.path or search_path

        file_pattern = params.get("file_pattern", "*")
        case_sensitive = params.get("case_sensitive", True)

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(raw_pattern, flags)
        except re.error as e:
            return ToolResult(success=False, output="", error=f"Invalid regex: {e}")

        if not search_path.exists():
            return ToolResult(
                success=False, output="", error=f"Path not found: {search_path}"
            )

        matches: list[str] = []
        files = _iter_files(
            search_path,
            file_pattern,
            boundary=self._boundary,
            skip_outside=self._boundary.is_inside(search_path) if self._boundary else False,
        )

        for filepath in files:
            if should_skip_search_path(filepath, repo_root=self._boundary.root if self._boundary else None):
                continue
            if len(matches) >= MAX_RESULTS:
                break
            try:
                for lineno, line in enumerate(
                    filepath.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if regex.search(line):
                        display_line = line[:MAX_LINE_LENGTH]
                        if len(line) > MAX_LINE_LENGTH:
                            display_line += " ..."
                        matches.append(f"{filepath}:{lineno}: {display_line}")
                        if len(matches) >= MAX_RESULTS:
                            break
            except OSError:
                continue

        if not matches:
            return ToolResult(
                success=True,
                output=f"No matches found for '{raw_pattern}'",
            )

        suffix = f"\n[Showing {len(matches)} matches]"
        if len(matches) == MAX_RESULTS:
            suffix = f"\n[Showing first {MAX_RESULTS} matches, there may be more]"

        return ToolResult(success=True, output="\n".join(matches) + suffix)


class FindFilesTool(BaseTool):
    """
    按文件名 pattern 查找文件。

    params:
        pattern (str): glob 风格的文件名 pattern（如 "*.py", "test_*.py"）
        path (str):    搜索根目录（默认当前目录）
    """

    def __init__(self, boundary: WorkspaceBoundary | None = None) -> None:
        self._boundary = boundary

    @property
    def name(self) -> str:
        return "find_files"

    @property
    def description(self) -> str:
        return (
            "Find files by name pattern (glob style). "
            "Example: pattern='test_*.py' finds all test files. "
            f"Returns at most {MAX_RESULTS} results."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern for file names (e.g. '*.py', 'conftest.py')",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default: current directory)",
                },
            },
            "required": ["pattern"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        pattern = params.get("pattern", "")
        raw_path = params.get("path", ".")
        search_path = Path(raw_path)
        if self._boundary is not None:
            check = self._boundary.resolve(raw_path, operation="find files")
            if not check.success:
                return ToolResult(success=False, output="", error=check.error)
            search_path = check.path or search_path

        if not search_path.exists():
            return ToolResult(
                success=False, output="", error=f"Path not found: {search_path}"
            )

        results: list[str] = []
        for filepath in _iter_files(
            search_path,
            pattern,
            boundary=self._boundary,
            skip_outside=self._boundary.is_inside(search_path) if self._boundary else False,
        ):
            if should_skip_search_path(filepath, repo_root=self._boundary.root if self._boundary else None):
                continue
            results.append(str(filepath))
            if len(results) >= MAX_RESULTS:
                break

        if not results:
            return ToolResult(
                success=True,
                output=f"No files found matching '{pattern}' in {search_path}",
            )

        suffix = ""
        if len(results) == MAX_RESULTS:
            suffix = f"\n[Showing first {MAX_RESULTS} results]"

        return ToolResult(
            success=True,
            output="\n".join(results) + suffix,
        )


class FindSymbolTool(BaseTool):
    """
    在 Python 文件中查找函数/类定义。
    用正则匹配 def / class 语句，Day 5 可替换为 tree-sitter 精确实现。

    params:
        symbol (str): 函数名或类名（支持部分匹配）
        path (str):   搜索根目录（默认当前目录）
    """

    def __init__(self, boundary: WorkspaceBoundary | None = None) -> None:
        self._boundary = boundary

    @property
    def name(self) -> str:
        return "find_symbol"

    @property
    def description(self) -> str:
        return (
            "Find function or class definitions in Python files. "
            "Searches for 'def symbol' or 'class symbol' patterns. "
            "Supports partial name matching."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Function or class name to find (partial match supported)",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default: current directory)",
                },
            },
            "required": ["symbol"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        symbol = params.get("symbol", "")
        raw_path = params.get("path", ".")
        search_path = Path(raw_path)
        if self._boundary is not None:
            check = self._boundary.resolve(raw_path, operation="find symbol")
            if not check.success:
                return ToolResult(success=False, output="", error=check.error)
            search_path = check.path or search_path

        if not symbol:
            return ToolResult(success=False, output="", error="symbol is required")

        # 匹配 def foo / class Foo（含缩进，用于方法）
        pattern = re.compile(
            rf"^(\s*)(def|class)\s+({re.escape(symbol)}\w*)\s*[:(]",
            re.MULTILINE,
        )

        matches: list[str] = []
        for filepath in _iter_files(
            search_path,
            "*.py",
            boundary=self._boundary,
            skip_outside=self._boundary.is_inside(search_path) if self._boundary else False,
        ):
            if should_skip_search_path(filepath, repo_root=self._boundary.root if self._boundary else None):
                continue
            if len(matches) >= MAX_RESULTS:
                break
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
                for m in pattern.finditer(content):
                    lineno = content[: m.start()].count("\n") + 1
                    kind = m.group(2)   # def / class
                    name = m.group(3)
                    indent = len(m.group(1))
                    scope = "method" if indent > 0 else "top-level"
                    matches.append(
                        f"{filepath}:{lineno}: {kind} {name} ({scope})"
                    )
                    if len(matches) >= MAX_RESULTS:
                        break
            except OSError:
                continue

        if not matches:
            return ToolResult(
                success=True,
                output=f"No definition found for '{symbol}'",
            )

        return ToolResult(success=True, output="\n".join(matches))


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _iter_files(
    root: Path,
    glob_pattern: str,
    boundary: WorkspaceBoundary | None = None,
    skip_outside: bool = False,
):
    """
    递归遍历目录，跳过 _SKIP_DIRS，按 glob_pattern 过滤文件名。
    """
    if root.is_file():
        if skip_outside and boundary is not None and not boundary.is_inside(root):
            return
        if is_sensitive_path(root, repo_root=boundary.root if boundary else None):
            return
        yield root
        return

    for filepath in sorted(root.rglob(glob_pattern)):
        # 跳过黑名单目录
        if any(part in _SKIP_DIRS for part in filepath.parts):
            continue
        if skip_outside and boundary is not None and not boundary.is_inside(filepath):
            continue
        if is_sensitive_path(filepath, repo_root=boundary.root if boundary else None):
            continue
        if filepath.is_file():
            yield filepath
