"""Media playback serves only stored bytes and honors browser range requests."""

import asyncio
import hashlib
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.routes.media import get_media, router
from backend.services.external_apis.tigerdata_storage import (
    MEDIA_CHUNK_BYTES,
    StorageError,
)

DATA = b"0123456789"


@pytest.fixture
def client(monkeypatch):
    class Store:
        closed = False
        data = DATA
        close_count = 0
        fail_after = None

        def __init__(self):
            self.calls = []

        def read_media_info(self, media_id):
            assert not self.closed
            if media_id == "missing":
                return None
            if media_id == "failed":
                raise StorageError("private connection details")
            return "video/mp4", len(self.data), hashlib.sha256(self.data).hexdigest()

        def read_media_chunk(self, media_id, start, length, *, size, digest):
            assert not self.closed
            assert (
                size == len(self.data)
                and digest == hashlib.sha256(self.data).hexdigest()
            )
            self.calls.append((media_id, start, length))
            if self.fail_after is not None and len(self.calls) > self.fail_after:
                raise StorageError("The stored media could not be read.")
            assert length <= MEDIA_CHUNK_BYTES
            return self.data[start : start + length]

        def close(self):
            self.closed = True
            self.close_count += 1

    store = Store()

    def connect():
        store.closed = False
        return store

    app = FastAPI()
    app.state.media_pool = SimpleNamespace(acquire=connect)
    app.include_router(router)
    with TestClient(app) as api:
        yield api, store


@pytest.mark.parametrize(
    "value,expected",
    [
        ("bytes=2-5", b"2345"),
        ("bytes=7-", b"789"),
        ("bytes=-3", b"789"),
        ("bytes=0-50", DATA),
    ],
)
def test_browser_range(client, value, expected):
    api, store = client
    response = api.get("/media/media_1", headers={"Range": value})
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["accept-ranges"] == "bytes"
    assert store.closed


@pytest.mark.parametrize(
    "value", ["bytes=50-", "bytes=5-2", "bytes=-0", "bytes=garbage", "bytes=-"]
)
def test_invalid_range(client, value):
    response = client[0].get("/media/media_1", headers={"Range": value})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"
    assert set(response.json()["error"]) == {"code", "stage", "message", "retryable"}
    assert client[1].calls == [] and client[1].closed


@pytest.mark.parametrize("value", ["items=0-1", "bytes=0-1,3-4", "invalid"])
def test_unsupported_range_is_ignored(client, value):
    api, store = client
    response = api.get("/media/media_1", headers={"Range": value})
    assert response.status_code == 200 and response.content == DATA
    assert "content-range" not in response.headers
    assert store.closed and store.close_count == 1


def test_head_reads_only_metadata_and_ignores_range(client):
    api, store = client
    response = api.head("/media/media_1", headers={"Range": "bytes=2-5"})
    assert response.status_code == 200 and response.content == b""
    assert response.headers["content-length"] == "10"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["etag"] == '"' + hashlib.sha256(DATA).hexdigest() + '"'
    assert "content-range" not in response.headers
    assert store.calls == [] and store.closed and store.close_count == 1


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("validator", ["exact", "weak", "list", "wildcard"])
def test_matching_etag_returns_304_without_loading_media(client, method, validator):
    api, store = client
    etag = '"' + hashlib.sha256(DATA).hexdigest() + '"'
    value = {
        "exact": etag,
        "weak": "W/" + etag,
        "list": '"other", W/' + etag,
        "wildcard": "*",
    }[validator]
    response = getattr(api, method)(
        "/media/media_1", headers={"If-None-Match": value, "Range": "bytes=50-"}
    )
    assert response.status_code == 304 and response.content == b""
    assert response.headers["etag"] == etag
    assert "content-range" not in response.headers
    assert store.calls == [] and store.closed and store.close_count == 1


def test_nonmatching_etag_still_returns_requested_range(client):
    api, store = client
    response = api.get(
        "/media/media_1", headers={"If-None-Match": '"other"', "Range": "bytes=2-5"}
    )
    assert response.status_code == 206 and response.content == b"2345"
    assert store.calls == [("media_1", 2, 4)]


def test_disabled_media_pool_returns_safe_storage_error(client):
    api, _ = client
    api.app.state.media_pool = None
    response = api.get("/media/media_1")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "STORAGE_FAILED",
        "stage": "display_report",
        "message": "The stored media could not be loaded.",
        "retryable": True,
    }


def test_full_missing_failed_and_invalid_id(client):
    api, store = client
    response = api.get("/media/media_1")
    assert response.status_code == 200 and response.content == DATA
    assert response.headers["content-type"] == "video/mp4"
    assert store.closed
    assert api.get("/media/missing").status_code == 404
    failed = api.get("/media/failed")
    assert failed.status_code == 503 and "private" not in failed.text
    assert api.get("/media/bad.id").status_code == 422


def test_small_range_never_loads_the_full_video(client):
    api, store = client
    store.data = b"v" * (4 * MEDIA_CHUNK_BYTES + 7)
    response = api.get("/media/media_1", headers={"Range": "bytes=1048576-1048577"})
    assert response.status_code == 206 and response.content == b"vv"
    assert response.headers["content-length"] == "2"
    assert (
        response.headers["content-range"] == f"bytes 1048576-1048577/{len(store.data)}"
    )
    assert store.calls == [("media_1", 1048576, 2)]
    assert store.closed and store.close_count == 1


def test_full_media_is_streamed_in_bounded_chunks_with_correct_headers(client):
    api, store = client
    store.data = b"v" * (2 * MEDIA_CHUNK_BYTES + 7)
    response = api.get("/media/media_1")
    assert response.status_code == 200 and response.content == store.data
    assert response.headers["content-length"] == str(len(store.data))
    assert (
        response.headers["etag"] == '"' + hashlib.sha256(store.data).hexdigest() + '"'
    )
    assert [length for _, _, length in store.calls] == [
        MEDIA_CHUNK_BYTES,
        MEDIA_CHUNK_BYTES,
        7,
    ]
    assert [offset for _, offset, _ in store.calls] == [
        0,
        MEDIA_CHUNK_BYTES,
        2 * MEDIA_CHUNK_BYTES,
    ]
    assert store.closed and store.close_count == 1


def test_connection_closes_after_client_disconnect(client):
    _, store = client
    response = get_media(
        "media_1", Request({"type": "http", "method": "GET", "app": client[0].app})
    )
    assert not store.closed

    async def disconnected():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            assert not store.closed
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        from starlette.requests import ClientDisconnect

        with pytest.raises(ClientDisconnect):
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
            )

    asyncio.run(disconnected())
    assert store.closed and store.close_count == 1


def test_connection_closes_when_a_later_chunk_fails(client):
    api, store = client
    store.data = b"v" * (MEDIA_CHUNK_BYTES + 7)
    store.fail_after = 1
    with pytest.raises(StorageError, match="could not be read"):
        api.get("/media/media_1")
    assert store.closed and store.close_count == 1


def test_initial_chunk_failure_is_structured_before_http_headers(client):
    api, store = client
    store.fail_after = 0
    response = api.get("/media/media_1")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORAGE_FAILED"
    assert store.closed and store.close_count == 1


def test_disconnect_during_database_read_waits_before_closing_connection(
    client, monkeypatch
):
    _, store = client
    store.data = b"v" * (MEDIA_CHUNK_BYTES + 7)
    response = get_media(
        "media_1", Request({"type": "http", "method": "GET", "app": client[0].app})
    )
    started, finish = Event(), Event()
    original = store.read_media_chunk

    def blocked_read(*args, **kwargs):
        started.set()
        assert finish.wait(5), "test must release the blocked database read"
        assert not store.closed
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "read_media_chunk", blocked_read)

    async def disconnected():
        async def receive():
            assert await asyncio.to_thread(started.wait, 3)
            return {"type": "http.disconnect"}

        async def send(_message):
            assert not store.closed

        task = asyncio.create_task(
            response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
        )
        try:
            assert await asyncio.to_thread(started.wait, 3)
            await asyncio.sleep(0.03)
            assert not store.closed and not task.done()
        finally:
            finish.set()
            await asyncio.wait_for(task, 3)

    asyncio.run(disconnected())
    assert store.closed and store.close_count == 1
