import json

import pytest

from backend.services.storage import tiger
from backend.services.storage.tiger import TigerConfigError, TigerStore


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'tiger.db'}"


@pytest.fixture
def store(db_url):
    tiger_store = TigerStore(db_url)
    tiger_store.ensure_schema()
    yield tiger_store
    tiger_store.close()


def test_save_report_is_insert_once(store):
    store.upsert_job("job_1", "https://x.com/a", "running")

    assert store.save_report("report_job_1", {"job_id": "job_1", "v": 1}, "job_1") is True
    assert store.save_report("report_job_1", {"job_id": "job_1", "v": 2}, "job_1") is False

    assert store.get_report("report_job_1") == {"job_id": "job_1", "v": 1}
    reports = store.get_job_reports("job_1")
    assert [r["report_id"] for r in reports] == ["report_job_1"]
    assert reports[0]["created_at"].endswith("Z")


def test_get_job_reports_for_unknown_job_is_empty(store):
    assert store.get_job_reports("missing") == []


def test_list_recent_jobs_orders_by_update_and_limits(store):
    for job_id in ("job_a", "job_b", "job_c"):
        store.upsert_job(job_id, f"https://x.com/{job_id}", "ingested")
    store.set_job_status("job_a", "completed")
    store.save_report("report_job_a", {"job_id": "job_a"}, "job_a")

    jobs = store.list_recent_jobs(limit=2)

    assert [job["id"] for job in jobs] == ["job_a", "job_c"]
    assert jobs[0]["report_id"] == "report_job_a" and jobs[0]["status"] == "completed"
    assert "post_json" not in jobs[0]


@pytest.mark.parametrize("limit", [0, 501, True])
def test_list_recent_jobs_rejects_bad_limits(store, limit):
    with pytest.raises(ValueError):
        store.list_recent_jobs(limit)


@pytest.mark.parametrize(
    "url",
    ["postgresql://user@localhost:5432/quantro", "postgresql://user@127.0.0.1/quantro", "postgresql:///quantro"],
)
def test_loopback_postgres_does_not_require_tls(url):
    TigerStore(url).close()


def test_remote_postgres_still_requires_tls():
    with pytest.raises(TigerConfigError):
        TigerStore("postgresql://user:pw@db.example.com/quantro")


def test_cli_list_and_show(store, db_url, monkeypatch, capsys):
    monkeypatch.setenv("TIGER_DATABASE_URL", db_url)
    store.upsert_job("job_1", "https://x.com/a", "completed")
    store.save_post_and_features("job_1", {"post_id": "p"}, {"media_id": "m"})
    store.save_report("report_job_1", {"job_id": "job_1"}, "job_1")

    assert tiger.main(["list"]) == 0
    assert [job["id"] for job in json.loads(capsys.readouterr().out)] == ["job_1"]

    assert tiger.main(["show", "job_1"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["job"]["post_json"] == {"post_id": "p"}
    assert shown["reports"][0]["report_json"] == {"job_id": "job_1"}


def test_cli_show_unknown_job_and_bad_config(db_url, monkeypatch, capsys):
    monkeypatch.setenv("TIGER_DATABASE_URL", db_url)
    TigerStore(db_url).ensure_schema()
    assert tiger.main(["show", "nope"]) == 1
    assert "no job 'nope'" in capsys.readouterr().err

    monkeypatch.setenv("TIGER_DATABASE_URL", "postgresql://user:pw@db.example.com/quantro")
    assert tiger.main(["list"]) == 2
    err = capsys.readouterr().err
    assert "sslmode" in err and "pw" not in err
