"""Tiger Data stores actual media and unchanged metadata in one bound transaction."""

import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest

from backend.services.shared.types import Post
from backend.services.external_apis.tigerdata_storage import (
    StorageError,
    TigerDataPostStore,
)

JPEG = b"\xff\xd8\xff\xe0real-image-bytes\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42video-bytes"
DSN = "postgresql://example_user:private_password@db.example.com:5432/quantro"
MODULE = "backend.services.external_apis.tigerdata_storage"


def test_read_media_uses_bound_id_and_ends_transaction() -> None:
    connection = FakeConnection()
    digest = hashlib.sha256(JPEG).hexdigest()
    connection.returned = ("image/jpeg", memoryview(JPEG), digest)
    result = TigerDataPostStore(connection).read_media("media_123")
    assert result == ("image/jpeg", JPEG, digest)
    assert connection.calls[0][1] == ("media_123",)
    assert connection.rollbacks == 1 and connection.commits == 0
    assert connection.cursors[0].closed


def test_read_media_missing_and_failure_are_safe() -> None:
    connection = FakeConnection()
    connection.returned = None
    store = TigerDataPostStore(connection)
    assert store.read_media("missing") is None
    connection.failure.add("execute")
    with pytest.raises(StorageError, match="could not read") as error:
        store.read_media("media_123")
    assert "private_password" not in str(error.value)
    assert connection.commits == 0


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.closed = False

    def execute(self, command: str, parameters: tuple[object, ...]) -> object:
        self.connection.calls.append((command, parameters))
        self.connection.fail("execute")
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        self.connection.fail("fetch")
        return self.connection.returned

    def close(self) -> None:
        self.closed = True
        self.connection.fail("cursor_close")


class FakeConnection:
    def __init__(self) -> None:
        self.autocommit = False
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.returned: tuple[object, ...] | None = ("post_example",)
        self.failure: set[str] = set()
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.cursors: list[FakeCursor] = []

    def fail(self, operation: str) -> None:
        if operation in self.failure:
            raise RuntimeError("private_password private SQL private response")

    def cursor(self) -> FakeCursor:
        self.fail("cursor")
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.fail("commit")
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        self.fail("rollback")

    def close(self) -> None:
        self.closes += 1
        self.fail("close")


def post_file(
    directory: Path, content: bytes = JPEG, mime_type: str = "image/jpeg"
) -> Post:
    path = directory / "downloaded-media"
    path.write_bytes(content)
    return {
        "post_id": "post_example",
        "candidate_id": None,
        "post_url": "https://example.com/post/1",
        "platform": "direct",
        "title": "Original title",
        "caption": "Original café caption '; DROP TABLE posts; --",
        "published_at": None,
        "author": {
            "id": None,
            "handle": "creator",
            "display_name": "Creator",
            "profile_url": None,
            "bio": None,
            "verified": False,
            "followers_count": 0,
            "following_count": None,
            "posts_count": None,
        },
        "engagement": {
            "views_count": 0,
            "likes_count": 9,
            "comments_count": None,
            "shares_count": None,
        },
        "media": {
            "media_id": "media_example",
            "local_path": str(path.resolve()),
            "mime_type": mime_type,
            "playback_url": None,
            "duration_seconds": None,
            "width": 640,
            "height": 480,
            "fps": None,
            "has_audio": False,
        },
        "retrieval": {
            "retrieved_at": "2026-10-10T17:00:00Z",
            "provider": "test",
            "metadata_source_urls": ["https://example.com/post/1"],
            "unavailable_fields": ["published_at"],
            "warnings": [],
        },
    }


@pytest.mark.parametrize(
    "content,mime_type", [(JPEG, "image/jpeg"), (MP4, "video/mp4")]
)
def test_atomic_bound_insert_contains_real_bytes_and_unchanged_post(
    tmp_path: Path, content: bytes, mime_type: str
) -> None:
    post = post_file(tmp_path, content, mime_type)
    original = json.dumps(post)
    connection = FakeConnection()
    store = TigerDataPostStore(connection)

    store.save_post(post)

    assert len(connection.calls) == 1
    command, parameters = connection.calls[0]
    assert command.startswith("INSERT INTO quantro_retrieval.posts ")
    assert "%s::jsonb" in command and command.endswith("RETURNING post_id")
    assert "CREATE" not in command and "DROP TABLE" not in command
    assert parameters[:6] == (
        "post_example",
        "media_example",
        mime_type,
        hashlib.sha256(content).hexdigest(),
        len(content),
        content,
    )
    assert isinstance(parameters[6], str) and json.loads(parameters[6]) == post
    assert json.dumps(post) == original
    assert connection.commits == 1 and connection.rollbacks == 0
    assert all(cursor.closed for cursor in connection.cursors)
    store.close()
    assert connection.closes == 0


@pytest.mark.parametrize(
    "returned", [None, (), ("other_post",), ("post_example", "extra")]
)
def test_unconfirmed_returning_never_commits(
    tmp_path: Path, returned: tuple[object, ...] | None
) -> None:
    connection = FakeConnection()
    connection.returned = returned
    with pytest.raises(StorageError, match="confirm"):
        TigerDataPostStore(connection).save_post(post_file(tmp_path))
    assert connection.commits == 0 and connection.rollbacks == 1


@pytest.mark.parametrize("operation", ["cursor", "execute", "fetch", "commit"])
def test_database_failure_rolls_back_and_redacts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, operation: str
) -> None:
    connection = FakeConnection()
    connection.failure.add(operation)
    with pytest.raises(StorageError) as caught:
        TigerDataPostStore(connection).save_post(post_file(tmp_path))
    assert "private_password" not in str(caught.value) + caplog.text
    assert "private SQL" not in str(caught.value) + caplog.text
    assert caught.value.__suppress_context__
    assert connection.commits == 0 and connection.rollbacks == 1
    assert all(cursor.closed for cursor in connection.cursors)


def test_failed_rollback_preserves_safe_original_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    connection = FakeConnection()
    connection.failure.update({"execute", "rollback"})
    with pytest.raises(StorageError) as caught:
        TigerDataPostStore(connection).save_post(post_file(tmp_path))
    assert "private_password" not in str(caught.value) + caplog.text


def test_cursor_close_failure_preserves_committed_result(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    connection = FakeConnection()
    connection.failure.add("cursor_close")
    TigerDataPostStore(connection).save_post(post_file(tmp_path))
    assert connection.commits == 1 and connection.rollbacks == 0
    assert "private_password" not in caplog.text


@pytest.mark.parametrize(
    "content,mime_type",
    [
        (b"", "image/jpeg"),
        (b"HTML", "image/jpeg"),
        (JPEG, "video/mp4"),
        (JPEG, "text/html"),
    ],
)
def test_invalid_media_never_inserts(
    tmp_path: Path, content: bytes, mime_type: str
) -> None:
    connection = FakeConnection()
    with pytest.raises(StorageError):
        TigerDataPostStore(connection).save_post(
            post_file(tmp_path, content, mime_type)
        )
    assert connection.calls == [] and connection.commits == 0


def test_missing_and_relative_paths_never_insert(tmp_path: Path) -> None:
    connection = FakeConnection()
    post = post_file(tmp_path)
    Path(post["media"]["local_path"]).unlink()
    with pytest.raises(StorageError):
        TigerDataPostStore(connection).save_post(post)
    post["media"]["local_path"] = "relative.jpg"
    with pytest.raises(StorageError):
        TigerDataPostStore(connection).save_post(post)
    assert connection.calls == []


def test_symlink_media_is_rejected(tmp_path: Path) -> None:
    post = post_file(tmp_path)
    link = tmp_path / "symlink.jpg"
    try:
        link.symlink_to(post["media"]["local_path"])
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    post["media"]["local_path"] = str(link)
    connection = FakeConnection()
    with pytest.raises(StorageError):
        TigerDataPostStore(connection).save_post(post)
    assert connection.calls == []


def test_media_size_limit_is_checked_before_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(MODULE + ".MAX_MEDIA_BYTES", 10)
    connection = FakeConnection()
    with pytest.raises(StorageError, match="100 MB"):
        TigerDataPostStore(connection).save_post(post_file(tmp_path))
    assert connection.calls == []


def test_nonfinite_or_non_json_metadata_never_inserts(tmp_path: Path) -> None:
    connection = FakeConnection()
    post = post_file(tmp_path)
    for value in (float("nan"), object(), ("tuple",)):
        cast(Any, post)["caption"] = value
        with pytest.raises(StorageError):
            TigerDataPostStore(connection).save_post(post)
    assert connection.calls == []


def test_autocommit_connections_are_rejected() -> None:
    connection = FakeConnection()
    connection.autocommit = True
    with pytest.raises(StorageError, match="autocommit"):
        TigerDataPostStore(connection)


def install_connector(
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
    *,
    fail: bool = False,
) -> tuple[FakeConnection, list[tuple[str, dict[str, object]]]]:
    connection = FakeConnection()
    calls: list[tuple[str, dict[str, object]]] = []
    connector = importlib.import_module("psycopg")

    def connect(dsn: str, **options: object) -> FakeConnection:
        calls.append((dsn, options))
        if fail:
            raise RuntimeError("private_password full DSN private response")
        return connection

    monkeypatch.setattr(connector, "connect", connect)
    monkeypatch.setattr(MODULE + "._environment", lambda: dict(values))
    return connection, calls


def test_from_env_overrides_dsn_tls_and_timeout_weakening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = (
        DSN
        + "?sslmode=disable&sslrootcert=unsafe.pem&connect_timeout=0&options=-c%20statement_timeout%3D0"
    )
    connection, calls = install_connector(monkeypatch, {"TIGERDATA_DSN": dsn})
    store = TigerDataPostStore.from_env()
    assert calls == [
        (
            dsn,
            {
                "sslmode": "verify-full",
                "sslrootcert": "system",
                "gssencmode": "disable",
                "connect_timeout": 10,
                "options": "-c statement_timeout=120000",
                "autocommit": False,
            },
        )
    ]
    store.close()
    store.close()
    assert connection.closes == 1


def test_custom_root_certificate_and_database_url_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, calls = install_connector(
        monkeypatch,
        {"DATABASE_URL": DSN, "TIGERDATA_SSL_ROOT_CERT": " C:/certs/root.pem "},
    )
    TigerDataPostStore.from_env().close()
    assert calls[0][0] == DSN
    assert calls[0][1]["sslrootcert"] == "C:/certs/root.pem"


def test_primary_dsn_never_silently_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _, calls = install_connector(
        monkeypatch, {"TIGERDATA_DSN": "  ", "DATABASE_URL": DSN}
    )
    with pytest.raises(StorageError, match="TIGERDATA_DSN"):
        TigerDataPostStore.from_env()
    assert calls == []


@pytest.mark.parametrize(
    "dsn",
    [
        "dbname=quantro",
        "service=quantro host=db.example.com",
        "host=db.example.com hostaddr=1.2.3.4",
        "host=db1.example.com,db2.example.com",
        "host=/var/run/postgresql",
        "host=db.example.com port=5432,5433",
        "host=db.example.com port=0",
        "postgresql://db.example.com,other.example.com/quantro",
        "not a DSN private_password",
    ],
)
def test_unsafe_or_ambiguous_dsn_never_connects(
    monkeypatch: pytest.MonkeyPatch, dsn: str
) -> None:
    _, calls = install_connector(monkeypatch, {"TIGERDATA_DSN": dsn})
    with pytest.raises(StorageError) as caught:
        TigerDataPostStore.from_env()
    assert calls == []
    assert "private_password" not in str(caught.value)


@pytest.mark.parametrize("setting", ["PGSERVICE", "PGHOSTADDR"])
def test_libpq_environment_cannot_override_validated_host(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    _, calls = install_connector(monkeypatch, {"TIGERDATA_DSN": DSN, setting: "unsafe"})
    with pytest.raises(StorageError):
        TigerDataPostStore.from_env()
    assert calls == []


def test_connection_failure_does_not_expose_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, calls = install_connector(monkeypatch, {"TIGERDATA_DSN": DSN}, fail=True)
    with pytest.raises(StorageError) as caught:
        TigerDataPostStore.from_env()
    assert len(calls) == 1
    assert "private_password" not in str(caught.value) + caplog.text
    assert caught.value.__suppress_context__


def test_owned_close_failure_is_safe_and_idempotent() -> None:
    connection = FakeConnection()
    connection.failure.add("close")
    store = TigerDataPostStore(connection, owned_connection=True)
    with pytest.raises(StorageError) as caught:
        store.close()
    assert "private_password" not in str(caught.value)
    store.close()
    assert connection.closes == 1


def test_closed_owned_store_cannot_save(tmp_path: Path) -> None:
    store = TigerDataPostStore(FakeConnection(), owned_connection=True)
    store.close()
    with pytest.raises(StorageError, match="closed"):
        store.save_post(post_file(tmp_path))


def test_environment_precedence_and_other_keys_are_preserved_without_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "TIGERDATA_DSN=file-base\nOTHER_KEY=base\nFILE_ONLY=kept\n", encoding="utf-8"
    )
    (tmp_path / ".env.local").write_text(
        "TIGERDATA_DSN=file-local\nOTHER_KEY=local\nLOCAL_ONLY=kept\n", encoding="utf-8"
    )
    monkeypatch.setattr(MODULE + ".ROOT", tmp_path)
    monkeypatch.setenv("TIGERDATA_DSN", "process-value")
    monkeypatch.delenv("OTHER_KEY", raising=False)
    monkeypatch.delenv("FILE_ONLY", raising=False)
    monkeypatch.delenv("LOCAL_ONLY", raising=False)
    before = dict(os.environ)
    module = importlib.import_module(MODULE)
    values = module._environment()
    assert values["TIGERDATA_DSN"] == "process-value"
    assert values["OTHER_KEY"] == "local"
    assert values["FILE_ONLY"] == values["LOCAL_ONLY"] == "kept"
    assert dict(os.environ) == before
