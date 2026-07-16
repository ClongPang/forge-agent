from __future__ import annotations

import shlex
from unittest.mock import MagicMock

from tools.file_tool import FileReadTool
from tools.git_tool import GitCommitTool, GitDiffTool, GitStatusTool
from tools.path_guard import WorkspaceBoundary
from tools.runtime import RunResult
from tools.search_tool import SearchTextTool
from tools.shell_tool import ShellTool
from tools.test_tool import PytestTool


class TestWorkspaceBoundary:
    def test_relative_path_resolves_inside_root(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        boundary = WorkspaceBoundary(root)

        result = boundary.resolve("src/app.py", operation="read")

        assert result.success
        assert result.path == root / "src" / "app.py"

    def test_absolute_path_inside_root_allowed(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        target = root / "README.md"
        boundary = WorkspaceBoundary(root)

        result = boundary.resolve(target, operation="read")

        assert result.success
        assert result.path == target

    def test_parent_escape_without_callback_rejected(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        boundary = WorkspaceBoundary(root)

        result = boundary.resolve("../outside.txt", operation="read")

        assert not result.success
        assert "outside" in result.error.lower()

    def test_sibling_prefix_is_not_inside_root(self, tmp_path):
        root = tmp_path / "repo"
        sibling = tmp_path / "repo2"
        root.mkdir()
        sibling.mkdir()
        boundary = WorkspaceBoundary(root)

        assert not boundary.is_inside(sibling)
        result = boundary.resolve(sibling / "x.py", operation="read")
        assert not result.success

    def test_symlink_to_outside_rejected(self, tmp_path):
        root = tmp_path / "repo"
        outside = tmp_path / "outside.txt"
        root.mkdir()
        outside.write_text("secret")
        link = root / "link.txt"
        link.symlink_to(outside)
        boundary = WorkspaceBoundary(root)

        result = boundary.resolve(link, operation="read")

        assert not result.success
        assert result.outside

    def test_write_under_symlink_parent_rejected(self, tmp_path):
        root = tmp_path / "repo"
        outside_dir = tmp_path / "outside"
        root.mkdir()
        outside_dir.mkdir()
        link = root / "linked_dir"
        link.symlink_to(outside_dir, target_is_directory=True)
        boundary = WorkspaceBoundary(root)

        result = boundary.resolve(link / "new.txt", operation="write")

        assert not result.success
        assert result.path == outside_dir / "new.txt"

    def test_outside_callback_allow(self, tmp_path):
        root = tmp_path / "repo"
        outside = tmp_path / "outside.txt"
        root.mkdir()
        outside.write_text("ok")
        received = []
        boundary = WorkspaceBoundary(root, confirm_callback=lambda reason: received.append(reason) or True)

        result = boundary.resolve(outside, operation="read")

        assert result.success
        assert result.outside
        assert received and "outside workspace" in received[0]

    def test_outside_callback_deny(self, tmp_path):
        root = tmp_path / "repo"
        outside = tmp_path / "outside.txt"
        root.mkdir()
        outside.write_text("no")
        boundary = WorkspaceBoundary(root, confirm_callback=lambda reason: False)

        result = boundary.resolve(outside, operation="read")

        assert not result.success


class TestToolBoundaryIntegration:
    def test_file_read_outside_without_confirm_rejected(self, tmp_path):
        root = tmp_path / "repo"
        outside = tmp_path / "outside.txt"
        root.mkdir()
        outside.write_text("secret")
        tool = FileReadTool(boundary=WorkspaceBoundary(root))

        result = tool.execute({"path": str(outside)})

        assert not result.success
        assert "outside" in result.error.lower()

    def test_file_read_outside_with_confirm_allowed(self, tmp_path):
        root = tmp_path / "repo"
        outside = tmp_path / "outside.txt"
        root.mkdir()
        outside.write_text("visible")
        boundary = WorkspaceBoundary(root, confirm_callback=lambda reason: True)
        tool = FileReadTool(boundary=boundary)

        result = tool.execute({"path": str(outside)})

        assert result.success
        assert "visible" in result.output

    def test_search_outside_without_confirm_rejected(self, tmp_path):
        root = tmp_path / "repo"
        outside = tmp_path / "outside.txt"
        root.mkdir()
        outside.write_text("needle")
        tool = SearchTextTool(boundary=WorkspaceBoundary(root))

        result = tool.execute({"pattern": "needle", "path": str(outside)})

        assert not result.success
        assert "outside" in result.error.lower()

    def test_search_inside_repo_skips_symlink_to_outside(self, tmp_path):
        root = tmp_path / "repo"
        outside = tmp_path / "outside.txt"
        root.mkdir()
        (root / "inside.txt").write_text("nothing here")
        outside.write_text("needle")
        (root / "outside_link.txt").symlink_to(outside)
        tool = SearchTextTool(boundary=WorkspaceBoundary(root))

        result = tool.execute({"pattern": "needle", "path": str(root)})

        assert result.success
        assert "outside_link" not in result.output
        assert "No matches" in result.output

    def test_shell_readonly_outside_path_requires_confirm(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        runtime = MagicMock()
        tool = ShellTool(
            confirm_callback=lambda cmd: False,
            runtime=runtime,
            boundary=WorkspaceBoundary(root, confirm_callback=lambda reason: False),
        )

        result = tool.execute({"cmd": "cat /etc/hosts"})

        assert not result.success
        assert "outside" in result.error.lower()
        runtime.exec.assert_not_called()

    def test_shell_redirect_outside_path_requires_confirm(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        runtime = MagicMock()
        tool = ShellTool(
            confirm_callback=lambda cmd: False,
            runtime=runtime,
            boundary=WorkspaceBoundary(root, confirm_callback=lambda reason: False),
        )

        result = tool.execute({"cmd": "cat > /tmp/x.py << 'EOF'\nprint(1)\nEOF"})

        assert not result.success
        runtime.exec.assert_not_called()

    def test_pytest_external_cwd_without_confirm_rejected(self, tmp_path):
        root = tmp_path / "repo"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        runtime = MagicMock()
        tool = PytestTool(runtime=runtime, boundary=WorkspaceBoundary(root))

        result = tool.execute({"cwd": str(outside)})

        assert not result.success
        assert "outside" in result.error.lower()
        runtime.exec.assert_not_called()

    def test_git_external_cwd_without_confirm_rejected(self, tmp_path):
        root = tmp_path / "repo"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        runtime = MagicMock()
        tool = GitStatusTool(runtime=runtime, boundary=WorkspaceBoundary(root))

        result = tool.execute({"cwd": str(outside)})

        assert not result.success
        assert "outside" in result.error.lower()
        runtime.exec.assert_not_called()

    def test_git_diff_external_path_without_confirm_rejected(self, tmp_path):
        root = tmp_path / "repo"
        outside = tmp_path / "outside.py"
        root.mkdir()
        outside.write_text("x = 1")
        runtime = MagicMock()
        tool = GitDiffTool(runtime=runtime, boundary=WorkspaceBoundary(root))

        result = tool.execute({"cwd": str(root), "path": str(outside)})

        assert not result.success
        assert "outside" in result.error.lower()
        runtime.exec.assert_not_called()

    def test_git_commit_message_is_shell_quoted(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        calls = []

        class RecordingRuntime:
            def exec(self, cmd, cwd=None, timeout=30):
                calls.append(cmd)
                return RunResult(returncode=0, stdout="ok", stderr="")

        message = 'fix "quoted"; echo bad'
        tool = GitCommitTool(
            runtime=RecordingRuntime(),
            boundary=WorkspaceBoundary(root),
        )

        result = tool.execute({"cwd": str(root), "message": message})

        assert result.success
        assert shlex.split(calls[0]) == ["git", "commit", "-m", message]

    def test_build_registry_injects_confirm_callback_into_shell(self, tmp_path):
        from config.schema import AppConfig
        from entry.cli import _build_registry

        root = tmp_path / "repo"
        root.mkdir()
        callback = lambda cmd: False
        registry = _build_registry(AppConfig(), confirm_callback=callback, repo_path=str(root))

        assert registry._tools["shell"]._confirm_callback is callback
