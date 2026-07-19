from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from agent.core_benchmark import (
    CommandExecutionResult,
    CoreBenchmarkCaseResult,
    CoreBenchmarkRunResult,
    all_core_cases,
    build_run_command,
    default_core_cases,
    evaluate_case_result,
    medium_cases,
    parse_report_path,
    run_core_benchmark,
    select_benchmark_cases,
    select_core_cases,
    smoke_cases,
)
from entry.cli import cli


def _case(case_id: str):
    return {case.id: case for case in all_core_cases()}[case_id]


def _write_report(
    path: Path,
    *,
    mode: str,
    changed_files: list[str],
    patch_chars: int | None = None,
) -> None:
    resolved_patch_chars = (
        patch_chars
        if patch_chars is not None
        else (42 if changed_files else 0)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "permission_mode": mode,
            "result": {
                "status": "success",
                "has_patch": bool(changed_files),
                "patch_chars": resolved_patch_chars,
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


def test_suite_definitions_keep_smoke_default_and_medium_separate():
    assert len(smoke_cases()) == 5
    assert len(default_core_cases()) == 5
    assert len(medium_cases()) == 8
    assert len(all_core_cases()) == 13
    assert all(case.suite == "smoke" for case in smoke_cases())
    assert all(case.suite == "medium" for case in medium_cases())


def test_explicit_case_ids_are_selected_across_suites():
    selected = select_benchmark_cases(case_ids=("no_test_cheating",))

    assert [case.id for case in selected] == ["no_test_cheating"]
    assert selected[0].suite == "medium"


def test_medium_suite_selection():
    selected = select_benchmark_cases(suite="medium", limit=2)

    assert [case.suite for case in selected] == ["medium", "medium"]
    assert [case.id for case in selected] == [
        "no_test_cheating",
        "existing_tests_must_stay_green",
    ]


def test_medium_case_repos_have_failing_pytest_oracles(tmp_path):
    for case in medium_cases():
        repo = tmp_path / case.id
        repo.mkdir()
        case.setup(repo)

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = proc.stdout + proc.stderr

        assert proc.returncode == 1, f"{case.id} did not fail as expected:\n{output}"
        assert "failed" in output
        assert "SyntaxError" not in output
        assert "ModuleNotFoundError" not in output


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
    assert result.suite == "custom"
    assert result.total == 1
    assert result.cases[0].changed_files == ["calc.py"]
    assert result.cases[0].suite == "smoke"
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] == 1
    assert summary["suite"] == "custom"
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


def test_evaluate_case_result_enforces_patch_size_limit(tmp_path):
    case = _case("minimal_patch_required")
    repo = tmp_path / "repo"
    repo.mkdir()
    case.setup(repo)
    (repo / "ranking.py").write_text(
        "def rank_players(players):\n    return []\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"
    _write_report(
        report_path,
        mode="fix",
        changed_files=["ranking.py"],
        patch_chars=5000,
    )

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
    assert any("patch_chars <=" in item for item in result.failures)


def test_cli_eval_run_core_list_cases():
    runner = CliRunner()

    result = runner.invoke(cli, [
        "eval",
        "run-core",
        "--suite",
        "all",
        "--list-cases",
    ], obj={})

    assert result.exit_code == 0
    assert "basic_python_fix" in result.output
    assert "no_test_cheating" in result.output
    assert "verification_guard" in result.output


def test_cli_eval_run_core_calls_helper(tmp_path):
    helper_result = CoreBenchmarkRunResult(
        run_id="run-1",
        suite="smoke",
        work_dir=str(tmp_path / "work"),
        summary_path=str(tmp_path / "summary.json"),
        total=1,
        passed=1,
        failed=0,
        cases=[
            CoreBenchmarkCaseResult(
                case_id="basic_python_fix",
                name="basic Python bug fix",
                suite="smoke",
                tags=["fix"],
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
            "--suite",
            "medium",
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
    assert kwargs["suite"] == "medium"
    assert kwargs["config_path"] == "config/eval.yaml"
    assert kwargs["case_ids"] == ("basic_python_fix",)
    assert kwargs["provider"] == "mock"
    assert kwargs["model"] == "mock-model"
    assert kwargs["max_steps"] == 5
