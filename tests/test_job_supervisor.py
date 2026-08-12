from __future__ import annotations

import json
import signal
import threading
import time

import pytest

from tweetxvault.config import AppConfig, XDGPaths
from tweetxvault.job_supervisor import JobConflictError, JobSupervisor
from tweetxvault.locking import ProcessLock


def _paths(tmp_path):
    return XDGPaths(config_dir=tmp_path / "config", data_dir=tmp_path, cache_dir=tmp_path / "cache")


def test_supervisor_launches_isolated_cli_worker_and_records_exit(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    finished = threading.Event()
    captured = {}

    class Process:
        pid = 4321

        def poll(self):
            return 0 if finished.is_set() else None

        def wait(self, timeout=None):
            assert timeout is None
            finished.wait(2)
            return 0

    def popen(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return Process()

    monkeypatch.setattr("tweetxvault.job_supervisor.subprocess.Popen", popen)
    supervisor = JobSupervisor(paths, AppConfig())

    result = supervisor.start(
        kind="sync",
        cli_args=["sync"],
        origin="web",
        title="tweetxvault sync",
    )

    assert captured["command"][-1] == "sync"
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["env"]["TWEETXVAULT_ACTIVITY_ORIGIN"] == "web"
    assert supervisor.active is True
    finished.set()
    metadata_path = paths.activity_runs_dir / result["run_id"] / "metadata.json"
    for _ in range(100):
        metadata = json.loads(metadata_path.read_text())
        if metadata["status"] == "completed":
            break
        time.sleep(0.01)
    metadata = json.loads(metadata_path.read_text())
    assert metadata["status"] == "completed"
    assert metadata["exit_code"] == 0


def test_supervisor_stops_the_worker_process_group(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    finished = threading.Event()
    signals = []

    class Process:
        pid = 6789

        def poll(self):
            return None

        def wait(self, timeout=None):
            finished.wait(2)
            return 130

    monkeypatch.setattr("tweetxvault.job_supervisor.subprocess.Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(
        "tweetxvault.job_supervisor.os.killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )
    supervisor = JobSupervisor(paths, AppConfig())
    result = supervisor.start(
        kind="sync", cli_args=["sync"], origin="web", title="tweetxvault sync"
    )

    response = supervisor.stop()

    assert response["stopping"] is True
    assert signals == [(6789, signal.SIGINT)]
    finished.set()
    metadata_path = paths.activity_runs_dir / result["run_id"] / "metadata.json"
    for _ in range(100):
        metadata = json.loads(metadata_path.read_text())
        if metadata["status"] == "stopped":
            break
        time.sleep(0.01)
    assert metadata["status"] == "stopped"


def test_supervisor_rejects_a_live_external_pipeline(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    paths.activity_status_file.write_text(
        json.dumps({"running": True, "pid": 2468}),
        encoding="utf-8",
    )
    monkeypatch.setattr("tweetxvault.job_supervisor.os.kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        "tweetxvault.job_supervisor.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("worker should not launch"),
    )
    supervisor = JobSupervisor(paths, AppConfig())

    with pytest.raises(JobConflictError, match="already running"):
        supervisor.start(
            kind="sync",
            cli_args=["sync"],
            origin="schedule",
            title="tweetxvault sync",
        )


def test_supervisor_rejects_lifecycle_lock_without_relying_on_snapshot(
    tmp_path, monkeypatch
) -> None:
    paths = _paths(tmp_path)
    lock = ProcessLock(paths.command_lock_file)
    lock.acquire()
    monkeypatch.setattr(
        "tweetxvault.job_supervisor.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("worker should not launch"),
    )
    supervisor = JobSupervisor(paths, AppConfig())
    try:
        with pytest.raises(JobConflictError, match="already running"):
            supervisor.start(
                kind="sync",
                cli_args=["sync"],
                origin="schedule",
                title="tweetxvault sync",
            )
    finally:
        lock.release()
