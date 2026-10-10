"""Snowflake persistence boundary: actual streams, safe bindings, verified completion."""

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import BinaryIO

import pytest

from backend.services.external_apis.snowflake_storage import (
    SnowflakePostStore,
    StorageError,
)

JPEG = b"\xff\xd8\xff\xe0" + b"test-jpeg-data" + b"\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + b"test-video-data"


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.closed = False

    def execute(
        self,
        command: str,
        parameters: tuple[object, ...] | None = None,
        *,
        file_stream: BinaryIO | None = None,
        timeout: int = 120,
    ) -> object:
        self.connection.calls.append((command, parameters))
        if command.startswith("PUT"):
            assert file_stream is not None and not file_stream.closed
            self.connection.uploaded = file_stream.read()
            if self.connection.fail_put:
                raise RuntimeError("secret-token signed-storage-url")
        elif self.connection.fail_insert:
            raise RuntimeError("secret-token SQL with sensitive-caption")
        assert timeout == 120
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        size = len(self.connection.uploaded)
        target = (
            "media.jpg"
            if self.connection.uploaded.startswith(b"\xff\xd8\xff")
            else "media.mp4"
        )
        target_size = (
            size if self.connection.target_size is None else self.connection.target_size
        )
        return [
            (
                target,
                target,
                size,
                target_size,
                "NONE",
                "NONE",
                self.connection.status,
                "",
            )
        ]

    def close(self) -> None:
        self.closed = True
        if self.connection.fail_cursor_close:
            raise RuntimeError("secret-token signed-storage-url")


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []
        self.uploaded = b""
        self.status = "UPLOADED"
        self.target_size: int | None = None
        self.fail_put = False
        self.fail_insert = False
        self.fail_commit = False
        self.fail_close = False
        self.fail_cursor_close = False
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("secret-token")
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("secret-token signed-storage-url")


def post_file(
    directory: Path, content: bytes = JPEG, mime_type: str = "image/jpeg"
) -> dict[str, object]:
    path = directory / ("source.jpg" if mime_type == "image/jpeg" else "source.mp4")
    path.write_bytes(content)
    return {
        "post_id": "post_example",
        "candidate_id": None,
        "post_url": "https://example.com/post/1",
        "platform": "direct",
        "title": "Example media title",
        "caption": "A real caption: café. '; DROP TABLE posts; --",
        "published_at": None,
        "author": {
            "id": "account_example",
            "handle": "example_creator",
            "display_name": "Example Creator",
            "profile_url": "https://example.com/@example_creator",
            "bio": None,
            "verified": False,
            "followers_count": 0,
            "following_count": None,
            "posts_count": None,
        },
        "engagement": {
            "views_count": 0,
            "likes_count": 23,
            "comments_count": None,
            "shares_count": None,
        },
        "media": {
            "media_id": "media_example",
            "local_path": str(path.resolve()),
            "mime_type": mime_type,
            "playback_url": None,
            "duration_seconds": None if mime_type == "image/jpeg" else 14.0,
            "width": 640,
            "height": 480,
            "fps": None if mime_type == "image/jpeg" else 30.0,
            "has_audio": mime_type != "image/jpeg",
        },
        "retrieval": {
            "retrieved_at": "2026-10-10T17:00:00Z",
            "provider": "test_provider",
            "metadata_source_urls": [
                "https://example.com/post/1",
                "https://example.com/@example_creator",
            ],
            "unavailable_fields": [
                "published_at",
                "author.bio",
                "author.following_count",
                "author.posts_count",
                "engagement.comments_count",
                "engagement.shares_count",
            ],
            "warnings": [],
        },
    }


@pytest.mark.parametrize(
    "content,mime_type,filename",
    [(JPEG, "image/jpeg", "media.jpg"), (MP4, "video/mp4", "media.mp4")],
)
def test_actual_media_bytes_upload_before_unchanged_bound_post(
    tmp_path: Path, content: bytes, mime_type: str, filename: str
) -> None:
    connection = FakeConnection()
    post = post_file(tmp_path, content, mime_type)
    snapshot = json.dumps(post)
    store = SnowflakePostStore(connection)

    store.save_post(post)

    assert connection.uploaded == content
    assert len(connection.calls) == 2
    put, parameters = connection.calls[0]
    assert put.startswith(f"PUT file:///{filename} @QUANTRO_MEDIA/media_example ")
    assert "AUTO_COMPRESS=FALSE" in put and "OVERWRITE=FALSE" in put
    assert parameters is None
    statement, bindings = connection.calls[1]
    assert statement.startswith("INSERT INTO QUANTRO_POSTS")
    assert "PARSE_JSON(%s)" in statement and bindings is not None
    assert bindings[:6] == (
        "post_example",
        "media_example",
        f"@QUANTRO_MEDIA/media_example/{filename}",
        hashlib.sha256(content).hexdigest(),
        len(content),
        mime_type,
    )
    assert isinstance(bindings[6], str) and json.loads(bindings[6]) == post
    assert "DROP TABLE" not in statement
    assert json.dumps(post) == snapshot
    assert connection.commits == 1 and connection.rollbacks == 0
    assert all(cursor.closed for cursor in connection.cursors)
    store.close()
    assert connection.closes == 0


@pytest.mark.parametrize("status", ["ERROR", "SKIPPED", "UNKNOWN"])
def test_unconfirmed_put_never_inserts_or_commits(tmp_path: Path, status: str) -> None:
    connection = FakeConnection()
    connection.status = status

    with pytest.raises(StorageError):
        SnowflakePostStore(connection).save_post(post_file(tmp_path))

    assert len(connection.calls) == 1
    assert connection.commits == 0 and connection.rollbacks == 1


def test_put_size_mismatch_is_a_failure(tmp_path: Path) -> None:
    connection = FakeConnection()
    connection.target_size = 1
    with pytest.raises(StorageError):
        SnowflakePostStore(connection).save_post(post_file(tmp_path))
    assert len(connection.calls) == 1 and connection.commits == 0


@pytest.mark.parametrize("failure", ["put", "insert", "commit"])
def test_connector_failure_is_redacted_and_rolled_back(
    tmp_path: Path, failure: str
) -> None:
    connection = FakeConnection()
    setattr(connection, "fail_" + failure, True)

    with pytest.raises(StorageError) as caught:
        SnowflakePostStore(connection).save_post(post_file(tmp_path))

    assert "secret-token" not in str(caught.value)
    assert "sensitive-caption" not in str(caught.value)
    assert connection.commits == 0 and connection.rollbacks == 1
    assert not any(command.startswith("REMOVE") for command, _ in connection.calls)
    assert all(cursor.closed for cursor in connection.cursors)


@pytest.mark.parametrize(
    "content,mime_type",
    [
        (b"", "image/jpeg"),
        (b"html", "image/jpeg"),
        (JPEG, "video/mp4"),
        (JPEG, "text/html"),
    ],
)
def test_invalid_files_never_upload(
    tmp_path: Path, content: bytes, mime_type: str
) -> None:
    connection = FakeConnection()
    with pytest.raises(StorageError):
        SnowflakePostStore(connection).save_post(
            post_file(tmp_path, content, mime_type)
        )
    assert connection.calls == [] and connection.commits == 0


def test_missing_file_never_uploads(tmp_path: Path) -> None:
    connection = FakeConnection()
    post = post_file(tmp_path)
    (tmp_path / "source.jpg").unlink()
    with pytest.raises(StorageError):
        SnowflakePostStore(connection).save_post(post)
    assert connection.calls == []


@pytest.mark.parametrize(
    "identifier",
    ["BAD; DROP TABLE X", "@STAGE", "stage/path", "a..b", '"quoted"', "a.b.c.d", ""],
)
def test_invalid_object_identifiers_are_rejected(identifier: str) -> None:
    with pytest.raises(StorageError):
        SnowflakePostStore(FakeConnection(), stage=identifier)
    with pytest.raises(StorageError):
        SnowflakePostStore(FakeConnection(), table=identifier)


def test_media_identifier_cannot_inject_stage_path(tmp_path: Path) -> None:
    connection = FakeConnection()
    post = post_file(tmp_path)
    media = post["media"]
    assert isinstance(media, dict)
    media["media_id"] = "../escape;DROP"
    with pytest.raises(StorageError):
        SnowflakePostStore(connection).save_post(post)
    assert connection.calls == []


def test_from_env_uses_pat_and_environment_overrides_without_global_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = FakeConnection()
    captured: dict[str, object] = {}
    dotenv = ModuleType("dotenv")
    connector = ModuleType("snowflake.connector")

    def values(path: Path) -> dict[str, str]:
        assert path == tmp_path / ".env"
        return {
            "SNOWFLAKE_ACCOUNT": "file-account",
            "SNOWFLAKE_USER": "file-user",
            "SNOWFLAKE_TOKEN": "file-token",
        }

    def connect(**kwargs: object) -> FakeConnection:
        captured.update(kwargs)
        return connection

    monkeypatch.setattr(dotenv, "dotenv_values", values, raising=False)
    monkeypatch.setattr(connector, "connect", connect, raising=False)
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector)
    monkeypatch.setattr(
        "backend.services.external_apis.snowflake_storage.ROOT", tmp_path
    )
    for name in (
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_DATABASE",
        "SNOWFLAKE_SCHEMA",
        "SNOWFLAKE_ROLE",
        "SNOWFLAKE_MEDIA_STAGE",
        "SNOWFLAKE_POSTS_TABLE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "env-account")
    monkeypatch.setenv("SNOWFLAKE_USER", "env-user")
    monkeypatch.setenv("SNOWFLAKE_TOKEN", "env-token")
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "APP_DB")

    store = SnowflakePostStore.from_env()

    assert captured["account"] == "env-account"
    assert captured["user"] == "env-user"
    assert captured["token"] == "env-token"
    assert captured["authenticator"] == "PROGRAMMATIC_ACCESS_TOKEN"
    assert captured["autocommit"] is False
    assert captured["database"] == "APP_DB"
    store.close()
    store.close()
    assert connection.closes == 1


def test_missing_configuration_is_safe_and_does_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.services.external_apis.snowflake_storage._environment", dict
    )
    with pytest.raises(StorageError, match="SNOWFLAKE_ACCOUNT"):
        SnowflakePostStore.from_env()


def test_owned_connection_close_failure_is_redacted_and_release_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    connection = FakeConnection()
    connection.fail_close = True
    store = SnowflakePostStore(connection)
    monkeypatch.setattr(store, "_owns_connection", True)

    with pytest.raises(StorageError) as caught:
        store.close()

    assert "secret-token" not in str(caught.value) + caplog.text
    assert "signed-storage-url" not in str(caught.value) + caplog.text
    assert connection.closes == 1
    store.close()
    assert connection.closes == 1


def test_injected_connection_is_never_closed_even_when_close_would_fail() -> None:
    connection = FakeConnection()
    connection.fail_close = True

    SnowflakePostStore(connection).close()

    assert connection.closes == 0


def test_cursor_close_failure_preserves_committed_save_without_exposing_diagnostics(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    connection = FakeConnection()
    connection.fail_cursor_close = True

    SnowflakePostStore(connection).save_post(post_file(tmp_path))

    assert connection.commits == 1 and connection.rollbacks == 0
    assert "secret-token" not in caplog.text
    assert "signed-storage-url" not in caplog.text


def install_auth_connector(
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
    *,
    failure: Exception | None = None,
) -> tuple[FakeConnection, list[dict[str, object]]]:
    connection = FakeConnection()
    calls: list[dict[str, object]] = []
    connector = ModuleType("snowflake.connector")

    def connect(**kwargs: object) -> FakeConnection:
        calls.append(kwargs)
        if failure is not None:
            raise failure
        return connection

    monkeypatch.setattr(connector, "connect", connect, raising=False)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector)
    monkeypatch.setattr(
        "backend.services.external_apis.snowflake_storage._environment",
        lambda: dict(values),
    )
    return connection, calls


@pytest.mark.parametrize("token", [None, "", " \t "])
def test_password_auth_preserves_password_and_accepts_missing_or_blank_token(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    password = "  example password with spaces  "
    values = {
        "SNOWFLAKE_ACCOUNT": "  org-account  ",
        "SNOWFLAKE_USER": "  example-user  ",
        "SNOWFLAKE_PASSWORD": password,
    }
    if token is not None:
        values["SNOWFLAKE_TOKEN"] = token
    connection, calls = install_auth_connector(monkeypatch, values)

    store = SnowflakePostStore.from_env()

    assert len(calls) == 1
    assert calls[0]["account"] == "org-account"
    assert calls[0]["user"] == "example-user"
    assert calls[0]["authenticator"] == "snowflake"
    assert calls[0]["password"] == password
    assert "token" not in calls[0]
    assert calls[0]["autocommit"] is False
    assert calls[0]["login_timeout"] == 30
    assert calls[0]["network_timeout"] == 120
    store.close()
    assert connection.closes == 1


@pytest.mark.parametrize("password", [None, "", "   ", "example-password"])
def test_pat_auth_has_priority_and_trims_token_without_sending_password(
    monkeypatch: pytest.MonkeyPatch, password: str | None
) -> None:
    values = {
        "SNOWFLAKE_ACCOUNT": "org-account",
        "SNOWFLAKE_USER": "example-user",
        "SNOWFLAKE_TOKEN": "  example-token  ",
    }
    if password is not None:
        values["SNOWFLAKE_PASSWORD"] = password
    connection, calls = install_auth_connector(monkeypatch, values)

    store = SnowflakePostStore.from_env()

    assert len(calls) == 1
    assert calls[0]["authenticator"] == "PROGRAMMATIC_ACCESS_TOKEN"
    assert calls[0]["token"] == "example-token"
    assert "password" not in calls[0]
    store.close()
    assert connection.closes == 1


@pytest.mark.parametrize(
    ("token", "password"),
    [(None, None), ("", ""), (" \t ", "   "), (None, "   "), ("   ", None)],
)
def test_missing_or_blank_auth_credentials_reject_before_connection(
    monkeypatch: pytest.MonkeyPatch, token: str | None, password: str | None
) -> None:
    values = {"SNOWFLAKE_ACCOUNT": "org-account", "SNOWFLAKE_USER": "example-user"}
    if token is not None:
        values["SNOWFLAKE_TOKEN"] = token
    if password is not None:
        values["SNOWFLAKE_PASSWORD"] = password
    _, calls = install_auth_connector(monkeypatch, values)

    with pytest.raises(StorageError) as caught:
        SnowflakePostStore.from_env()

    assert "SNOWFLAKE_PASSWORD" in str(caught.value)
    assert "SNOWFLAKE_TOKEN" in str(caught.value)
    assert calls == []


@pytest.mark.parametrize("missing", ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER"])
def test_password_does_not_bypass_required_account_and_user(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    values = {
        "SNOWFLAKE_ACCOUNT": "org-account",
        "SNOWFLAKE_USER": "example-user",
        "SNOWFLAKE_PASSWORD": "example-password",
    }
    values[missing] = "   "
    _, calls = install_auth_connector(monkeypatch, values)

    with pytest.raises(StorageError):
        SnowflakePostStore.from_env()

    assert calls == []


@pytest.mark.parametrize("use_pat", [False, True])
def test_auth_connection_failure_is_redacted_and_never_retries_another_auth_method(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    use_pat: bool,
) -> None:
    password = "private-example-password"
    token = "private-example-token"
    diagnostic = "private connector diagnostic: " + password + " " + token
    values = {
        "SNOWFLAKE_ACCOUNT": "org-account",
        "SNOWFLAKE_USER": "example-user",
        "SNOWFLAKE_PASSWORD": password,
    }
    if use_pat:
        values["SNOWFLAKE_TOKEN"] = token
    _, calls = install_auth_connector(
        monkeypatch, values, failure=RuntimeError(diagnostic)
    )

    with pytest.raises(StorageError) as caught:
        SnowflakePostStore.from_env()

    assert len(calls) == 1
    assert calls[0]["authenticator"] == (
        "PROGRAMMATIC_ACCESS_TOKEN" if use_pat else "snowflake"
    )
    if use_pat:
        assert calls[0]["token"] == token
        assert "password" not in calls[0]
    else:
        assert calls[0]["password"] == password
        assert "token" not in calls[0]
    assert caught.value.code == "STORAGE_FAILED"
    assert caught.value.__suppress_context__
    visible = str(caught.value) + caplog.text
    assert password not in visible
    assert token not in visible
    assert diagnostic not in visible
