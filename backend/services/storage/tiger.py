"""Tiger Data (Postgres) persistence for ingest jobs, posts, media features and reports.

Internal storage: post and feature JSON are stored exactly as validated, including
worker file paths. Never return these rows to a browser-facing report without the
reporting sanitizer.

Operator CLI (reads TIGER_DATABASE_URL from .secrets/.env, then backend/.env):
    python -m services.storage.tiger list [--limit N]   # from backend/
    python -m services.storage.tiger show <job_id>
"""

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    Table,
    Text,
    create_engine,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import ArgumentError, OperationalError, ProgrammingError

_SECURE_SSL_MODES = {"require", "verify-ca", "verify-full"}
_LOOPBACK_HOSTS = {None, "", "localhost", "127.0.0.1", "::1"}
_PREFIX_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
_JsonColumn = JSON().with_variant(JSONB(), "postgresql")


class TigerConfigError(RuntimeError):
    """Raised when the Tiger Data connection settings are missing or unsafe."""


def _engine_url(database_url: str) -> URL:
    try:
        url = make_url(database_url)
    except (ArgumentError, TypeError, ValueError):
        raise TigerConfigError("TIGER_DATABASE_URL is not a valid database URL.") from None

    production = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "")).lower() == "production"
    if production and url.get_backend_name() not in {"postgres", "postgresql"}:
        raise TigerConfigError("Production TIGER_DATABASE_URL must use PostgreSQL with TLS.")
    if url.get_backend_name() in {"postgres", "postgresql"}:
        # A loopback Postgres (local smoke tests) never crosses the network, so TLS is optional there.
        if (production or url.host not in _LOOPBACK_HOSTS) and url.query.get("sslmode") not in _SECURE_SSL_MODES:
            raise TigerConfigError(
                "TIGER_DATABASE_URL must include sslmode=require (or verify-ca/verify-full)."
            )
        return url.set(drivername="postgresql+psycopg")
    if url.get_backend_name() == "sqlite":
        return url
    raise TigerConfigError("TIGER_DATABASE_URL must be a postgres:// or postgresql:// URL.")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class TigerStore:
    def __init__(self, database_url: str, table_prefix: str = "quantro_") -> None:
        if not _PREFIX_PATTERN.match(table_prefix):
            raise TigerConfigError("TIGER_TABLE_PREFIX may only contain lowercase letters, digits and _.")
        self._engine: Engine = create_engine(_engine_url(database_url), pool_pre_ping=True)
        metadata = MetaData()
        self.jobs = Table(
            f"{table_prefix}jobs",
            metadata,
            Column("id", Text, primary_key=True),
            Column("url", Text, nullable=False),
            Column("status", Text, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
            Column("error_code", Text),
            Column("error_stage", Text),
        )
        self.posts = Table(
            f"{table_prefix}posts",
            metadata,
            Column("job_id", Text, ForeignKey(f"{table_prefix}jobs.id"), primary_key=True),
            Column("post_json", _JsonColumn, nullable=False),
            Column("features_json", _JsonColumn, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self.reports = Table(
            f"{table_prefix}reports",
            metadata,
            Column("report_id", Text, primary_key=True),
            Column("job_id", Text, ForeignKey(f"{table_prefix}jobs.id"), nullable=False),
            Column("report_json", _JsonColumn, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self._metadata = metadata

    @classmethod
    def from_env(cls) -> "TigerStore":
        database_url = os.getenv("TIGER_DATABASE_URL", "").strip()
        if not database_url:
            database_url = (os.getenv("TIGERDATA_DSN") or os.getenv("DATABASE_URL") or "").strip()
            if database_url:
                try:
                    url = make_url(database_url)
                    if url.get_backend_name() in {"postgres", "postgresql"}:
                        database_url = url.update_query_dict({
                            "sslmode": "verify-full",
                            "sslrootcert": os.getenv("TIGERDATA_SSL_ROOT_CERT") or url.query.get("sslrootcert") or "system",
                            "connect_timeout": "10",
                        }).render_as_string(hide_password=False)
                except (ArgumentError, TypeError, ValueError):
                    raise TigerConfigError("TIGERDATA_DSN is not a valid database URL.") from None
        if not database_url:
            raise TigerConfigError(
                "TIGER_DATABASE_URL is not set. Add it to backend/.env before starting the API."
            )
        return cls(database_url, os.getenv("TIGER_TABLE_PREFIX", "quantro_").strip() or "quantro_")

    def _insert(self, table: Table):
        dialect = postgresql if self._engine.dialect.name == "postgresql" else sqlite
        return dialect.insert(table)

    def ensure_schema(self) -> None:
        self._metadata.create_all(self._engine, checkfirst=True)

    def upsert_job(
        self,
        job_id: str,
        url: str,
        status: str,
        error_code: str | None = None,
        error_stage: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        statement = self._insert(self.jobs).values(
            id=job_id,
            url=url,
            status=status,
            created_at=now,
            updated_at=now,
            error_code=error_code,
            error_stage=error_stage,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[self.jobs.c.id],
            set_={
                "url": statement.excluded.url,
                "status": statement.excluded.status,
                "updated_at": statement.excluded.updated_at,
                "error_code": statement.excluded.error_code,
                "error_stage": statement.excluded.error_stage,
            },
        )
        with self._engine.begin() as connection:
            connection.execute(statement)

    def save_post_and_features(
        self, job_id: str, post_json: dict[str, Any], features_json: dict[str, Any]
    ) -> None:
        statement = self._insert(self.posts).values(
            job_id=job_id,
            post_json=post_json,
            features_json=features_json,
            created_at=datetime.now(UTC),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[self.posts.c.job_id],
            set_={
                "post_json": statement.excluded.post_json,
                "features_json": statement.excluded.features_json,
                "created_at": statement.excluded.created_at,
            },
        )
        with self._engine.begin() as connection:
            connection.execute(statement)

    def set_job_status(
        self,
        job_id: str,
        status: str,
        error_code: str | None = None,
        error_stage: str | None = None,
    ) -> None:
        statement = (
            self.jobs.update()
            .where(self.jobs.c.id == job_id)
            .values(
                status=status,
                updated_at=datetime.now(UTC),
                error_code=error_code,
                error_stage=error_stage,
            )
        )
        with self._engine.begin() as connection:
            if connection.execute(statement).rowcount != 1:
                raise LookupError(f"No job row for {job_id!r}")

    def save_report(self, report_id: str, job_id: str | dict[str, Any], report_json: dict[str, Any] | str) -> bool:
        """Insert a report once; returns False when report_id already exists (the first save wins)."""
        # Preserve teammates using the earlier (report_id, report_json, job_id) order.
        if isinstance(job_id, dict) and isinstance(report_json, str):
            job_id, report_json = report_json, job_id
        if not isinstance(job_id, str) or not isinstance(report_json, dict):
            raise TypeError("save_report expects report_id, job_id, report_json.")
        statement = (
            self._insert(self.reports)
            .values(
                report_id=report_id,
                job_id=job_id,
                report_json=report_json,
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[self.reports.c.report_id])
        )
        with self._engine.begin() as connection:
            return connection.execute(statement).rowcount == 1

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        query = select(self.reports.c.report_json).where(self.reports.c.report_id == report_id)
        with self._engine.connect() as connection:
            return connection.execute(query).scalar_one_or_none()

    def get_job_reports(self, job_id: str) -> list[dict[str, Any]]:
        query = (
            select(self.reports)
            .where(self.reports.c.job_id == job_id)
            .order_by(self.reports.c.created_at, self.reports.c.report_id)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        return [
            {
                "report_id": row["report_id"],
                "job_id": row["job_id"],
                "created_at": _iso(row["created_at"]),
                "report_json": row["report_json"],
            }
            for row in rows
        ]

    def list_recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer between 1 and 500.")
        query = (
            select(self.jobs, self.reports.c.report_id)
            .select_from(self.jobs.outerjoin(self.reports, self.reports.c.job_id == self.jobs.c.id))
            .order_by(self.jobs.c.updated_at.desc(), self.jobs.c.id)
            .limit(limit)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        return [
            {
                "id": row["id"],
                "status": row["status"],
                "url": row["url"],
                "report_id": row["report_id"],
                "error_code": row["error_code"],
                "error_stage": row["error_stage"],
                "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]),
            }
            for row in rows
        ]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        query = (
            select(self.jobs, self.posts.c.post_json, self.posts.c.features_json)
            .select_from(self.jobs.outerjoin(self.posts, self.posts.c.job_id == self.jobs.c.id))
            .where(self.jobs.c.id == job_id)
        )
        with self._engine.connect() as connection:
            row = connection.execute(query).mappings().first()
        if row is None:
            return None
        return {
            "id": row["id"],
            "url": row["url"],
            "status": row["status"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
            "error_code": row["error_code"],
            "error_stage": row["error_stage"],
            "post_json": row["post_json"],
            "features_json": row["features_json"],
        }

    def close(self) -> None:
        self._engine.dispose()


class TigerReportStore:
    """ReportStore (save_report.py) backed by the reports table."""

    def __init__(self, store: TigerStore) -> None:
        self._store = store

    def put(self, report_id: str, report_json: str) -> None:
        report = json.loads(report_json)
        self._store.save_report(report_id, report["job_id"], report)

    def get(self, report_id: str) -> str | None:
        report = self._store.get_report(report_id)
        return None if report is None else json.dumps(report)


class TigerJobStore:
    """JobStore (save_report.py) backed by the jobs table."""

    def __init__(self, store: TigerStore) -> None:
        self._store = store

    def mark_completed(self, job_id: str, report_id: str, completed_at: str) -> None:
        self._store.set_job_status(job_id, "completed")

    def get_status(self, job_id: str) -> str | None:
        job = self._store.get_job(job_id)
        return None if job is None else job["status"]


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=False))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m services.storage.tiger", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    list_command = commands.add_parser("list", help="print the most recently updated jobs")
    list_command.add_argument("--limit", type=int, default=20)
    show_command = commands.add_parser("show", help="print one job with its post, features and reports")
    show_command.add_argument("job_id")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[3] / ".secrets" / ".env")
    load_dotenv(Path(__file__).resolve().parents[3] / ".env.local")
    load_dotenv()
    try:
        store = TigerStore.from_env()
    except TigerConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    try:
        if args.command == "list":
            _print_json(store.list_recent_jobs(args.limit))
            return 0
        job = store.get_job(args.job_id)
        if job is None:
            print(f"error: no job {args.job_id!r}", file=sys.stderr)
            return 1
        _print_json({"job": job, "reports": store.get_job_reports(args.job_id)})
        return 0
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OperationalError:
        print("error: could not connect to Tiger Data; check the TIGER_DATABASE_URL host, "
              "password and sslmode", file=sys.stderr)
        return 1
    except ProgrammingError:
        print("error: Quantro tables not found; boot the API once (it creates them) "
              "or check TIGER_TABLE_PREFIX", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - CLI boundary never prints connection credentials.
        print(f"error: could not query Tiger Data ({type(error).__name__})", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
