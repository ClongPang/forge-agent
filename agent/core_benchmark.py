"""
agent/core_benchmark.py

Local Core Benchmark for Forge Agent run mode.

The suite turns the manual examples from EVAL_GUIDE.md into executable cases:
each case creates a disposable git repository, invokes the existing `run`
command, reads the generated report.json, and checks machine-readable
expectations.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


DEFAULT_CORE_WORK_DIR = "runs/core-eval"
DEFAULT_CASE_TIMEOUT = 900

_REPORT_LINE_RE = re.compile(r"^Report\s*:\s*(.+?)\s*$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class CoreBenchmarkExpectation:
    """Assertions for one local core benchmark case."""

    exit_code: int | None = None
    exit_nonzero: bool = False
    result_status: str | None = None
    verification_passed: bool | None = None
    verification_status: str | None = None
    verification_blocked_min: int = 0
    has_patch: bool | None = None
    repo_clean: bool | None = None
    changed_files_exact: tuple[str, ...] | None = None
    changed_files_include: tuple[str, ...] = ()
    changed_files_exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoreBenchmarkCase:
    """A benchmark case that can build its own disposable repository."""

    id: str
    name: str
    task: str
    mode: str
    setup: Callable[[Path], None]
    verify_commands: tuple[str, ...] = ()
    fail_on_unverified: bool = True
    expectation: CoreBenchmarkExpectation = CoreBenchmarkExpectation()
    tags: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "mode": self.mode,
            "verify_commands": list(self.verify_commands),
            "fail_on_unverified": self.fail_on_unverified,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class CommandExecutionResult:
    """Captured result of invoking `entry.cli run` for one case."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class CoreBenchmarkCaseResult:
    """Structured result for one benchmark case."""

    case_id: str
    name: str
    passed: bool
    failures: list[str]
    exit_code: int
    repo_path: str
    report_path: str | None
    artifact_dir: str | None
    duration_seconds: float
    permission_mode: str | None
    result_status: str | None
    verification_status: str | None
    verification_passed: bool | None
    changed_files: list[str]
    report_changed_files: list[str]
    stdout_tail: str
    stderr_tail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CoreBenchmarkRunResult:
    """Summary for a completed core benchmark run."""

    run_id: str
    work_dir: str
    summary_path: str
    total: int
    passed: int
    failed: int
    cases: list[CoreBenchmarkCaseResult]

    @property
    def success(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "work_dir": self.work_dir,
            "summary_path": self.summary_path,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "cases": [case.to_dict() for case in self.cases],
        }


CommandRunner = Callable[
    [list[str], Path, dict[str, str], int],
    CommandExecutionResult,
]


def default_core_cases() -> list[CoreBenchmarkCase]:
    """Return the built-in local core benchmark suite."""
    return [
        CoreBenchmarkCase(
            id="basic_python_fix",
            name="basic Python bug fix",
            mode="fix",
            setup=_setup_basic_python_fix,
            task=(
                "Fix the failing pytest tests. Keep the public API unchanged, "
                "do not edit tests, and make the smallest reasonable source "
                "code change."
            ),
            verify_commands=("python -m pytest -q",),
            expectation=CoreBenchmarkExpectation(
                exit_code=0,
                result_status="success",
                verification_passed=True,
                verification_status="passed",
                has_patch=True,
                changed_files_exact=("calc.py",),
            ),
            tags=("fix", "verification", "artifact"),
        ),
        CoreBenchmarkCase(
            id="multi_file_python_fix",
            name="multi-file Python bug fix",
            mode="fix",
            setup=_setup_multi_file_python_fix,
            task=(
                "Fix the failing pytest tests by finding the source bug. "
                "Do not edit tests and avoid changing the public API."
            ),
            verify_commands=("python -m pytest -q",),
            expectation=CoreBenchmarkExpectation(
                exit_code=0,
                result_status="success",
                verification_passed=True,
                verification_status="passed",
                has_patch=True,
                changed_files_exact=("store/pricing.py",),
            ),
            tags=("fix", "context", "verification"),
        ),
        CoreBenchmarkCase(
            id="inspect_readonly",
            name="inspect mode remains read-only",
            mode="inspect",
            setup=_setup_inspect_readonly,
            task=(
                "Inspect this repository and explain why the test is failing. "
                "Do not edit files."
            ),
            verify_commands=("python -m pytest -q",),
            expectation=CoreBenchmarkExpectation(
                exit_nonzero=True,
                verification_passed=False,
                verification_status="failed",
                has_patch=False,
                repo_clean=True,
                changed_files_exact=(),
            ),
            tags=("inspect", "policy", "verification"),
        ),
        CoreBenchmarkCase(
            id="fail_on_unverified",
            name="verification failure controls exit code",
            mode="inspect",
            setup=_setup_passing_calculator,
            task="Inspect this repository briefly. Do not edit files.",
            verify_commands=("false",),
            expectation=CoreBenchmarkExpectation(
                exit_nonzero=True,
                verification_passed=False,
                verification_status="failed",
                has_patch=False,
                repo_clean=True,
                changed_files_exact=(),
            ),
            tags=("exit-code", "verification"),
        ),
        CoreBenchmarkCase(
            id="verification_guard",
            name="verification command safety guard",
            mode="inspect",
            setup=_setup_passing_calculator,
            task="Inspect this repository briefly. Do not edit files.",
            verify_commands=("python -m pytest -q && curl https://example.com",),
            expectation=CoreBenchmarkExpectation(
                exit_nonzero=True,
                verification_passed=False,
                verification_status="failed",
                verification_blocked_min=1,
                has_patch=False,
                repo_clean=True,
                changed_files_exact=(),
            ),
            tags=("security", "verification"),
        ),
    ]


def select_core_cases(
    cases: Sequence[CoreBenchmarkCase],
    case_ids: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[CoreBenchmarkCase]:
    """Select benchmark cases by repeated/comma-separated ids and optional limit."""
    parsed_ids = _parse_case_ids(case_ids)
    selected: list[CoreBenchmarkCase]
    if parsed_ids:
        by_id = {case.id: case for case in cases}
        missing = [case_id for case_id in parsed_ids if case_id not in by_id]
        if missing:
            raise ValueError(f"Core benchmark case(s) not found: {', '.join(missing)}")
        selected = [by_id[case_id] for case_id in parsed_ids]
    else:
        selected = list(cases)

    if limit is not None:
        if limit < 0:
            raise ValueError("--limit must be non-negative")
        selected = selected[:limit]
    return selected


def run_core_benchmark(
    *,
    work_dir: str | Path = DEFAULT_CORE_WORK_DIR,
    output_path: str | Path | None = None,
    case_ids: Sequence[str] | None = None,
    limit: int | None = None,
    config_path: str | Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_steps: int | None = None,
    verify_timeout: int = 300,
    case_timeout: int = DEFAULT_CASE_TIMEOUT,
    fail_fast: bool = False,
    python_executable: str | Path | None = None,
    project_root: str | Path | None = None,
    command_runner: CommandRunner | None = None,
) -> CoreBenchmarkRunResult:
    """Run the local core benchmark suite and write a JSON summary."""
    if verify_timeout <= 0:
        raise ValueError("--verify-timeout must be positive")
    if case_timeout <= 0:
        raise ValueError("--case-timeout must be positive")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    run_dir = Path(work_dir).resolve() / run_id
    repos_dir = run_dir / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)

    summary_path = Path(output_path).resolve() if output_path else run_dir / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    root = Path(project_root).resolve() if project_root else Path(__file__).parent.parent
    # Do not resolve the Python executable. On macOS, venv python is often a
    # symlink to the framework binary; resolving it escapes the venv and loses
    # installed packages such as click.
    py = str(python_executable or sys.executable)
    runner = command_runner or _run_subprocess
    env = _benchmark_env(py)
    selected = select_core_cases(default_core_cases(), case_ids=case_ids, limit=limit)

    results: list[CoreBenchmarkCaseResult] = []
    for case in selected:
        repo_path = _prepare_case_repo(case, repos_dir)
        command = build_run_command(
            case=case,
            repo_path=repo_path,
            python_executable=py,
            config_path=config_path,
            provider=provider,
            model=model,
            max_steps=max_steps,
            verify_timeout=verify_timeout,
        )
        started = time.time()
        process = runner(command, root, env, case_timeout)
        duration = time.time() - started
        result = evaluate_case_result(
            case=case,
            process=process,
            repo_path=repo_path,
            project_root=root,
            duration_seconds=duration,
        )
        results.append(result)
        if fail_fast and not result.passed:
            break

    passed_count = sum(1 for result in results if result.passed)
    run_result = CoreBenchmarkRunResult(
        run_id=run_id,
        work_dir=str(run_dir),
        summary_path=str(summary_path),
        total=len(results),
        passed=passed_count,
        failed=len(results) - passed_count,
        cases=results,
    )
    summary_path.write_text(
        json.dumps(run_result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_result


def build_run_command(
    *,
    case: CoreBenchmarkCase,
    repo_path: str | Path,
    python_executable: str | Path,
    config_path: str | Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_steps: int | None = None,
    verify_timeout: int = 300,
) -> list[str]:
    """Build the `python -m entry.cli run ...` command for one case."""
    command = [str(python_executable), "-m", "entry.cli"]
    if config_path:
        command.extend(["--config", str(config_path)])
    command.extend([
        "run",
        "--repo",
        str(repo_path),
        "--mode",
        case.mode,
        "--task",
        case.task,
        "--verify-timeout",
        str(verify_timeout),
    ])
    if provider:
        command.extend(["--provider", provider])
    if model:
        command.extend(["--model", model])
    if max_steps is not None:
        command.extend(["--max-steps", str(max_steps)])
    for verify_command in case.verify_commands:
        command.extend(["--verify", verify_command])
    if case.fail_on_unverified:
        command.append("--fail-on-unverified")
    return command


def evaluate_case_result(
    *,
    case: CoreBenchmarkCase,
    process: CommandExecutionResult,
    repo_path: str | Path,
    project_root: str | Path,
    duration_seconds: float,
) -> CoreBenchmarkCaseResult:
    """Evaluate one completed case against its expected outcomes."""
    repo = Path(repo_path).resolve()
    root = Path(project_root).resolve()
    stdout = process.stdout or ""
    stderr = process.stderr or ""
    report_path = parse_report_path(stdout, project_root=root)
    report = _load_report(report_path)
    actual_changed_files = changed_files_from_git_status(repo)
    report_changed_files = _report_changed_files(report)
    failures = _check_expectations(
        case=case,
        process=process,
        report=report,
        report_path=report_path,
        actual_changed_files=actual_changed_files,
    )

    verification = report.get("verification", {}) if report else {}
    result = report.get("result", {}) if report else {}
    return CoreBenchmarkCaseResult(
        case_id=case.id,
        name=case.name,
        passed=not failures,
        failures=failures,
        exit_code=process.returncode,
        repo_path=str(repo),
        report_path=str(report_path) if report_path else None,
        artifact_dir=str(report_path.parent) if report_path else None,
        duration_seconds=round(duration_seconds, 3),
        permission_mode=report.get("permission_mode") if report else None,
        result_status=result.get("status"),
        verification_status=verification.get("status"),
        verification_passed=verification.get("passed"),
        changed_files=actual_changed_files,
        report_changed_files=report_changed_files,
        stdout_tail=_tail(stdout),
        stderr_tail=_tail(stderr),
    )


def parse_report_path(stdout: str, *, project_root: str | Path) -> Path | None:
    """Extract the report path printed by `entry.cli run`."""
    cleaned = _ANSI_RE.sub("", stdout)
    matches = _REPORT_LINE_RE.findall(cleaned)
    if not matches:
        return None
    path = Path(matches[-1].strip())
    if not path.is_absolute():
        path = Path(project_root) / path
    return path.resolve(strict=False)


def changed_files_from_git_status(repo_path: str | Path) -> list[str]:
    """Return tracked/untracked files currently changed in the case repo."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    changed: set[str] = set()
    for line in proc.stdout.splitlines():
        if not line:
            continue
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            changed.add(path.strip())
    return sorted(changed)


def _check_expectations(
    *,
    case: CoreBenchmarkCase,
    process: CommandExecutionResult,
    report: dict[str, Any] | None,
    report_path: Path | None,
    actual_changed_files: list[str],
) -> list[str]:
    failures: list[str] = []
    expectation = case.expectation

    if process.timed_out:
        failures.append("case command timed out")
    if expectation.exit_code is not None and process.returncode != expectation.exit_code:
        failures.append(
            f"expected exit code {expectation.exit_code}, got {process.returncode}"
        )
    if expectation.exit_nonzero and process.returncode == 0:
        failures.append("expected non-zero exit code, got 0")

    if report_path is None:
        failures.append("run did not print a Report path")
    elif report is None:
        failures.append(f"report.json was not readable: {report_path}")

    if report is None:
        return failures

    if report.get("permission_mode") != case.mode:
        failures.append(
            f"expected permission_mode {case.mode!r}, got {report.get('permission_mode')!r}"
        )

    result = report.get("result", {})
    verification = report.get("verification", {})
    if (
        expectation.result_status is not None
        and result.get("status") != expectation.result_status
    ):
        failures.append(
            f"expected result.status {expectation.result_status!r}, "
            f"got {result.get('status')!r}"
        )
    if (
        expectation.verification_passed is not None
        and verification.get("passed") is not expectation.verification_passed
    ):
        failures.append(
            f"expected verification.passed {expectation.verification_passed!r}, "
            f"got {verification.get('passed')!r}"
        )
    if (
        expectation.verification_status is not None
        and verification.get("status") != expectation.verification_status
    ):
        failures.append(
            f"expected verification.status {expectation.verification_status!r}, "
            f"got {verification.get('status')!r}"
        )
    if verification.get("blocked_count", 0) < expectation.verification_blocked_min:
        failures.append(
            "expected verification.blocked_count >= "
            f"{expectation.verification_blocked_min}, got "
            f"{verification.get('blocked_count', 0)}"
        )
    if expectation.has_patch is not None and result.get("has_patch") is not expectation.has_patch:
        failures.append(
            f"expected result.has_patch {expectation.has_patch!r}, "
            f"got {result.get('has_patch')!r}"
        )

    actual = tuple(actual_changed_files)
    if expectation.repo_clean is not None:
        is_clean = not actual_changed_files
        if is_clean is not expectation.repo_clean:
            failures.append(
                f"expected repo_clean {expectation.repo_clean!r}, "
                f"got {is_clean!r} with changed files {actual_changed_files}"
            )
    if expectation.changed_files_exact is not None:
        expected = tuple(sorted(expectation.changed_files_exact))
        if actual != expected:
            failures.append(
                f"expected changed files exactly {list(expected)}, "
                f"got {actual_changed_files}"
            )
    for path in expectation.changed_files_include:
        if path not in actual_changed_files:
            failures.append(f"expected changed file {path!r} was missing")
    for path in expectation.changed_files_exclude:
        if path in actual_changed_files:
            failures.append(f"forbidden changed file {path!r} was present")

    return failures


def _prepare_case_repo(case: CoreBenchmarkCase, repos_dir: Path) -> Path:
    repo_path = repos_dir / _safe_path_name(case.id)
    if repo_path.exists():
        shutil.rmtree(repo_path)
    repo_path.mkdir(parents=True, exist_ok=True)
    case.setup(repo_path)
    return repo_path


def _setup_basic_python_fix(repo_path: Path) -> None:
    _write_files(
        repo_path,
        {
            ".gitignore": _python_gitignore(),
            "calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": (
                "from calc import add\n\n\n"
                "def test_add_positive_numbers():\n"
                "    assert add(2, 3) == 5\n\n\n"
                "def test_add_negative_numbers():\n"
                "    assert add(-2, -3) == -5\n"
            ),
        },
    )
    _commit_all(repo_path, "Initial broken calculator")


def _setup_multi_file_python_fix(repo_path: Path) -> None:
    _write_files(
        repo_path,
        {
            ".gitignore": _python_gitignore(),
            "store/__init__.py": "",
            "store/cart.py": (
                "from store.pricing import discounted_total\n\n\n"
                "def checkout_total(items, discount_rate):\n"
                "    prices = [item['price'] for item in items]\n"
                "    return discounted_total(prices, discount_rate)\n"
            ),
            "store/pricing.py": (
                "def discounted_total(prices, discount_rate):\n"
                "    subtotal = sum(prices)\n"
                "    return subtotal * discount_rate\n"
            ),
            "tests/test_cart.py": (
                "from store.cart import checkout_total\n\n\n"
                "def test_checkout_total_applies_discount_rate():\n"
                "    items = [{'price': 10}, {'price': 30}]\n"
                "    assert checkout_total(items, 0.25) == 30\n"
            ),
        },
    )
    _commit_all(repo_path, "Initial broken cart pricing")


def _setup_inspect_readonly(repo_path: Path) -> None:
    _write_files(
        repo_path,
        {
            ".gitignore": _python_gitignore(),
            "calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": (
                "from calc import add\n\n\n"
                "def test_add():\n"
                "    assert add(10, 7) == 17\n"
            ),
        },
    )
    _commit_all(repo_path, "Initial broken calculator")


def _setup_passing_calculator(repo_path: Path) -> None:
    _write_files(
        repo_path,
        {
            ".gitignore": _python_gitignore(),
            "calc.py": "def add(a, b):\n    return a + b\n",
            "test_calc.py": (
                "from calc import add\n\n\n"
                "def test_add():\n"
                "    assert add(10, 7) == 17\n"
            ),
        },
    )
    _commit_all(repo_path, "Initial passing calculator")


def _python_gitignore() -> str:
    return "__pycache__/\n.pytest_cache/\n*.pyc\n"


def _write_files(repo_path: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        path = repo_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _commit_all(repo_path: Path, message: str) -> None:
    _run_git(["init"], cwd=repo_path)
    _run_git(["config", "user.email", "eval@example.invalid"], cwd=repo_path)
    _run_git(["config", "user.name", "Forge Core Eval"], cwd=repo_path)
    _run_git(["add", "."], cwd=repo_path)
    _run_git(["commit", "-m", message], cwd=repo_path)


def _run_subprocess(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> CommandExecutionResult:
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandExecutionResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _decode_timeout_output(exc.stdout)
        stderr = _decode_timeout_output(exc.stderr)
        return CommandExecutionResult(
            returncode=-1,
            stdout=stdout,
            stderr=stderr + f"\nCommand timed out after {timeout}s",
            timed_out=True,
        )


def _run_git(args: list[str], *, cwd: Path, timeout: int = 60) -> None:
    proc = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        rendered = " ".join(["git"] + args)
        output = (proc.stdout + proc.stderr).strip()
        raise RuntimeError(f"{rendered} failed in {cwd}: {output}")


def _benchmark_env(python_executable: str | Path) -> dict[str, str]:
    env = os.environ.copy()
    python_bin = str(Path(python_executable).expanduser().parent)
    env["PATH"] = python_bin + os.pathsep + env.get("PATH", "")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _load_report(report_path: Path | None) -> dict[str, Any] | None:
    if report_path is None or not report_path.exists():
        return None
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _report_changed_files(report: dict[str, Any] | None) -> list[str]:
    if not report:
        return []
    changed = report.get("changed_files")
    if not isinstance(changed, list):
        return []
    return sorted(str(path) for path in changed)


def _parse_case_ids(raw: Sequence[str] | None) -> list[str]:
    if not raw:
        return []
    parsed: list[str] = []
    for item in raw:
        for part in re.split(r"[\s,]+", item.strip()):
            if part:
                parsed.append(part)
    return parsed


def _safe_path_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", value).strip("_") or "case"


def _tail(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
