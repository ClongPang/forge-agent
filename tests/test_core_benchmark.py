from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from agent.core_benchmark import (
    CommandExecutionResult,
    CoreBenchmarkCaseResult,
    CoreBenchmarkRunResult,
    build_run_command,
    default_core_cases,
    evaluate_case_result,
    parse_report_path,
    run_core_benchmark,
    select_core_cases,
)
from entry.cli import cli


def _case(case_id: str):
    return {case.id: case for case in default_core_cases()}[case_id]


def _write_report(path: Path, *, mode: str, changed_files: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "permission_mode": mode,
            "result": {
                "status": "success",
                "has_patch": bool(changed_files),
                "patch_chars": 42 if changed_files else 0,
            },
            "verification": {
                "requested": True,
                "status": "passed",
                "passed": True,
                "blocked_count": 0,
                "commands": [],
            },
            "changed_files": changed_files,
        }),
        encoding="utf-8",
    )


def test_select_core_cases_accepts_comma_separated_ids():
    selected = select_core_cases(
        default_core_cases(),
        case_ids=("verification_guard,basic_python_fix",),
        limit=1,
    )

    assert [case.id for case in selected] == ["verification_guard"]


def test_build_run_command_passes_case_controls(tmp_path):
    case = _case("basic_python_fix")
    command = build_run_command(
        case=case,
        repo_path=tmp_path / "repo",
        python_executable="/venv/bin/python",
        config_path="config/eval.yaml",
        provider="openai",
        model="gpt-test",
        max_steps=7,
        verify_timeout=12,
    )

    assert command[:5] == ["/venv/bin/python", "-m", "entry.cli", "--config", "config/eval.yaml"]
    assert "--repo" in command
    assert "--mode" in command
    assert "--verify" in command
    assert "python -m pytest -q" in command
    assert "--fail-on-unverified" in command
    assert command[command.index("--max-steps") + 1] == "7"


def test_parse_report_path_accepts_relative_and_ansi_paths(tmp_path):
    stdout = "\x1b[32mReport  : logs/run/report.json\x1b[0m\n"

    parsed = parse_report_path(stdout, project_root=tmp_path)

    assert parsed == tmp_path / "logs/run/report.json"


def test_run_core_benchmark_uses_fake_runner_and_writes_summary(tmp_path):
    invoked_commands = []

    def fake_runner(command, cwd, env, timeout):
        invoked_commands.append(command)
        repo = Path(command[command.index("--repo") + 1])
        mode = command[command.index("--mode") + 1]
        (repo / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        report_path = tmp_path / "artifacts" / repo.name / "report.json"
        _write_report(report_path, mode=mode, changed_files=["calc.py"])
        return CommandExecutionResult(
            returncode=0,
            stdout=f"Report  : {report_path}\n",
            stderr="",
        )

    result = run_core_benchmark(
        work_dir=tmp_path / "work",
        output_path=tmp_path / "summary.json",
        case_ids=("basic_python_fix",),
        project_root=tmp_path,
        python_executable=".venv/bin/python",
        command_runner=fake_runner,
    )

    assert result.success is True
    assert invoked_commands[0][:3] == [".venv/bin/python", "-m", "entry.cli"]
    assert result.total == 1
    assert result.cases[0].changed_files == ["calc.py"]
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] == 1
    assert summary["cases"][0]["case_id"] == "basic_python_fix"


def test_evaluate_case_result_detects_forbidden_test_edit(tmp_path):
    case = _case("basic_python_fix")
    repo = tmp_path / "repo"
    repo.mkdir()
    case.setup(repo)
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (repo / "test_calc.py").write_text(
        "def test_rewritten():\n    assert True\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"
    _write_report(report_path, mode="fix", changed_files=["calc.py", "test_calc.py"])

    result = evaluate_case_result(
        case=case,
        process=CommandExecutionResult(
            returncode=0,
            stdout=f"Report  : {report_path}\n",
            stderr="",
        ),
        repo_path=repo,
        project_root=tmp_path,
        duration_seconds=0.1,
    )

    assert result.passed is False
    assert "test_calc.py" in result.changed_files
    assert any("expected changed files exactly" in item for item in result.failures)


def test_cli_eval_run_core_list_cases():
    runner = CliRunner()

    result = runner.invoke(cli, ["eval", "run-core", "--list-cases"], obj={})

    assert result.exit_code == 0
    assert "basic_python_fix" in result.output
    assert "verification_guard" in result.output


def test_cli_eval_run_core_calls_helper(tmp_path):
    helper_result = CoreBenchmarkRunResult(
        run_id="run-1",
        work_dir=str(tmp_path / "work"),
        summary_path=str(tmp_path / "summary.json"),
        total=1,
        passed=1,
        failed=0,
        cases=[
            CoreBenchmarkCaseResult(
                case_id="basic_python_fix",
                name="basic Python bug fix",
                passed=True,
                failures=[],
                exit_code=0,
                repo_path=str(tmp_path / "repo"),
                report_path=str(tmp_path / "report.json"),
                artifact_dir=str(tmp_path),
                duration_seconds=0.1,
                permission_mode="fix",
                result_status="success",
                verification_status="passed",
                verification_passed=True,
                changed_files=["calc.py"],
                report_changed_files=["calc.py"],
                stdout_tail="",
                stderr_tail="",
            )
        ],
    )

    runner = CliRunner()
    with patch("agent.core_benchmark.run_core_benchmark", return_value=helper_result) as mock_run:
        result = runner.invoke(cli, [
            "--config",
            "config/eval.yaml",
            "eval",
            "run-core",
            "--case",
            "basic_python_fix",
            "--work-dir",
            str(tmp_path / "work"),
            "--output",
            str(tmp_path / "summary.json"),
            "--provider",
            "mock",
            "--model",
            "mock-model",
            "--max-steps",
            "5",
        ], obj={})

    assert result.exit_code == 0, result.output
    assert "Passed : 1/1" in result.output
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert kwargs["config_path"] == "config/eval.yaml"
    assert kwargs["case_ids"] == ("basic_python_fix",)
    assert kwargs["provider"] == "mock"
    assert kwargs["model"] == "mock-model"
    assert kwargs["max_steps"] == 5
