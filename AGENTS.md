# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11 package for an autonomous coding agent. Core execution lives in `agent/` (`core.py`, `task.py`, `event_log.py`, prompts). LLM adapters are in `llm/`, tools in `tools/`, context builders in `context/`, CLI entry points in `entry/`, and configuration in `config/`. Tests live in `tests/`. Runtime logs under `logs/` are ignored. Keep `README.md` and `USAGE.md` in sync with CLI changes.

## Build, Test, and Development Commands

Create a local environment and install editable development dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Common commands:

```bash
pytest
pytest tests/test_path_guard.py
pytest --cov
python smoke_test.py
forgeagent --help
python -m entry.cli run --repo . --task "inspect the project"
```

`pytest` runs the suite. `pytest --cov` reports coverage for `agent`, `llm`, `tools`, `context`, and `entry`. `smoke_test.py` checks end-to-end wiring. The installed console script is `forgeagent`; use `python -m entry.cli ...` when testing directly from source.

## Coding Style & Naming Conventions

Use 4-space indentation, type hints, dataclasses where appropriate, and `from __future__ import annotations` in new modules. Follow existing patterns, such as `BaseTool.execute()` returning `ToolResult` instead of raising user-facing errors. Use `snake_case` for functions, methods, variables, and modules; use `PascalCase` for classes; keep tool names stable and lowercase, for example `file_read` or `git_status`. No formatter is configured, so keep imports tidy and match nearby style.

## Testing Guidelines

Tests use pytest and are named `tests/test_*.py`. Add focused tests near the changed behavior, using `tmp_path`, mocks, or fake backends to avoid network and filesystem side effects. For tool changes, cover success, validation errors, and boundary/security failures. Run the relevant test file before submitting; run full `pytest` for shared behavior.

## Commit & Pull Request Guidelines

Recent commits use short, imperative summaries such as `Harden tool execution validation` or `Add workspace path boundary checks`. Keep commits focused and avoid mixing refactors with behavior changes. Pull requests should describe the problem, summarize the solution, list tests run, and call out security or configuration impacts. Link issues when applicable.

## Security & Configuration Tips

Do not commit API keys, `.env*`, virtualenvs, logs, caches, or generated egg metadata. Store provider secrets in environment variables referenced by `config/default.yaml`. Preserve workspace boundary checks and confirmation behavior when adding file, shell, git, or test tooling.
