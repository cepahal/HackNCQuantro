"""Media playback serves only stored bytes and honors browser range requests."""

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routes import media
from backend.routes.media import router
from backend.services.external_apis.tigerdata_storage import (
    StorageError,
    TigerDataPostStore,
)

DATA = b"0123456789"
DIGEST = hashlib.sha256(DATA).hexdigest()


class Store:
    def __init__(self):
        self.closed = False
        self.slices = []
        self.fail_next = False

    def read_media_info(self, media_id):
        if self.fail_next:
            self.fail_next = False
            raise StorageError("stale connection")
        if media_id == "missing":
            return None
        if media_id == "failed":
            raise StorageError("private connection details")
        return "video/mp4", len(DATA), DIGEST

    def read_media_slice(self, media_id, digest, start, length):
        assert digest == DIGEST
        self.slices.append((start, length))
        return DATA[start : start + length]

    def close(self):
        self.closed = True


@pytest.fixture
def client(monkeypatch):
    stores = []

    def open_store():
        stores.append(Store())
        return stores[-1]

    media.close_media_store()
    monkeypatch.setattr(TigerDataPostStore, "from_env", open_store)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as api:
        yield api, stores
    media.close_media_store()


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
    api, stores = client
    response = api.get("/media/media_1", headers={"Range": value})
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(len(expected))
    assert sum(length for _, length in stores[0].slices) == len(expected)


@pytest.mark.parametrize("value", ["bytes=50-", "bytes=5-2", "bytes=-0", "bytes=x-1"])
def test_unsatisfiable_range(client, value):
    response = client[0].get("/media/media_1", headers={"Range": value})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"
    assert set(response.json()["error"]) == {"code", "stage", "message", "retryable"}


@pytest.mark.parametrize("value", ["bytes=0-1,3-4", "items=0-1", "invalid"])
def test_multi_range_and_other_units_are_ignored(client, value):
    response = client[0].get("/media/media_1", headers={"Range": value})
    assert response.status_code == 200 and response.content == DATA
    assert "content-range" not in response.headers


def test_full_missing_failed_and_invalid_id(client):
    api, _ = client
    response = api.get("/media/media_1")
    assert response.status_code == 200 and response.content == DATA
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["etag"] == f'"{DIGEST}"'
    missing = api.get("/media/missing")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "MEDIA_UNAVAILABLE"
    failed = api.get("/media/failed")
    assert failed.status_code == 503 and "private" not in failed.text
    assert failed.json()["error"]["retryable"] is True
    assert api.get("/media/bad.id").status_code == 422


def test_large_bodies_stream_in_bounded_slices(client, monkeypatch):
    api, stores = client
    monkeypatch.setattr(media, "CHUNK_BYTES", 4)
    assert api.get("/media/media_1").content == DATA
    assert stores[0].slices == [(0, 4), (4, 4), (8, 2)]


def test_connection_is_reused_and_reopened_once_when_stale(client):
    api, stores = client
    for _ in range(3):
        assert api.get("/media/media_1", headers={"Range": "bytes=0-1"}).status_code == 206
    assert len(stores) == 1 and not stores[0].closed
    stores[0].fail_next = True
    assert api.get("/media/media_1").status_code == 200
    assert len(stores) == 2 and stores[0].closed


def test_head_and_conditional_requests(client):
    api, stores = client
    head = api.head("/media/media_1")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == "10" and head.headers["accept-ranges"] == "bytes"
    assert stores[0].slices == []
    ranged_head = api.head("/media/media_1", headers={"Range": "bytes=2-3"})
    assert ranged_head.status_code == 206 and ranged_head.headers["content-range"] == "bytes 2-3/10"
    for value in (f'"{DIGEST}"', f'W/"{DIGEST}"', f'"other", "{DIGEST}"', "*"):
        cached = api.get("/media/media_1", headers={"If-None-Match": value})
        assert cached.status_code == 304 and cached.content == b""
        assert cached.headers["etag"] == f'"{DIGEST}"'
    assert api.get("/media/media_1", headers={"If-None-Match": '"other"'}).status_code == 200
