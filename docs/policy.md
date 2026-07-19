# Forge Policy Design

`forge-policy.yaml` is a deferred advanced configuration format for Forge
Agent. It is not wired into runtime behavior yet, and it is not part of the
core user flow.

The core flow is:

```text
forgeagent run
  -> built-in safety checks
  -> event log
  -> report.json / diff.patch
```

Policy files should come after stable run artifacts, built-in permission modes,
and verification reporting.

---

## Position

This file is for repository or team governance, not per-task planning.

It should express durable boundaries such as:

- files the agent must never read
- shell commands the agent must never run
- default permission mode for the repository
- operations that require confirmation in non-local workflows

It should not express:

- a generated Task Contract
- a predicted command list for one task
- natural-language instructions to the model
- permissions that can override built-in hard-deny rules

---

## Current Priority Order

Forge Agent is implementing these in order:

1. Stable run artifacts are done: `events.jsonl`, `report.json`, `diff.patch`.
2. Built-in permission modes are done: `inspect`, `fix`, `maintain`.
3. Verification reporting is done: explicit verify commands and
   machine-readable verification status.
4. Optional policy file support for advanced team governance is next.

---

## Proposed Shape

```yaml
version: 1

default_mode: fix

hard_deny:
  files:
    read:
      - ".env"
      - ".env.*"
      - "*.pem"
      - "*.key"
      - "id_rsa*"
      - ".git/config"
      - ".git-credentials"
      - "logs/*.jsonl"
  shell:
    - "rm -rf"
    - "mkfs"
    - "dd if="
    - "git push"
    - "curl"
    - "wget"
    - "sudo"

modes:
  inspect:
    files:
      write: deny
    shell:
      allow:
        - "ls"
        - "pwd"
        - "cat"
        - "head"
        - "tail"
        - "rg"
        - "find"
        - "git status"
        - "git diff"
        - "git log"
      default: confirm
    network: deny
    git:
      commit: deny

  fix:
    files:
      allow_write:
        - "**"
      confirm_write:
        - "pyproject.toml"
        - "setup.py"
        - "requirements*.txt"
        - "package*.json"
        - ".github/workflows/*"
        - "deploy*"
        - "release*"
    shell:
      allow:
        - "ls"
        - "pwd"
        - "rg"
        - "git status"
        - "git diff"
        - "pytest"
        - "python -m pytest"
      deny:
        - "pip install"
        - "python -m pip install"
        - "npm install"
        - "pnpm install"
        - "uv pip install"
        - "git push"
        - "curl"
        - "wget"
        - "sudo"
        - "docker"
        - "docker-compose"
      default: confirm
    network: deny
    git:
      add:
        explicit_paths_only: true
        confirm_unmodified_by_agent: true
      commit: confirm

  maintain:
    files:
      allow_write:
        - "**"
      confirm_write:
        - "pyproject.toml"
        - "setup.py"
        - "requirements*.txt"
        - "package*.json"
        - ".github/workflows/*"
        - "deploy*"
        - "release*"
    shell:
      allow:
        - "ls"
        - "pwd"
        - "rg"
        - "git status"
        - "git diff"
        - "pytest"
        - "python -m pytest"
      confirm:
        - "pip install"
        - "python -m pip install"
        - "npm install"
        - "pnpm install"
        - "uv pip install"
      deny:
        - "git push"
        - "curl"
        - "wget"
        - "sudo"
        - "docker"
        - "docker-compose"
      default: confirm
    network: deny
    git:
      add:
        explicit_paths_only: true
        confirm_unmodified_by_agent: true
      commit: confirm
```

---

## Semantics

### `default_mode`

The repository-level default permission mode when the CLI user does not pass a
mode explicitly. CLI arguments should override this default, unless an
organization-managed policy later forbids a mode.

### `hard_deny`

Hard-deny rules are mandatory. No permission mode, CLI grant, or future policy
extension can allow something matched here.

This keeps the security model understandable:

```text
hard deny beats everything
mode decides the baseline
runtime confirmation handles unresolved actions
```

### `modes`

Modes are reusable permission bundles. They are not plans.

- `inspect`: read-only analysis and low-risk read commands.
- `fix`: normal code and test edits; dependency installs, raw network commands,
  remote git pushes, sudo, and docker are denied.
- `maintain`: maintenance workflow where dependency installs are routed to
  confirmation; raw network commands, remote git pushes, sudo, and docker remain
  denied.

### Runtime Evaluation

Every tool call should still be evaluated at runtime:

```text
tool call
  -> built-in hard blocks
  -> workspace and sensitive path checks
  -> hard_deny
  -> active mode
  -> confirmation if needed
  -> execute or deny
  -> event log
```

The model may choose a tool call, but it does not authorize that tool call.

---

## First Implementation Scope

When policy loading is eventually implemented, keep the first version small:

- parse YAML with clear errors
- support `default_mode`
- support `hard_deny.files.read`
- support `hard_deny.shell`
- support project overrides for built-in `inspect`, `fix`, `maintain`
- allow a project file to narrow or confirm more actions
- never let project policy loosen built-in hard-deny rules

Do not add roles, users, remote approval services, time windows, or generated
per-task contracts in the first version.

---

## Validation Checklist

Policy loading should be covered by tests for:

- missing policy file falls back to built-in defaults
- malformed YAML fails with a clear error
- unknown top-level keys fail or warn consistently
- hard deny beats allow
- hard-blocked shell commands cannot be allowed by policy
- outside workspace access remains fail-closed without confirmation
- sensitive file reads remain denied
- `git add .` remains denied when explicit paths are required
- `inspect` rejects writes
- `fix` denies package installs
- `maintain` requires confirmation for package installs
