from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tweetxvault.config import AppConfig, ScheduleConfig, XDGPaths
from tweetxvault.job_supervisor import JobConflictError
from tweetxvault.scheduler import ScheduleManager, next_run_after


@pytest.mark.parametrize(
    ("config", "start", "expected"),
    [
        (
            ScheduleConfig(cadence="hours", every_hours=6),
            datetime(2026, 8, 12, 10, 30, tzinfo=UTC),
            datetime(2026, 8, 12, 16, 30, tzinfo=UTC),
        ),
        (
            ScheduleConfig(cadence="daily", time="03:00", timezone="UTC"),
            datetime(2026, 8, 12, 10, 30, tzinfo=UTC),
            datetime(2026, 8, 13, 3, 0, tzinfo=UTC),
        ),
        (
            ScheduleConfig(cadence="weekly", weekday=0, time="09:15", timezone="UTC"),
            datetime(2026, 8, 12, 10, 30, tzinfo=UTC),
            datetime(2026, 8, 17, 9, 15, tzinfo=UTC),
        ),
        (
            ScheduleConfig(cadence="monthly", day_of_month=31, time="04:00", timezone="UTC"),
            datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
            datetime(2026, 9, 30, 4, 0, tzinfo=UTC),
        ),
    ],
)
def test_next_run_supports_all_schedule_cadences(config, start, expected) -> None:
    assert next_run_after(config, start) == expected


def test_scheduler_launches_due_sync_and_advances_next_run(tmp_path) -> None:
    paths = XDGPaths(
        config_dir=tmp_path / "config", data_dir=tmp_path, cache_dir=tmp_path / "cache"
    )
    config = AppConfig(
        schedule=ScheduleConfig(enabled=True, cadence="hours", every_hours=2, timezone="UTC")
    )

    class Supervisor:
        def __init__(self):
            self.config = config

        def start(self, **kwargs):
            assert kwargs["cli_args"] == ["sync"]
            assert kwargs["origin"] == "schedule"
            return {"run_id": "scheduled-run"}

    manager = ScheduleManager(paths, config, Supervisor())
    manager._state["next_run_at"] = 1_000.0
    manager.tick(now=1_001.0)

    assert manager._state["last_result"] == "started"
    assert manager._state["last_run_id"] == "scheduled-run"
    assert manager._state["next_run_at"] == 1_001.0 + 2 * 3600


def test_scheduler_records_conflicting_run_as_skipped(tmp_path) -> None:
    paths = XDGPaths(
        config_dir=tmp_path / "config", data_dir=tmp_path, cache_dir=tmp_path / "cache"
    )
    config = AppConfig(schedule=ScheduleConfig(enabled=True, cadence="daily", timezone="UTC"))

    class Supervisor:
        def __init__(self):
            self.config = config

        def start(self, **_kwargs):
            raise JobConflictError("busy")

    manager = ScheduleManager(paths, config, Supervisor())
    manager._state["next_run_at"] = 1_000.0
    manager.tick(now=1_001.0)

    assert manager._state["last_result"] == "skipped: another command was running"
