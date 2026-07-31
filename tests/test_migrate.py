from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tweetxvault.config import AppConfig
from tweetxvault.storage import migrate, open_archive_store


class FakeLegacyTable:
    def __init__(self, total_rows: int) -> None:
        self.total_rows = total_rows

    def count_rows(self) -> int:
        return self.total_rows


class FakeLegacyDatabase:
    def __init__(
        self,
        table: FakeLegacyTable | None = None,
        *,
        open_error: BaseException | None = None,
    ) -> None:
        self.table = table
        self.open_error = open_error
        self.opened_tables: list[str] = []

    def open_table(self, name: str) -> FakeLegacyTable:
        self.opened_tables.append(name)
        if self.open_error:
            raise self.open_error
        assert self.table is not None
        return self.table


class FakeLanceDB:
    def __init__(
        self,
        database: FakeLegacyDatabase | None = None,
        *,
        connect_error: BaseException | None = None,
    ) -> None:
        self.database = database
        self.connect_error = connect_error
        self.connected_paths: list[Path] = []

    def connect(self, path: Path) -> FakeLegacyDatabase:
        self.connected_paths.append(path)
        if self.connect_error:
            raise self.connect_error
        assert self.database is not None
        return self.database


class FakeConnection:
    def __init__(self, *, rebuild_error: BaseException | None = None) -> None:
        self.rebuild_error = rebuild_error
        self.executed: list[str] = []
        self.commits = 0

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self.rebuild_error:
            raise self.rebuild_error

    def commit(self) -> None:
        self.commits += 1


class FakeStore:
    def __init__(
        self,
        events: list[object],
        *,
        connection: FakeConnection | None = None,
        label: str = "store",
    ) -> None:
        self.events = events
        self.conn = connection or FakeConnection()
        self.label = label
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.events.append(("close", self.label))


class FakeProgress:
    def __init__(self, total: int) -> None:
        self.total = total
        self.n = 0
        self.updates: list[int] = []
        self.messages: list[str] = []
        self.closed = False

    def update(self, amount: int) -> None:
        self.updates.append(amount)
        self.n += amount

    def write(self, message: str) -> None:
        self.messages.append(message)

    def close(self) -> None:
        self.closed = True


def worker_result(return_code: int) -> SimpleNamespace:
    return SimpleNamespace(returncode=return_code, stdout=b"", stderr=b"")


def install_legacy_source(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    *,
    total_rows: int,
) -> tuple[AppConfig, Path, FakeLanceDB]:
    config = AppConfig()
    lance_path = paths.data_dir / "archive.lancedb"
    lance_path.mkdir(parents=True)
    legacy = FakeLanceDB(FakeLegacyDatabase(FakeLegacyTable(total_rows)))
    monkeypatch.setattr(migrate, "load_config", lambda: (config, paths))
    monkeypatch.setattr(migrate, "_import_lancedb", lambda: legacy)
    return config, lance_path, legacy


def install_fake_stores(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[object],
    final_connection: FakeConnection | None = None,
) -> tuple[list[tuple[bool, Any, Any]], list[FakeStore]]:
    calls: list[tuple[bool, Any, Any]] = []
    stores: list[FakeStore] = []

    def fake_open(paths, *, create: bool, config) -> FakeStore:
        calls.append((create, paths, config))
        label = "schema" if not stores else "fts"
        connection = final_connection if stores and final_connection else FakeConnection()
        store = FakeStore(events, connection=connection, label=label)
        stores.append(store)
        events.append(("open", label, create))
        return store

    monkeypatch.setattr(migrate, "open_archive_store", fake_open)
    return calls, stores


def install_worker_sequence(
    monkeypatch: pytest.MonkeyPatch,
    return_codes: list[int],
    *,
    events: list[object] | None = None,
) -> list[tuple[Path, Path, int, int]]:
    calls: list[tuple[Path, Path, int, int]] = []
    remaining = list(return_codes)

    def fake_worker(
        lance_path: Path,
        database_path: Path,
        batch_size: int,
        offset: int,
    ) -> SimpleNamespace:
        calls.append((lance_path, database_path, batch_size, offset))
        if events is not None:
            events.append(("worker", offset))
        return worker_result(remaining.pop(0))

    monkeypatch.setattr(migrate, "_run_worker", fake_worker)
    return calls


def test_worker_code_executes_batch_and_preserves_existing_rows(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    config = AppConfig()
    store = open_archive_store(paths, create=True, config=config)
    assert store is not None
    store.conn.execute(
        """
        INSERT INTO archive (row_key, record_type, tweet_id, text)
        VALUES ('tweet_object:1', 'tweet_object', '1', 'newer SQLite value')
        """
    )
    store.conn.commit()
    store.close()

    class FakeSearch:
        def __init__(self) -> None:
            self.limit_value: int | None = None
            self.offset_value: int | None = None

        def limit(self, value: int) -> FakeSearch:
            self.limit_value = value
            return self

        def offset(self, value: int) -> FakeSearch:
            self.offset_value = value
            return self

        def to_list(self) -> list[dict[str, object]]:
            assert self.limit_value == 25
            assert self.offset_value == 75
            return [
                {
                    "row_key": "tweet_object:1",
                    "record_type": "tweet_object",
                    "tweet_id": "1",
                    "text": "legacy value must be ignored",
                },
                {
                    "row_key": "tweet_object:2",
                    "record_type": "tweet_object",
                    "tweet_id": "2",
                    "text": "migrated value",
                    "unknown_legacy_field": "ignored",
                },
            ]

    search = FakeSearch()
    table = SimpleNamespace(search=lambda: search)
    database = SimpleNamespace(
        open_table=lambda name: table
        if name == "archive"
        else pytest.fail(f"unexpected table: {name}")
    )
    connected_paths: list[str] = []
    fake_lancedb = SimpleNamespace(connect=lambda path: connected_paths.append(path) or database)
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "-c",
            str(paths.data_dir / "archive.lancedb"),
            str(paths.database_path),
            "25",
            "75",
        ],
    )

    with pytest.raises(SystemExit) as exit_info:
        exec(compile(migrate.WORKER_CODE, "<migration-worker>", "exec"), {"__name__": "__main__"})

    assert exit_info.value.code == 0
    assert connected_paths == [str(paths.data_dir / "archive.lancedb")]
    connection = sqlite3.connect(paths.database_path)
    try:
        rows = connection.execute(
            """
            SELECT row_key, text
            FROM archive
            WHERE row_key IN ('tweet_object:1', 'tweet_object:2')
            ORDER BY row_key
            """
        ).fetchall()
    finally:
        connection.close()
    assert rows == [
        ("tweet_object:1", "newer SQLite value"),
        ("tweet_object:2", "migrated value"),
    ]


def test_worker_code_empty_batch_exits_without_opening_destination(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    search = SimpleNamespace(
        limit=lambda value: SimpleNamespace(
            offset=lambda offset: SimpleNamespace(to_list=lambda: [])
        )
    )
    fake_lancedb = SimpleNamespace(
        connect=lambda path: SimpleNamespace(
            open_table=lambda name: SimpleNamespace(search=lambda: search)
        )
    )
    monkeypatch.setitem(sys.modules, "lancedb", fake_lancedb)
    database_path = paths.data_dir / "must-not-be-created.sqlite"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "-c",
            str(paths.data_dir / "archive.lancedb"),
            str(database_path),
            "5000",
            "0",
        ],
    )

    with pytest.raises(SystemExit) as exit_info:
        exec(compile(migrate.WORKER_CODE, "<migration-worker>", "exec"), {"__name__": "__main__"})

    assert exit_info.value.code == migrate.WORKER_END_OF_TABLE
    assert not database_path.exists()


def test_module_does_not_require_lancedb_until_migration_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_import(name: str) -> None:
        assert name == "lancedb"
        raise ImportError("not installed")

    monkeypatch.setattr(migrate.importlib, "import_module", missing_import)

    assert migrate._import_lancedb() is None
    compile(migrate.WORKER_CODE, "<migration-worker>", "exec")


def test_missing_legacy_archive_returns_without_loading_lancedb(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(migrate, "load_config", lambda: (AppConfig(), paths))
    monkeypatch.setattr(
        migrate,
        "_import_lancedb",
        lambda: pytest.fail("legacy dependency loaded without a source archive"),
    )

    result = migrate.run_migration()

    assert result.status == "source_missing"
    assert "No old LanceDB archive found" in capsys.readouterr().out


def test_missing_optional_dependency_returns_result_instead_of_exiting(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = AppConfig()
    (paths.data_dir / "archive.lancedb").mkdir()
    monkeypatch.setattr(migrate, "load_config", lambda: (config, paths))
    monkeypatch.setattr(migrate, "_import_lancedb", lambda: None)
    monkeypatch.setattr(
        migrate,
        "open_archive_store",
        lambda *args, **kwargs: pytest.fail("destination opened without lancedb"),
    )

    result = migrate.run_migration()

    assert result.status == "dependency_missing"
    output = capsys.readouterr().out
    assert "lancedb and pyarrow are required" in output
    assert "reinstall tweetxvault" in output


@pytest.mark.parametrize(
    "legacy",
    [
        FakeLanceDB(connect_error=RuntimeError("cannot connect")),
        FakeLanceDB(
            FakeLegacyDatabase(open_error=KeyError("archive")),
        ),
    ],
)
def test_missing_or_unreadable_archive_table_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    capsys: pytest.CaptureFixture[str],
    legacy: FakeLanceDB,
) -> None:
    (paths.data_dir / "archive.lancedb").mkdir()
    monkeypatch.setattr(migrate, "load_config", lambda: (AppConfig(), paths))
    monkeypatch.setattr(migrate, "_import_lancedb", lambda: legacy)
    monkeypatch.setattr(
        migrate,
        "open_archive_store",
        lambda *args, **kwargs: pytest.fail("destination opened without a legacy table"),
    )

    result = migrate.run_migration()

    assert result.status == "table_missing"
    assert "not found or unreadable" in capsys.readouterr().out


def test_destination_schema_is_created_and_closed_before_first_worker(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    config, _, _ = install_legacy_source(monkeypatch, paths, total_rows=1)
    events: list[object] = []
    store_calls, stores = install_fake_stores(monkeypatch, events=events)
    worker_calls = install_worker_sequence(
        monkeypatch,
        [0, migrate.WORKER_END_OF_TABLE],
        events=events,
    )
    monkeypatch.setattr(migrate, "_create_progress", lambda total: None)

    result = migrate.run_migration()

    assert result.status == "complete"
    assert events[:3] == [
        ("open", "schema", True),
        ("close", "schema"),
        ("worker", 0),
    ]
    assert store_calls[0] == (True, paths, config)
    assert stores[0].closed is True
    assert worker_calls[0][1] == paths.database_path


@pytest.mark.parametrize(
    ("total_rows", "return_codes", "expected_offsets", "expected_updates"),
    [
        (3, [0, migrate.WORKER_END_OF_TABLE], [0, 5000], [3]),
        (
            12_000,
            [0, 0, 0, migrate.WORKER_END_OF_TABLE],
            [0, 5000, 10_000, 15_000],
            [5000, 5000, 2000],
        ),
    ],
)
def test_single_and_multi_batch_offsets_and_progress(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    total_rows: int,
    return_codes: list[int],
    expected_offsets: list[int],
    expected_updates: list[int],
) -> None:
    install_legacy_source(monkeypatch, paths, total_rows=total_rows)
    events: list[object] = []
    install_fake_stores(monkeypatch, events=events)
    worker_calls = install_worker_sequence(monkeypatch, return_codes)
    progress = FakeProgress(total_rows)
    monkeypatch.setattr(migrate, "_create_progress", lambda total: progress)

    result = migrate.run_migration()

    assert result.status == "complete"
    assert result.total_rows == total_rows
    assert result.migrated_rows == total_rows
    assert result.skipped_rows == 0
    assert result.final_offset == expected_offsets[-1]
    assert [call[3] for call in worker_calls] == expected_offsets
    assert progress.updates == expected_updates
    assert progress.n == total_rows
    assert progress.closed is True


def test_worker_end_code_stops_immediately_without_advancing_progress(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    install_legacy_source(monkeypatch, paths, total_rows=0)
    install_fake_stores(monkeypatch, events=[])
    worker_calls = install_worker_sequence(
        monkeypatch,
        [migrate.WORKER_END_OF_TABLE],
    )
    progress = FakeProgress(0)
    monkeypatch.setattr(migrate, "_create_progress", lambda total: progress)

    result = migrate.run_migration()

    assert result.worker_calls == 1
    assert result.final_offset == 0
    assert result.migrated_rows == 0
    assert worker_calls[0][3] == 0
    assert progress.updates == []
    assert progress.closed is True


def test_corrupted_chunk_is_skipped_then_success_resets_failure_count(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    install_legacy_source(monkeypatch, paths, total_rows=10_000)
    install_fake_stores(monkeypatch, events=[])
    worker_calls = install_worker_sequence(
        monkeypatch,
        [1, 0, migrate.WORKER_END_OF_TABLE],
    )
    progress = FakeProgress(10_000)
    monkeypatch.setattr(migrate, "_create_progress", lambda total: progress)

    result = migrate.run_migration()

    assert result.status == "complete"
    assert result.skipped_rows == 5000
    assert result.migrated_rows == 5000
    assert [call[3] for call in worker_calls] == [0, 5000, 10_000]
    assert progress.updates == [5000, 5000]
    assert len(progress.messages) == 1
    assert "offset 0" in progress.messages[0]


def test_corrupted_chunk_without_tqdm_uses_plain_warning(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_legacy_source(monkeypatch, paths, total_rows=5000)
    install_fake_stores(monkeypatch, events=[])
    worker_calls = install_worker_sequence(
        monkeypatch,
        [7, migrate.WORKER_END_OF_TABLE],
    )
    monkeypatch.setattr(migrate, "_create_progress", lambda total: None)

    result = migrate.run_migration()

    assert result.status == "complete"
    assert result.skipped_rows == 5000
    assert [call[3] for call in worker_calls] == [0, 5000]
    assert "Corrupted chunk detected at offset 0" in capsys.readouterr().out


def test_more_than_one_hundred_consecutive_failures_aborts(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_legacy_source(monkeypatch, paths, total_rows=1_000_000)
    install_fake_stores(monkeypatch, events=[])
    offsets: list[int] = []

    def failed_worker(
        lance_path: Path,
        database_path: Path,
        batch_size: int,
        offset: int,
    ) -> SimpleNamespace:
        offsets.append(offset)
        return worker_result(1)

    monkeypatch.setattr(migrate, "_run_worker", failed_worker)
    monkeypatch.setattr(migrate, "_create_progress", lambda total: None)

    result = migrate.run_migration()

    assert result.status == "aborted"
    assert result.worker_calls == 101
    assert offsets == list(range(0, 505_000, 5000))
    assert result.final_offset == 500_000
    assert result.skipped_rows == 505_000
    output = capsys.readouterr().out
    assert "Too many consecutive failures" in output
    assert "Migration stopped before all readable chunks were processed" in output
    assert "Migration complete!" not in output


def test_worker_launch_exception_is_treated_as_corrupted_chunk(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    install_legacy_source(monkeypatch, paths, total_rows=5000)
    install_fake_stores(monkeypatch, events=[])
    attempts = 0

    def flaky_worker(*args: Any, **kwargs: Any) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("cannot spawn")
        return worker_result(migrate.WORKER_END_OF_TABLE)

    monkeypatch.setattr(migrate, "_run_worker", flaky_worker)
    progress = FakeProgress(5000)
    monkeypatch.setattr(migrate, "_create_progress", lambda total: progress)

    result = migrate.run_migration()

    assert result.status == "complete"
    assert result.skipped_rows == 5000
    assert result.worker_calls == 2
    assert len(progress.messages) == 2
    assert "cannot spawn" in progress.messages[0]
    assert "Corrupted chunk" in progress.messages[1]


def test_fts_rebuild_commits_and_closes_destination(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    install_legacy_source(monkeypatch, paths, total_rows=0)
    events: list[object] = []
    final_connection = FakeConnection()
    store_calls, stores = install_fake_stores(
        monkeypatch,
        events=events,
        final_connection=final_connection,
    )
    install_worker_sequence(monkeypatch, [migrate.WORKER_END_OF_TABLE])
    monkeypatch.setattr(migrate, "_create_progress", lambda total: None)

    result = migrate.run_migration()

    assert result.fts_rebuilt is True
    assert final_connection.executed == ["INSERT INTO archive_fts(archive_fts) VALUES('rebuild')"]
    assert final_connection.commits == 1
    assert store_calls[-1][0] is True
    assert stores[-1].closed is True


def test_fts_rebuild_failure_warns_and_still_closes_store(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_legacy_source(monkeypatch, paths, total_rows=0)
    events: list[object] = []
    final_connection = FakeConnection(rebuild_error=sqlite3.OperationalError("no fts5"))
    _, stores = install_fake_stores(
        monkeypatch,
        events=events,
        final_connection=final_connection,
    )
    install_worker_sequence(monkeypatch, [migrate.WORKER_END_OF_TABLE])
    monkeypatch.setattr(migrate, "_create_progress", lambda total: None)

    result = migrate.run_migration()

    assert result.status == "complete"
    assert result.fts_rebuilt is False
    assert stores[-1].closed is True
    assert "Failed to rebuild FTS index: no fts5" in capsys.readouterr().out


@pytest.mark.parametrize("open_behavior", ["none", "error"])
def test_destination_initialization_failure_prevents_workers(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    capsys: pytest.CaptureFixture[str],
    open_behavior: str,
) -> None:
    install_legacy_source(monkeypatch, paths, total_rows=10)

    def failed_open(*args: Any, **kwargs: Any) -> None:
        if open_behavior == "error":
            raise sqlite3.OperationalError("disk full")
        return None

    monkeypatch.setattr(migrate, "open_archive_store", failed_open)
    monkeypatch.setattr(
        migrate,
        "_run_worker",
        lambda *args, **kwargs: pytest.fail("worker started without destination schema"),
    )

    result = migrate.run_migration()

    assert result.status == "destination_failed"
    assert "Failed to" in capsys.readouterr().out


def test_realistic_rerun_preserves_existing_rows_and_backfills_search_data(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    config, _, _ = install_legacy_source(monkeypatch, paths, total_rows=2)
    monkeypatch.setattr(migrate, "_create_progress", lambda total: None)

    existing_store = open_archive_store(paths, create=True, config=config)
    assert existing_store is not None
    existing_store.conn.execute(
        """
        INSERT INTO archive (row_key, record_type, tweet_id, text, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "tweet_object:1",
            "tweet_object",
            "1",
            "preserve newer SQLite text",
            "Sat Mar 14 00:00:00 +0000 2026",
        ),
    )
    existing_store.conn.commit()
    existing_store.close()

    offsets: list[int] = []

    def sqlite_worker(
        lance_path: Path,
        database_path: Path,
        batch_size: int,
        offset: int,
    ) -> SimpleNamespace:
        offsets.append(offset)
        if offset:
            return worker_result(migrate.WORKER_END_OF_TABLE)
        connection = sqlite3.connect(database_path)
        with connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO archive
                    (row_key, record_type, tweet_id, text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        "tweet_object:1",
                        "tweet_object",
                        "1",
                        "legacy text must not overwrite",
                        "Sat Mar 14 00:00:00 +0000 2026",
                    ),
                    (
                        "tweet_object:2",
                        "tweet_object",
                        "2",
                        "newly migrated searchable phrase",
                        "Sun Mar 15 00:00:00 +0000 2026",
                    ),
                ],
            )
        connection.close()
        return worker_result(0)

    monkeypatch.setattr(migrate, "_run_worker", sqlite_worker)

    first = migrate.run_migration()
    second = migrate.run_migration()

    assert first.status == second.status == "complete"
    assert first.fts_rebuilt is second.fts_rebuilt is True
    assert offsets == [0, 5000, 0, 5000]
    assert "INSERT OR IGNORE INTO archive" in migrate.WORKER_CODE

    final_store = open_archive_store(paths, create=False, config=config)
    assert final_store is not None
    try:
        rows = final_store.conn.execute(
            """
            SELECT row_key, text, created_at_ts
            FROM archive
            WHERE row_key IN ('tweet_object:1', 'tweet_object:2')
            ORDER BY row_key
            """
        ).fetchall()
        assert [(row["row_key"], row["text"]) for row in rows] == [
            ("tweet_object:1", "preserve newer SQLite text"),
            ("tweet_object:2", "newly migrated searchable phrase"),
        ]
        assert all(row["created_at_ts"] is not None for row in rows)
        fts_matches = final_store.conn.execute(
            """
            SELECT archive.row_key
            FROM archive_fts
            JOIN archive ON archive.rowid = archive_fts.rowid
            WHERE archive_fts MATCH 'searchable'
            """
        ).fetchall()
        assert [row["row_key"] for row in fts_matches] == ["tweet_object:2"]
    finally:
        final_store.close()
