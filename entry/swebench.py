"""
entry/swebench.py

Generate SWE-bench prediction files with Forge Agent.

This module intentionally does not grade patches. The official SWE-bench
harness owns scoring; this entrypoint only turns SWE-bench instances into
`predictions.jsonl` records:

    {"instance_id": "...", "model_name_or_path": "...", "model_patch": "..."}
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import click

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent.core import Agent, AgentConfig  # noqa: E402
from agent.event_log import EventLog  # noqa: E402
from agent.task import RunResult, Task  # noqa: E402
from agent.telemetry import build_tracer_from_config  # noqa: E402
from config.schema import AppConfig, load_config, merge_cli_overrides  # noqa: E402
from llm.router import create_backend_from_config  # noqa: E402
from tools.base import ToolRegistry  # noqa: E402
from tools.file_tool import FileReadTool, FileViewTool, FileWriteTool  # noqa: E402
from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool  # noqa: E402
from tools.path_guard import WorkspaceBoundary  # noqa: E402
from tools.runtime import LocalRuntime  # noqa: E402
from tools.search_tool import FindFilesTool, FindSymbolTool, SearchTextTool  # noqa: E402
from tools.shell_tool import ShellTool, always_allow, always_deny  # noqa: E402
from tools.test_tool import PytestTool  # noqa: E402


logger = logging.getLogger(__name__)

DEFAULT_DATASET_NAME = "princeton-nlp/SWE-bench_Lite"
DEFAULT_SPLIT = "dev"
DEFAULT_WORK_DIR = "runs/swebench"

KEY_INSTANCE_ID = "instance_id"
KEY_MODEL = "model_name_or_path"
KEY_PREDICTION = "model_patch"


@dataclass
class SwebenchRunRecord:
    """Sidecar metadata for one Forge Agent SWE-bench generation run."""

    instance_id: str
    repo: str
    base_commit: str
    agent_status: str
    agent_success: bool
    patch_chars: int
    empty_patch: bool
    steps_taken: int
    total_tokens: int
    elapsed_sec: float
    log_path: str
    repo_path: str
    error: str | None = None


@dataclass
class SwebenchInstanceResult:
    """Generated prediction plus internal metadata for one instance."""

    prediction: dict[str, str]
    metadata: SwebenchRunRecord


class GitCommandError(RuntimeError):
    """Raised when a git command needed for benchmark setup fails."""


def load_swebench_instances(dataset_name: str, split: str) -> list[dict[str, Any]]:
    """Load SWE-bench instances from Hugging Face Datasets."""
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            "The 'datasets' package is required for SWE-bench generation. "
            'Install it with: pip install -e ".[swebench]"'
        ) from exc

    dataset = load_dataset(dataset_name, split=split)
    return [dict(item) for item in dataset]


def parse_instance_ids(raw: Iterable[str] | None) -> list[str]:
    """Parse repeated or comma/space-separated instance id CLI values."""
    if not raw:
        return []

    ids: list[str] = []
    for item in raw:
        for part in re.split(r"[\s,]+", item.strip()):
            if part:
                ids.append(part)
    return ids


def select_instances(
    instances: list[dict[str, Any]],
    *,
    instance_ids: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Select instances by explicit ids and/or a simple prefix limit."""
    requested_ids = list(instance_ids or [])
    if requested_ids:
        by_id = {str(item[KEY_INSTANCE_ID]): item for item in instances}
        missing = [item_id for item_id in requested_ids if item_id not in by_id]
        if missing:
            raise ValueError(f"Instance ids not found in dataset: {', '.join(missing)}")
        selected = [by_id[item_id] for item_id in requested_ids]
    else:
        selected = list(instances)

    if limit is not None:
        if limit < 0:
            raise ValueError("--limit must be non-negative")
        selected = selected[:limit]
    return selected


def prediction_record(
    *,
    instance_id: str,
    model_name_or_path: str,
    model_patch: str,
) -> dict[str, str]:
    """Build one official SWE-bench prediction JSON object."""
    return {
        KEY_INSTANCE_ID: instance_id,
        KEY_MODEL: model_name_or_path,
        KEY_PREDICTION: model_patch,
    }


def read_prediction_ids(path: str | Path) -> set[str]:
    """Read completed prediction ids from an existing JSONL file."""
    output_path = Path(path)
    if not output_path.exists():
        return set()

    ids: set[str] = set()
    with output_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            instance_id = raw.get(KEY_INSTANCE_ID)
            if instance_id:
                ids.add(str(instance_id))
    return ids


def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    """Append a JSON object to a JSONL file, creating parents as needed."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def default_metadata_path(output_path: str | Path) -> Path:
    """Return the default sidecar metadata path for a predictions file."""
    path = Path(output_path)
    suffix = "".join(path.suffixes)
    if suffix:
        return path.with_name(path.name[: -len(suffix)] + ".metadata.jsonl")
    return path.with_name(path.name + ".metadata.jsonl")


def model_label(config: AppConfig, override: str | None = None) -> str:
    """Format the model name stored in SWE-bench prediction records."""
    if override:
        return override
    return f"forgeagent/{config.llm.provider}/{config.llm.model}"


def prepare_instance_repo(
    instance: dict[str, Any],
    *,
    work_dir: str | Path,
    refresh_repos: bool = False,
) -> Path:
    """
    Prepare a clean per-instance checkout at the SWE-bench base commit.

    Repositories are cached under work_dir/repos and cloned into
    work_dir/instances/{instance_id} for each run.
    """
    repo_name = str(instance["repo"])
    instance_id = str(instance[KEY_INSTANCE_ID])
    base_commit = str(instance["base_commit"])

    root = Path(work_dir).resolve()
    cache_dir = root / "repos" / _safe_path_name(repo_name)
    instance_dir = root / "instances" / _safe_path_name(instance_id)
    root.mkdir(parents=True, exist_ok=True)

    if not (cache_dir / ".git").exists():
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_command(
            ["git", "clone", f"https://github.com/{repo_name}.git", str(cache_dir)],
            cwd=root,
            timeout=600,
        )
    elif refresh_repos:
        _run_git(["fetch", "--all", "--tags", "--prune"], cwd=cache_dir, timeout=300)

    if not (instance_dir / ".git").exists():
        instance_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_command(
            ["git", "clone", str(cache_dir), str(instance_dir)],
            cwd=root,
            timeout=300,
        )

    try:
        _checkout_clean(instance_dir, base_commit)
    except GitCommandError:
        _run_git(["fetch", "--all", "--tags", "--prune"], cwd=instance_dir, timeout=300)
        _checkout_clean(instance_dir, base_commit)

    return instance_dir


def extract_patch(repo_path: str | Path) -> str:
    """Extract the current git diff as a SWE-bench patch string."""
    proc = _run_command(
        ["git", "-c", "core.fileMode=false", "diff", "HEAD"],
        cwd=Path(repo_path),
        timeout=60,
    )
    return proc.stdout.strip()


def run_swebench_instance(
    instance: dict[str, Any],
    *,
    config: AppConfig,
    work_dir: str | Path,
    log_dir: str | Path,
    model_name_or_path: str,
    auto_confirm_tools: bool = True,
    refresh_repos: bool = False,
    stream: bool = False,
) -> SwebenchInstanceResult:
    """Run Forge Agent on one SWE-bench instance and return its prediction."""
    repo_path = prepare_instance_repo(
        instance,
        work_dir=work_dir,
        refresh_repos=refresh_repos,
    )
    instance_id = str(instance[KEY_INSTANCE_ID])
    problem_statement = str(instance.get("problem_statement") or "")
    repo_name = str(instance.get("repo") or "")
    base_commit = str(instance.get("base_commit") or "")
    task_description = build_swebench_task_description(problem_statement)

    backend = create_backend_from_config({
        "provider": config.llm.provider,
        "model": config.llm.model,
        "api_key": config.llm.api_key or None,
        "base_url": config.llm.base_url or None,
        "max_tokens": config.llm.max_tokens,
    })
    confirm_cb = always_allow if auto_confirm_tools else always_deny
    registry = build_benchmark_registry(repo_path, confirm_callback=confirm_cb)

    tracer = build_tracer_from_config(
        config,
        mode="swebench",
        provider=config.llm.provider,
        model=config.llm.model,
    )
    agent_config = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        history_max_messages=config.context.history_window * 2,
        stream=stream,
        confirm_dangerous=auto_confirm_tools,
        confirm_callback=confirm_cb,
        tracer=tracer,
    )
    agent = Agent(backend, registry, agent_config)
    task = Task(
        task_id=_task_id_from_instance(instance_id),
        description=task_description,
        repo_path=str(repo_path),
        issue_url=instance.get("issue_url"),
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )

    started = time.time()
    try:
        with EventLog.create(task, log_dir=str(log_dir)) as log:
            result = agent.run(task, log)
            log_path = str(log.path)
    finally:
        if getattr(tracer, "flush_on_exit", False):
            tracer.flush()

    patch = extract_patch(repo_path)
    elapsed = time.time() - started
    metadata = build_metadata_record(
        instance=instance,
        result=result,
        patch=patch,
        elapsed_sec=elapsed,
        log_path=log_path,
        repo_path=str(repo_path),
    )
    prediction = prediction_record(
        instance_id=instance_id,
        model_name_or_path=model_name_or_path,
        model_patch=patch,
    )
    return SwebenchInstanceResult(prediction=prediction, metadata=metadata)


def build_metadata_record(
    *,
    instance: dict[str, Any],
    result: RunResult,
    patch: str,
    elapsed_sec: float,
    log_path: str,
    repo_path: str,
) -> SwebenchRunRecord:
    """Build sidecar metadata for one generation attempt."""
    return SwebenchRunRecord(
        instance_id=str(instance[KEY_INSTANCE_ID]),
        repo=str(instance.get("repo") or ""),
        base_commit=str(instance.get("base_commit") or ""),
        agent_status=result.status.value,
        agent_success=result.is_success(),
        patch_chars=len(patch),
        empty_patch=not bool(patch),
        steps_taken=result.steps_taken,
        total_tokens=result.total_tokens,
        elapsed_sec=round(elapsed_sec, 3),
        log_path=log_path,
        repo_path=repo_path,
        error=result.error,
    )


def build_error_result(
    *,
    instance: dict[str, Any],
    model_name_or_path: str,
    error: str,
) -> SwebenchInstanceResult:
    """Build empty-patch records for setup or agent crashes."""
    instance_id = str(instance[KEY_INSTANCE_ID])
    metadata = SwebenchRunRecord(
        instance_id=instance_id,
        repo=str(instance.get("repo") or ""),
        base_commit=str(instance.get("base_commit") or ""),
        agent_status="error",
        agent_success=False,
        patch_chars=0,
        empty_patch=True,
        steps_taken=0,
        total_tokens=0,
        elapsed_sec=0,
        log_path="",
        repo_path="",
        error=error,
    )
    return SwebenchInstanceResult(
        prediction=prediction_record(
            instance_id=instance_id,
            model_name_or_path=model_name_or_path,
            model_patch="",
        ),
        metadata=metadata,
    )


def build_swebench_task_description(problem_statement: str) -> str:
    """Build the user-visible task for Forge Agent."""
    return (
        "Fix the following SWE-bench issue in this repository. "
        "Make the minimal source-code change that resolves the problem. "
        "Avoid editing tests or benchmark files unless the issue explicitly "
        "requires it.\n\n"
        "## Problem Statement\n"
        f"{problem_statement.strip()}"
    )


def build_benchmark_registry(
    repo_path: str | Path,
    *,
    confirm_callback,
) -> ToolRegistry:
    """
    Build tools for unattended benchmark generation.

    The workspace boundary does not receive the auto-confirm callback, so
    explicit outside-workspace paths remain denied while in-repo commands that
    require confirmation can run in the disposable checkout.
    """
    boundary = WorkspaceBoundary(repo_path, confirm_callback=None)
    runtime = LocalRuntime(boundary=boundary)
    return (
        ToolRegistry()
        .register(ShellTool(
            confirm_callback=confirm_callback,
            runtime=runtime,
            boundary=boundary,
        ))
        .register(FileReadTool(boundary=boundary))
        .register(FileViewTool(boundary=boundary))
        .register(FileWriteTool(boundary=boundary))
        .register(SearchTextTool(boundary=boundary))
        .register(FindFilesTool(boundary=boundary))
        .register(FindSymbolTool(boundary=boundary))
        .register(PytestTool(runtime=runtime, boundary=boundary))
        .register(GitStatusTool(runtime=runtime, boundary=boundary))
        .register(GitDiffTool(runtime=runtime, boundary=boundary))
        .register(GitAddTool(runtime=runtime, boundary=boundary))
        .register(GitCommitTool(runtime=runtime, boundary=boundary))
    )


def generate_predictions(
    *,
    instances: list[dict[str, Any]],
    config: AppConfig,
    work_dir: str | Path,
    output_path: str | Path,
    metadata_path: str | Path,
    model_name_or_path: str,
    resume: bool = True,
    auto_confirm_tools: bool = True,
    refresh_repos: bool = False,
    stream: bool = False,
    fail_fast: bool = False,
) -> tuple[int, int]:
    """Generate predictions for selected instances and return (run, skipped)."""
    completed = read_prediction_ids(output_path) if resume else set()
    run_count = 0
    skipped_count = 0

    for index, instance in enumerate(instances, start=1):
        instance_id = str(instance[KEY_INSTANCE_ID])
        if instance_id in completed:
            click.echo(f"[{index}/{len(instances)}] skip {instance_id} (already in output)")
            skipped_count += 1
            continue

        click.echo(f"[{index}/{len(instances)}] generate {instance_id}")
        try:
            item_result = run_swebench_instance(
                instance,
                config=config,
                work_dir=work_dir,
                log_dir=Path(work_dir) / "logs",
                model_name_or_path=model_name_or_path,
                auto_confirm_tools=auto_confirm_tools,
                refresh_repos=refresh_repos,
                stream=stream,
            )
        except Exception as exc:
            logger.exception("SWE-bench generation failed for %s", instance_id)
            item_result = build_error_result(
                instance=instance,
                model_name_or_path=model_name_or_path,
                error=str(exc),
            )
            if fail_fast:
                append_jsonl(output_path, item_result.prediction)
                append_jsonl(metadata_path, asdict(item_result.metadata))
                raise

        append_jsonl(output_path, item_result.prediction)
        append_jsonl(metadata_path, asdict(item_result.metadata))
        run_count += 1
        patch_status = "empty patch" if item_result.metadata.empty_patch else (
            f"{item_result.metadata.patch_chars} patch chars"
        )
        click.echo(
            "  -> "
            f"{item_result.metadata.agent_status}; {patch_status}; "
            f"log={item_result.metadata.log_path or '(none)'}"
        )

    return run_count, skipped_count


def _checkout_clean(repo_path: Path, base_commit: str) -> None:
    _run_git(
        ["-c", "advice.detachedHead=false", "checkout", "-f", base_commit],
        cwd=repo_path,
        timeout=120,
    )
    _run_git(["reset", "--hard", base_commit], cwd=repo_path, timeout=120)
    _run_git(["clean", "-fdx"], cwd=repo_path, timeout=120)


def _run_git(args: list[str], *, cwd: str | Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return _run_command(["git"] + args, cwd=cwd, timeout=timeout)


def _run_command(
    cmd: list[str],
    *,
    cwd: str | Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        rendered = " ".join(cmd)
        output = (proc.stdout + proc.stderr).strip()
        raise GitCommandError(f"{rendered} failed in {cwd}: {output}")
    return proc


def _safe_path_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", value).strip("_") or "instance"


def _task_id_from_instance(instance_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9.-]+", "-", instance_id).strip("-")
    return (cleaned or "swebench")[-48:]


@click.group()
def cli() -> None:
    """Generate SWE-bench prediction files with Forge Agent."""


@cli.command("generate")
@click.option(
    "--dataset-name",
    default=DEFAULT_DATASET_NAME,
    show_default=True,
    help="Hugging Face SWE-bench dataset name.",
)
@click.option(
    "--split",
    default=DEFAULT_SPLIT,
    show_default=True,
    help="Dataset split to generate predictions for.",
)
@click.option(
    "--instance-ids",
    multiple=True,
    help="Instance ids to run. Can be repeated or comma/space separated.",
)
@click.option("--limit", type=int, default=None, help="Limit selected instances.")
@click.option(
    "--work-dir",
    default=DEFAULT_WORK_DIR,
    show_default=True,
    help="Directory for repo caches, per-instance checkouts, and logs.",
)
@click.option(
    "--output",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Output predictions JSONL path.",
)
@click.option(
    "--metadata-output",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Sidecar metadata JSONL path. Defaults next to --output.",
)
@click.option("--config", "-c", default=None, help="Path to Forge Agent config YAML.")
@click.option("--provider", "-p", default=None, help="Override LLM provider.")
@click.option("--model", "-m", default=None, help="Override LLM model.")
@click.option("--max-steps", default=None, type=int, help="Override agent max steps.")
@click.option(
    "--model-name-or-path",
    default=None,
    help="Value to write into prediction model_name_or_path.",
)
@click.option(
    "--resume/--no-resume",
    default=True,
    show_default=True,
    help="Skip instance ids already present in the output JSONL.",
)
@click.option(
    "--auto-confirm-tools/--no-auto-confirm-tools",
    default=True,
    show_default=True,
    help=(
        "Automatically allow in-worktree tool actions that require confirmation. "
        "Explicit outside-workspace paths remain denied."
    ),
)
@click.option(
    "--refresh-repos",
    is_flag=True,
    help="Fetch cached repositories before preparing instances.",
)
@click.option("--stream", is_flag=True, default=False, help="Enable model streaming.")
@click.option("--fail-fast", is_flag=True, help="Stop after the first failed instance.")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs.")
def generate_cmd(
    dataset_name: str,
    split: str,
    instance_ids: tuple[str, ...],
    limit: int | None,
    work_dir: str,
    output: Path,
    metadata_output: Path | None,
    config: str | None,
    provider: str | None,
    model: str | None,
    max_steps: int | None,
    model_name_or_path: str | None,
    resume: bool,
    auto_confirm_tools: bool,
    refresh_repos: bool,
    stream: bool,
    fail_fast: bool,
    verbose: bool,
) -> None:
    """Generate official SWE-bench predictions JSONL."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    app_config = load_config(config)
    app_config = merge_cli_overrides(
        app_config,
        provider=provider,
        model=model,
        max_steps=max_steps,
    )
    label = model_label(app_config, model_name_or_path)
    metadata_path = metadata_output or default_metadata_path(output)

    try:
        loaded = load_swebench_instances(dataset_name, split)
        selected = select_instances(
            loaded,
            instance_ids=parse_instance_ids(instance_ids),
            limit=limit,
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo("Forge Agent SWE-bench generation")
    click.echo(f"  Dataset : {dataset_name} ({split})")
    click.echo(f"  Selected: {len(selected)}")
    click.echo(f"  Model   : {label}")
    click.echo(f"  Output  : {output}")
    click.echo(f"  Metadata: {metadata_path}")
    click.echo(f"  Work dir: {Path(work_dir).resolve()}")

    try:
        run_count, skipped_count = generate_predictions(
            instances=selected,
            config=app_config,
            work_dir=work_dir,
            output_path=output,
            metadata_path=metadata_path,
            model_name_or_path=label,
            resume=resume,
            auto_confirm_tools=auto_confirm_tools,
            refresh_repos=refresh_repos,
            stream=stream,
            fail_fast=fail_fast,
        )
    except Exception as exc:
        click.echo(f"Error: generation stopped: {exc}", err=True)
        sys.exit(1)

    click.echo(
        f"Done. Generated {run_count} prediction record(s), "
        f"skipped {skipped_count}."
    )
    click.echo(
        "Score with the official SWE-bench harness by passing this "
        "predictions JSONL to swebench.harness.run_evaluation."
    )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
