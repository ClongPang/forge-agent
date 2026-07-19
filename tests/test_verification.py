from __future__ import annotations

from agent.verification import run_verifications


def test_run_verifications_passes_explicit_read_command(tmp_path):
    report = run_verifications(
        ["printf verify-ok"],
        repo_path=tmp_path,
        timeout=10,
    )

    assert report["requested"] is True
    assert report["passed"] is True
    assert report["status"] == "passed"
    assert report["commands"][0]["status"] == "passed"
    assert "verify-ok" in report["commands"][0]["output"]


def test_run_verifications_records_failed_command(tmp_path):
    report = run_verifications(
        ["false"],
        repo_path=tmp_path,
        timeout=10,
    )

    assert report["requested"] is True
    assert report["passed"] is False
    assert report["status"] == "failed"
    assert report["failed_count"] == 1
    assert report["commands"][0]["returncode"] != 0


def test_run_verifications_blocks_non_verification_commands(tmp_path):
    report = run_verifications(
        ["pip install requests"],
        repo_path=tmp_path,
        timeout=10,
    )

    assert report["requested"] is True
    assert report["passed"] is False
    assert report["blocked_count"] == 1
    assert report["commands"][0]["status"] == "blocked"
    assert "not read/test-like" in report["commands"][0]["error"]


def test_run_verifications_rejects_shell_control(tmp_path):
    report = run_verifications(
        ["pytest && curl https://example.com"],
        repo_path=tmp_path,
        timeout=10,
    )

    assert report["passed"] is False
    assert report["commands"][0]["status"] == "blocked"
    assert "single command" in report["commands"][0]["error"]
