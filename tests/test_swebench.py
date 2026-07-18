"""
Tests for the SWE-bench prediction generation adapter.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agent.task import RunResult, RunStatus
from config.schema import AppConfig
from entry.swebench import (
    KEY_INSTANCE_ID,
    KEY_MODEL,
    KEY_PREDICTION,
    SwebenchInstanceResult,
    SwebenchRunRecord,
    cli,
    default_metadata_path,
    generate_predictions,
    parse_instance_ids,
    prediction_record,
    read_prediction_ids,
    run_swebench_instance,
    select_instances,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git"] + list(args),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _init_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "module.py")
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Forge Agent Tests",
            "-c",
            "user.email=forge-agent@example.invalid",
            "commit",
            "-m",
            "initial",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return repo


class TestSwebenchHelpers:
    def test_parse_instance_ids_accepts_repeated_and_separated_values(self):
        parsed = parse_instance_ids(("sympy__sympy-1,django__django-2", "astropy__astropy-3"))
        assert parsed == [
            "sympy__sympy-1",
            "django__django-2",
            "astropy__astropy-3",
        ]

    def test_select_instances_preserves_requested_order_and_limit(self):
        instances = [
            {KEY_INSTANCE_ID: "a"},
            {KEY_INSTANCE_ID: "b"},
            {KEY_INSTANCE_ID: "c"},
        ]

        selected = select_instances(
            instances,
            instance_ids=["c", "a"],
            limit=1,
        )

        assert selected == [{KEY_INSTANCE_ID: "c"}]

    def test_prediction_record_uses_official_keys(self):
        record = prediction_record(
            instance_id="sympy__sympy-20590",
            model_name_or_path="forgeagent/deepseek/deepseek-v4-pro",
            model_patch="diff --git a/x b/x\n",
        )

        assert set(record) == {KEY_INSTANCE_ID, KEY_MODEL, KEY_PREDICTION}
        assert record[KEY_INSTANCE_ID] == "sympy__sympy-20590"

    def test_read_prediction_ids_ignores_bad_lines(self, tmp_path):
        output = tmp_path / "predictions.jsonl"
        output.write_text(
            '{"instance_id": "a", "model_patch": ""}\n'
            "not-json\n"
            '{"instance_id": "b", "model_patch": "diff"}\n',
            encoding="utf-8",
        )

        assert read_prediction_ids(output) == {"a", "b"}

    def test_default_metadata_path_keeps_jsonl_suffix(self):
        assert default_metadata_path("runs/dev_predictions.jsonl").name == (
            "dev_predictions.metadata.jsonl"
        )


class TestSwebenchGeneration:
    def test_run_swebench_instance_collects_patch_and_metadata(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        instance = {
            KEY_INSTANCE_ID: "owner__repo-1",
            "repo": "owner/repo",
            "base_commit": _git(repo, "rev-parse", "HEAD"),
            "problem_statement": "VALUE should be 2",
        }

        class FakeAgent:
            def __init__(self, *_args, **_kwargs):
                pass

            def run(self, task, _log):
                assert "SWE-bench" in task.description
                assert "__" not in task.task_id
                Path(task.repo_path, "module.py").write_text(
                    "VALUE = 2\n",
                    encoding="utf-8",
                )
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.SUCCESS,
                    summary="changed value",
                    steps_taken=2,
                    total_tokens=123,
                )

        with patch("entry.swebench.prepare_instance_repo", return_value=repo):
            with patch("entry.swebench.create_backend_from_config", return_value=MagicMock()):
                with patch("entry.swebench.Agent", FakeAgent):
                    result = run_swebench_instance(
                        instance,
                        config=AppConfig(),
                        work_dir=tmp_path / "work",
                        log_dir=tmp_path / "logs",
                        model_name_or_path="forgeagent/mock",
                    )

        patch_text = result.prediction[KEY_PREDICTION]
        assert result.prediction[KEY_INSTANCE_ID] == "owner__repo-1"
        assert patch_text.startswith("diff --git")
        assert "-VALUE = 1" in patch_text
        assert "+VALUE = 2" in patch_text
        assert result.metadata.agent_status == "success"
        assert result.metadata.patch_chars == len(patch_text)
        assert result.metadata.total_tokens == 123
        assert result.metadata.log_path.endswith(".jsonl")

    def test_generate_predictions_resumes_existing_output(self, tmp_path):
        instances = [
            {KEY_INSTANCE_ID: "a", "repo": "owner/repo", "base_commit": "abc"},
            {KEY_INSTANCE_ID: "b", "repo": "owner/repo", "base_commit": "def"},
        ]
        output = tmp_path / "predictions.jsonl"
        metadata = tmp_path / "metadata.jsonl"
        output.write_text(
            json.dumps(prediction_record(
                instance_id="a",
                model_name_or_path="forgeagent/mock",
                model_patch="diff --git a/x b/x\n",
            )) + "\n",
            encoding="utf-8",
        )

        def fake_run(instance, **_kwargs):
            return SwebenchInstanceResult(
                prediction=prediction_record(
                    instance_id=instance[KEY_INSTANCE_ID],
                    model_name_or_path="forgeagent/mock",
                    model_patch="diff --git a/y b/y\n",
                ),
                metadata=SwebenchRunRecord(
                    instance_id=instance[KEY_INSTANCE_ID],
                    repo=instance["repo"],
                    base_commit=instance["base_commit"],
                    agent_status="success",
                    agent_success=True,
                    patch_chars=21,
                    empty_patch=False,
                    steps_taken=1,
                    total_tokens=10,
                    elapsed_sec=0.1,
                    log_path="log.jsonl",
                    repo_path="/repo",
                ),
            )

        with patch("entry.swebench.run_swebench_instance", side_effect=fake_run):
            run_count, skipped_count = generate_predictions(
                instances=instances,
                config=AppConfig(),
                work_dir=tmp_path / "work",
                output_path=output,
                metadata_path=metadata,
                model_name_or_path="forgeagent/mock",
                resume=True,
            )

        assert (run_count, skipped_count) == (1, 1)
        prediction_lines = output.read_text(encoding="utf-8").splitlines()
        metadata_lines = metadata.read_text(encoding="utf-8").splitlines()
        assert len(prediction_lines) == 2
        assert json.loads(prediction_lines[-1])[KEY_INSTANCE_ID] == "b"
        assert len(metadata_lines) == 1
        assert json.loads(metadata_lines[0])[KEY_INSTANCE_ID] == "b"

    def test_cli_generate_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["generate", "--help"])

        assert result.exit_code == 0
        assert "--dataset-name" in result.output
        assert "--output" in result.output
