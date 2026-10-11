import hashlib
import io
import json

import pytest
from sqlalchemy import event, func, select

from backend.scripts import import_source_archive as importer
from backend.services.storage.tiger import TigerStore


def record(**changes):
    return {
        "version": 1,
        "source_url": "https://publisher.example/transcript",
        "text": "A public transcript with sufficient words to index for retrieval.",
        "metadata": {"title": "Speech"},
        "provenance": {"rights_status": "public"},
        **changes,
    }


def archive(*records):
    return io.BytesIO(b"".join(json.dumps(r).encode() + b"\n" for r in records))


class FakeStore:
    def __init__(self):
        self.records = []

    def upsert_transcript_document(self, **kwargs):
        self.records.append(kwargs)
        return True


def test_preserves_exact_text_and_hash():
    store = FakeStore()
    text = "  Transcript café.\nExact spacing.  "
    assert (
        importer.import_archive(archive(record(text=text)), store, io.StringIO())[
            "inserted"
        ]
        == 1
    )
    assert store.records[0]["text"] == text
    assert (
        store.records[0]["provenance"]["sha256"]
        == hashlib.sha256(text.encode()).hexdigest()
    )
    assert store.records[0]["timing_basis"] == "unavailable"


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"extra": "field"},
        {"text": ""},
        {"source_url": "http://127.0.0.1/private"},
        {"source_url": "https://user:secret@publisher.example/"},
        {"metadata": {"start_seconds": 0}},
        {"provenance": {"sha256": "wrong"}},
        {"metadata": {"recording_url": "https://publisher.example/video"}},
        {"provenance": {"transcript_source_url": "http://localhost/"}},
    ],
)
def test_invalid_record_continues_with_sanitized_feedback(changes):
    store, errors = FakeStore(), io.StringIO()
    counts = importer.import_archive(
        archive(record(**changes), record()), store, errors
    )
    assert counts == {"records": 2, "inserted": 1, "unchanged": 0, "failed": 1}
    assert json.loads(errors.getvalue())["line"] == 1
    assert "secret" not in errors.getvalue()


def test_publisher_linked_recording():
    result = importer.validate_record(
        record(
            metadata={"recording_url": "https://publisher.example/video"},
            provenance={"recording_link_basis": "publisher_link"},
        )
    )
    assert result["metadata"]["recording_url"].endswith("/video")


@pytest.mark.parametrize("claimed_date", [None, "2024-02-29", "2026-10-10"])
def test_archive_date_claims_preserved_without_timestamp(claimed_date):
    result = importer.validate_record(
        record(
            provenance={
                "event_date": claimed_date,
                "publication_date": "2026-10-11",
            }
        )
    )
    assert result["provenance"]["event_date"] == claimed_date
    assert result["provenance"]["publication_date"] == "2026-10-11"
    assert "published_at" not in result["metadata"]


@pytest.mark.parametrize("field", ["event_date", "publication_date"])
@pytest.mark.parametrize(
    "claimed_date",
    [
        "2025-02-29",
        "2026-04-31",
        "2026-13-01",
        "20261010",
        "2026-W41-6",
        "2026-1-01",
        "2026-10-10T00:00:00Z",
        "",
        " 2026-10-10",
        20261010,
    ],
)
def test_archive_dates_reject_invalid_or_non_day_precision(field, claimed_date):
    with pytest.raises(ValueError):
        importer.validate_record(record(provenance={field: claimed_date}))


def test_oversize_line_drained_and_next_line_processed(monkeypatch):
    monkeypatch.setattr(importer, "MAX_LINE_BYTES", 512)
    stream = io.BytesIO(b"x" * 2000 + b"\n" + archive(record()).getvalue())
    counts = importer.import_archive(stream, FakeStore(), io.StringIO())
    assert counts["records"] == 2 and counts["failed"] == 1 and counts["inserted"] == 1


def test_invalid_encoding_and_json():
    counts = importer.import_archive(
        io.BytesIO(b"\xff\n{bad}\n"), FakeStore(), io.StringIO()
    )
    assert counts["failed"] == 2


def test_storage_error_does_not_leak_and_is_retriable():
    class BrokenStore:
        def upsert_transcript_document(self, **kwargs):
            raise RuntimeError("password=secret transcript=private")

    errors = io.StringIO()
    assert (
        importer.import_archive(archive(record()), BrokenStore(), errors)["failed"] == 1
    )
    assert "secret" not in errors.getvalue() and "private" not in errors.getvalue()


def test_sqlite_restart_idempotency_and_changed_content(tmp_path):
    url = f"sqlite:///{tmp_path / 'archive.db'}"
    store = TigerStore(url)
    store.ensure_schema()
    first = importer.import_archive(archive(record()), store, io.StringIO())
    assert first["inserted"] == 1
    store.close()
    store = TigerStore(url)
    store.ensure_schema()
    try:
        retry = importer.import_archive(archive(record()), store, io.StringIO())
        assert retry["unchanged"] == 1
        changed = importer.import_archive(
            archive(record(text="A changed transcript version with new content.")),
            store,
            io.StringIO(),
        )
        assert changed["inserted"] == 1
        # No ingest jobs/media assets are fabricated for transcript imports.
        with store._engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(store.jobs)) == 0
            assert connection.scalar(select(func.count()).select_from(store.posts)) == 0
    finally:
        store.close()


def test_cli_summary_and_failure_status(tmp_path, monkeypatch, capsys):
    path = tmp_path / "input.jsonl"
    path.write_bytes(archive(record(), record(version=9)).getvalue())
    monkeypatch.setattr(importer, "load_environment", lambda: None)
    monkeypatch.setenv("TIGER_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("APP_ENV", "test")
    assert importer.main([str(path)]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["inserted"] == 1
    assert json.loads(output.err)["line"] == 2


def test_sqlite_chunk_failure_rolls_back_then_retry_succeeds(tmp_path):
    store = TigerStore(f"sqlite:///{tmp_path / 'atomic.db'}")
    store.ensure_schema()
    calls = 0

    def fail_second_insert(
        connection, cursor, statement, parameters, context, executemany
    ):
        nonlocal calls
        if statement.startswith("INSERT INTO"):
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated driver failure")

    event.listen(store._engine, "before_cursor_execute", fail_second_insert)
    item = record(text="Transcript content. " * 6000)
    try:
        assert (
            importer.import_archive(archive(item), store, io.StringIO())["failed"] == 1
        )
        assert calls >= 2
        event.remove(store._engine, "before_cursor_execute", fail_second_insert)
        with store._engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count()).select_from(store.source_passages)
                )
                == 0
            )
        assert (
            importer.import_archive(archive(item), store, io.StringIO())["inserted"]
            == 1
        )
        with store._engine.connect() as connection:
            rows = connection.execute(select(store.source_passages)).mappings().all()
        assert len(rows) > 50
        assert all(
            row["start_seconds"] is None and row["end_seconds"] is None for row in rows
        )
        assert all(row["timing_basis"] == "unavailable" for row in rows)
    finally:
        store.close()


def test_100000_records_use_only_bounded_reads():
    raw = archive(record()).getvalue()

    class GeneratedStream:
        remaining = 100_000

        def readline(self, size):
            assert size == importer.MAX_LINE_BYTES + 1
            if not self.remaining:
                return b""
            self.remaining -= 1
            return raw

    class CountingStore:
        def upsert_transcript_document(self, **kwargs):
            return True

    counts = importer.import_archive(GeneratedStream(), CountingStore(), io.StringIO())
    assert counts == {
        "records": 100_000,
        "inserted": 100_000,
        "unchanged": 0,
        "failed": 0,
    }


def test_cli_cleanup_failure_is_sanitized(tmp_path, monkeypatch, capsys):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")

    class CloseFailure:
        def ensure_schema(self):
            pass

        def close(self):
            raise RuntimeError("password=secret")

    monkeypatch.setattr(importer, "load_environment", lambda: None)
    monkeypatch.setattr(importer.TigerStore, "from_env", lambda: CloseFailure())
    assert importer.main([str(path)]) == 1
    output = capsys.readouterr()
    assert "secret" not in output.err
    assert "cleanup failed" in output.err
