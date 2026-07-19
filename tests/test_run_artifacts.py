from __future__ import annotations

import json

from agent.event_log import EventLog
from agent.run_artifacts import write_run_artifacts
from agent.task import RunResult, RunStatus, Task


def test_write_run_artifacts_includes_patch_and_changed_files(tmp_path):
    task = Task(
        task_id="artifact",
        description="fix it",
        repo_path=str(tmp_path),
    )
    patch = "\n".join([
        "diff --git a/src/app.py b/src/app.py",
        "index 1111111..2222222 100644",
        "--- a/src/app.py",
        "+++ b/src/app.py",
        "@@ -1 +1 @@",
        "-old",
        "+new",
    ])
    result = RunResult(
        task_id=task.task_id,
        status=RunStatus.SUCCESS,
        summary="done",
        steps_taken=1,
        total_tokens=12,
        patch=patch,
    )

    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        log.log_task_start(task)
        log.log_task_complete(steps=1, summary="done")
        paths = write_run_artifacts(
            task=task,
            result=result,
            log=log,
            duration_seconds=1.25,
            permission_mode="maintain",
            verification={
                "requested": True,
                "status": "passed",
                "passed": True,
                "commands": [],
            },
        )

    assert paths.events.exists()
    assert paths.report.exists()
    assert paths.diff.read_text(encoding="utf-8") == patch

    report = json.loads(paths.report.read_text(encoding="utf-8"))
    assert report["changed_files"] == ["src/app.py"]
    assert report["permission_mode"] == "maintain"
    assert report["verification"]["passed"] is True
    assert report["result"]["has_patch"] is True
    assert report["result"]["patch_chars"] == len(patch)
    assert report["duration_seconds"] == 1.25
