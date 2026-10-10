"""Media playback serves only stored bytes and honors browser range requests."""

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routes.media import router
from backend.services.external_apis.tigerdata_storage import (
    StorageError,
    TigerDataPostStore,
)

DATA = b"0123456789"


@pytest.fixture
def client(monkeypatch):
    class Store:
        closed = False

        def read_media(self, media_id):
            if media_id == "missing":
                return None
            if media_id == "failed":
                raise StorageError("private connection details")
            return "video/mp4", DATA, hashlib.sha256(DATA).hexdigest()

        def close(self):
            self.closed = True

    store = Store()
    monkeypatch.setattr(TigerDataPostStore, "from_env", lambda: store)
    app = FastAPI()
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
    "value", ["bytes=50-", "bytes=5-2", "bytes=-0", "bytes=0-1,3-4", "invalid"]
)
def test_invalid_range(client, value):
    response = client[0].get("/media/media_1", headers={"Range": value})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"


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
