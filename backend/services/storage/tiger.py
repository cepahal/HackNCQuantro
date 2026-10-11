"""Tiger Data (Postgres) persistence for ingest jobs, posts, media features and reports.

Internal storage: post and feature JSON are stored exactly as validated, including
worker file paths. Never return these rows to a browser-facing report without the
reporting sanitizer.

Operator CLI (reads TIGER_DATABASE_URL from .secrets/.env, then backend/.env):
    python -m services.storage.tiger list [--limit N]   # from backend/
    python -m services.storage.tiger show <job_id>
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    Table,
    Text,
    create_engine,
    func,
    inspect,
    literal_column,
    select,
    text,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, OperationalError, ProgrammingError
from sqlalchemy.schema import CreateColumn
from sqlalchemy.sql.elements import TextClause

from ..shared.post_identity import post_identity
from ..shared.types import Author, MediaFeatures, Post

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
            Column("error_message", Text),
            Column("error_retryable", Boolean),
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
        self.deliveries = Table(
            f"{table_prefix}message_deliveries", metadata,
            Column("id", Text, primary_key=True),
            Column("namespace", Text, nullable=False),
            Column("message_id", Text),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        self.source_passages = Table(
            f"{table_prefix}source_passages", metadata,
            Column("id", Text, primary_key=True),
            Column("source_identity", Text, nullable=False, index=True),
            Column("source_url", Text, nullable=False),
            Column("content_sha256", Text, nullable=False),
            Column("passage_text", Text, nullable=False),
            Column("post_metadata", _JsonColumn, nullable=False),
            Column("provenance", _JsonColumn, nullable=False),
            Column("timing_basis", Text, nullable=False),
            Column("start_seconds", Float),
            Column("end_seconds", Float),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        Index(
            f"{table_prefix}passage_fts_idx",
            func.to_tsvector(literal_column("'simple'"), self.source_passages.c.passage_text),
            postgresql_using="gin",
            _table=self.source_passages,
        ).ddl_if(dialect="postgresql")
        self.source_features = Table(
            f"{table_prefix}source_features", metadata,
            Column("id", Text, primary_key=True),
            Column("source_identity", Text, nullable=False, index=True),
            Column("extraction_signature", Text, nullable=False),
            Column("media_sha256", Text, nullable=False),
            Column("post_json", _JsonColumn, nullable=False),
            Column("features_json", _JsonColumn, nullable=False),
            Column("retrieved_at", DateTime(timezone=True), nullable=False),
        )
        Index(f"{table_prefix}source_features_lookup_idx", self.source_features.c.source_identity,
              self.source_features.c.extraction_signature, self.source_features.c.retrieved_at)
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
                            "sslrootcert": os.getenv("TIGERDATA_SSL_ROOT_CERT") or str(url.query.get("sslrootcert") or "system"),
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
        # Additive migration preserves existing jobs and reports from earlier deployments.
        with self._engine.begin() as connection:
            columns = {item["name"] for item in inspect(connection).get_columns(self.jobs.name)}
            table = connection.dialect.identifier_preparer.quote(self.jobs.name)
            for name in ("error_message", "error_retryable"):
                if name not in columns:
                    clause = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
                    definition = str(CreateColumn(self.jobs.c[name]).compile(dialect=connection.dialect))
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {clause}{definition}"))

    def try_create_job(self, job_id: str, url: str) -> bool:
        """Atomically reserve a new public ingest ID without changing any existing row."""
        now = datetime.now(UTC)
        statement = self._insert(self.jobs).values(
            id=job_id, url=url, status="running", error_code=None, error_stage=None,
            created_at=now, updated_at=now,
        ).on_conflict_do_nothing(index_elements=[self.jobs.c.id]).returning(self.jobs.c.id)
        with self._engine.begin() as connection:
            # psycopg3 can report rowcount=-1 for INSERT. RETURNING identifies
            # the reservation owner consistently on Postgres and SQLite.
            return connection.execute(statement).scalar_one_or_none() == job_id

    def upsert_job(
        self,
        job_id: str,
        url: str,
        status: str,
        error_code: str | None = None,
        error_stage: str | None = None,
        error_message: str | None = None,
        error_retryable: bool = False,
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
            error_message=error_message,
            error_retryable=error_retryable,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[self.jobs.c.id],
            set_={
                "url": statement.excluded.url,
                "status": statement.excluded.status,
                "updated_at": statement.excluded.updated_at,
                "error_code": statement.excluded.error_code,
                "error_stage": statement.excluded.error_stage,
                "error_message": statement.excluded.error_message,
                "error_retryable": statement.excluded.error_retryable,
            },
        )
        with self._engine.begin() as connection:
            connection.execute(statement)

    @staticmethod
    def _document_values(source_url: str, metadata: dict[str, Any], provenance: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
        if not isinstance(source_url, str) or not source_url.startswith(("https://", "http://")):
            raise ValueError("Transcript source URL must be HTTP(S).")
        if not isinstance(metadata, dict) or not isinstance(provenance, dict):
            raise TypeError("Transcript metadata/provenance must be objects.")
        recording = metadata.get("recording_url")
        url = recording if (isinstance(recording, str) and recording.startswith(("https://", "http://"))
                            and provenance.get("recording_link_basis") == "publisher_link") else source_url
        safe = {key: metadata.get(key) for key in ("title", "caption", "published_at")}
        safe.update(post_url=url, author=metadata.get("author") or {})
        if not isinstance(safe["author"], dict):
            raise TypeError("Transcript author must be an object.")
        safe["author"] = {key: value for key, value in safe["author"].items() if key in Author.model_fields}
        publisher = metadata.get("publisher")
        if publisher is not None and not isinstance(publisher, str):
            raise TypeError("Transcript publisher must be a string or null.")
        if not safe["author"] and publisher:
            safe["author"] = {"display_name": publisher}
        # Internal provenance is allowlisted; media local paths never enter this table.
        evidence = {key: provenance.get(key) for key in (
            "transcript_source_url", "acquired_at", "rights_status", "recording_link_basis",
            "sha256", "feature_sha256", "provider", "model_revision", "index_revision",
            "event_date", "publication_date",
        )}
        return url, safe, evidence

    def _write_passages(self, connection: Connection, *, source_url: str, metadata: dict[str, Any], provenance: dict[str, Any], passages: Sequence[tuple[str, float | None, float | None]], timing_basis: str) -> bool:
        url, safe, evidence = self._document_values(source_url, metadata, provenance)
        identity = post_identity(url)
        inserted = False
        batch = []
        for ordinal, (value, begin, end) in enumerate(passages):
            if not value.strip():
                continue
            key = hashlib.sha256(f"{identity}\0{provenance['sha256']}\0{provenance.get('feature_sha256', '')}\0{provenance.get('index_revision', 'timed-v1')}\0{ordinal}".encode()).hexdigest()
            batch.append({"id": key, "source_identity": identity, "source_url": url,
                              "content_sha256": provenance["sha256"], "passage_text": value,
                              "post_metadata": safe, "provenance": evidence, "timing_basis": timing_basis,
                              "start_seconds": begin, "end_seconds": end, "created_at": datetime.now(UTC)})
            if len(batch) == 50:
                statement = self._insert(self.source_passages).values(batch).on_conflict_do_nothing(index_elements=[self.source_passages.c.id])
                inserted = connection.execute(statement).rowcount > 0 or inserted
                batch = []
        if batch:
            statement = self._insert(self.source_passages).values(batch).on_conflict_do_nothing(index_elements=[self.source_passages.c.id])
            inserted = connection.execute(statement).rowcount > 0 or inserted
        return inserted

    def upsert_transcript_document(self, *, source_url: str, text: str, metadata: dict[str, Any],
                                   provenance: dict[str, Any], timing_basis: str = "unavailable") -> bool:
        """Index immutable untimed text; URL safety/rights are checked by the importer.

        This is an internal discovery document, never a fabricated Post/MediaAsset.
        Every chunk has null seconds; word positions are not recording timestamps.
        """
        if timing_basis != "unavailable":
            raise ValueError("Untimed documents must use timing_basis=unavailable.")
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 2_000_000:
            raise ValueError("Transcript must contain at most 2 MB of text.")
        digest = hashlib.sha256(text.encode()).hexdigest()
        if not isinstance(provenance, dict) or provenance.get("sha256") != digest:
            raise ValueError("Transcript SHA256 provenance does not match text.")
        # Prefer paragraph boundaries, retaining overlap so boundary phrases remain searchable.
        passages = []
        start = 0
        while start < len(text):
            end = min(start + 2000, len(text))
            if end < len(text):
                boundary = text.rfind("\n\n", start + 1000, end)
                if boundary >= 0:
                    end = boundary + 2
            passages.append((text[start:end], None, None))
            if end == len(text):
                break
            start = end - 250
        provenance = {**provenance, "index_revision": "paragraph-overlap-v1"}
        with self._engine.begin() as connection:
            return self._write_passages(connection, source_url=source_url, metadata=metadata,
                                        provenance=provenance, passages=passages, timing_basis=timing_basis)

    def _index_feature_transcript(self, connection: Connection, post_json: dict[str, Any], features_json: dict[str, Any]) -> bool:
        transcript = features_json.get("transcript") if isinstance(features_json, dict) else None
        if not isinstance(transcript, dict) or transcript.get("status") not in {"complete", "partial"}:
            return False
        post = Post.model_validate(post_json)
        features = MediaFeatures.model_validate(features_json)
        sentences = features.transcript.sentences
        if not sentences:
            return False
        if len(sentences) > 10000 or sum(len(s.text.encode()) for s in sentences) > 2_000_000:
            raise ValueError("Transcript exceeds the bounded passage index capacity.")
        units = []
        for sentence in sentences:
            if (not all(math.isfinite(value) for value in (sentence.start_seconds, sentence.end_seconds, features.duration_seconds))
                    or not 0 <= sentence.start_seconds < sentence.end_seconds <= features.duration_seconds):
                raise ValueError("Transcript sentence times must be within observed media duration.")
            for i in range(0, len(sentence.text), 1000):
                units.append((sentence.text[i:i + 1000], sentence.start_seconds, sentence.end_seconds))
                if len(units) > 10000:
                    raise ValueError("Transcript exceeds the bounded passage index capacity.")
        passages = []
        start = 0
        while start < len(units):
            end = start + 1
            size = len(units[start][0])
            while end < len(units) and size + 1 + len(units[end][0]) <= 2000:
                size += 1 + len(units[end][0])
                end += 1
            window = units[start:end]
            passages.append((" ".join(unit[0] for unit in window),
                             min(unit[1] for unit in window), max(unit[2] for unit in window)))
            if end == len(units):
                break
            start = max(start + 1, end - 1)
        immutable = "\n".join(sentence.text for sentence in sentences)
        digest = hashlib.sha256(immutable.encode()).hexdigest()
        feature_hash = hashlib.sha256(json.dumps(features_json, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return self._write_passages(
            connection, source_url=post.post_url,
            metadata={"title": post.title, "caption": post.caption, "published_at": post.published_at,
                      "author": post.author.model_dump(mode="json")},
            provenance={"sha256": digest, "feature_sha256": feature_hash, "index_revision": "timed-v2",
                        "transcript_source_url": post.post_url, "provider": post.retrieval.provider,
                        "acquired_at": post.retrieval.retrieved_at},
            passages=passages, timing_basis="asr",
        )

    def upsert_transcript_source(self, post_json: dict[str, Any], features_json: dict[str, Any]) -> bool:
        """Index validated complete/partial timed feature sentences without changing JSON."""
        with self._engine.begin() as connection:
            return self._index_feature_transcript(connection, post_json, features_json)

    @staticmethod
    def _cache_signature(value: str) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[a-fA-F0-9]{64}", value) is None:
            raise ValueError("Cache hashes must be 64 hexadecimal characters.")
        return value.lower()

    def cache_source_features(self, post_json: dict[str, Any], features_json: dict[str, Any],
                              extraction_signature: str, media_sha256: str) -> None:
        """Save an immutable prior-recording snapshot, not proof of current URL bytes."""
        signature = self._cache_signature(extraction_signature)
        content_hash = self._cache_signature(media_sha256)
        post = Post.model_validate(post_json)
        features = MediaFeatures.model_validate(features_json)
        if post.media.media_id != features.media_id:
            raise ValueError("Cached Post and MediaFeatures media_id must match.")
        identity = post_identity(post.post_url)
        acquired = datetime.fromisoformat(post.retrieval.retrieved_at)
        if acquired > datetime.now(UTC):
            raise ValueError("Cached retrieval timestamp cannot be in the future.")
        key = hashlib.sha256(f"{identity}\0{content_hash}\0{signature}".encode()).hexdigest()
        statement = self._insert(self.source_features).values(
            id=key, source_identity=identity, extraction_signature=signature,
            media_sha256=content_hash, post_json=post_json, features_json=features_json,
            retrieved_at=acquired,
        ).on_conflict_do_nothing(index_elements=[self.source_features.c.id])
        with self._engine.begin() as connection:
            connection.execute(statement)
            self._index_feature_transcript(connection, post_json, features_json)

    def get_cached_source(self, source_url: str, extraction_signature: str,
                          max_age_seconds: int = 3600) -> dict[str, Any] | None:
        """Return the latest fresh snapshot; callers must disclose reuse and verify alignment."""
        signature = self._cache_signature(extraction_signature)
        if type(max_age_seconds) is not int or not 0 < max_age_seconds <= 86400:
            raise ValueError("Cache TTL must be between 1 and 86400 seconds.")
        if not isinstance(source_url, str) or not source_url.startswith(("https://", "http://")):
            raise ValueError("Cache source URL must be HTTP(S).")
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        query = select(self.source_features).where(
            self.source_features.c.source_identity == post_identity(source_url),
            self.source_features.c.extraction_signature == signature,
            self.source_features.c.retrieved_at >= cutoff,
        ).order_by(self.source_features.c.retrieved_at.desc(), self.source_features.c.id).limit(1)
        with self._engine.connect() as connection:
            row = connection.execute(query).mappings().first()
        if row is None:
            return None
        return {"post_json": row["post_json"], "features_json": row["features_json"],
                "media_sha256": row["media_sha256"], "retrieved_at": _iso(row["retrieved_at"])}

    def _transcript_search_statement(self, queries: list[str], limit: int, excluded_identities: Sequence[str]) -> tuple[TextClause, dict[str, Any]]:
        table = self._engine.dialect.identifier_preparer.quote(self.source_passages.name)
        if self._engine.dialect.name == "postgresql":
            statement = text(f"""
                WITH inputs AS (SELECT unnest(CAST(:queries AS text[])) AS query),
                matched AS MATERIALIZED (
                    SELECT p.id,p.source_identity,p.post_metadata,i.query,
                    ts_rank_cd(to_tsvector('simple',p.passage_text),websearch_to_tsquery('simple',i.query)) AS score
                    FROM {table} p CROSS JOIN inputs i
                    WHERE p.source_identity != ALL(CAST(:excluded AS text[]))
                    AND to_tsvector('simple',p.passage_text) @@ websearch_to_tsquery('simple',i.query)
                ), grouped AS (
                    SELECT source_identity,array_agg(DISTINCT query ORDER BY query) AS discovered,max(score) AS score
                    FROM matched GROUP BY source_identity
                ), representative AS (
                    SELECT DISTINCT ON(source_identity) source_identity,post_metadata
                    FROM matched ORDER BY source_identity,score DESC,id
                ) SELECT r.post_metadata,g.discovered FROM grouped g JOIN representative r USING(source_identity)
                ORDER BY g.score DESC,g.source_identity LIMIT :limit
            """)
            return statement, {"queries": queries, "excluded": list(excluded_identities), "limit": limit}
        # SQLite is a bounded test/reference implementation, not production FTS parity.
        params: dict[str, Any] = {"limit": limit};conditions=[];flags=[]
        for index, query in enumerate(queries):
            tokens = re.findall(r"\w+", query.lower())[:12]
            terms=[]
            for ordinal, token in enumerate(tokens):
                key=f"q{index}t{ordinal}";params[key]=f"%{token}%";terms.append(f"lower(passage_text) LIKE :{key}")
            condition="("+" AND ".join(terms)+")" if terms else "0=1"
            conditions.append(condition);flags.append(f"max(CASE WHEN {condition} THEN 1 ELSE 0 END) AS q{index}")
        excluded=[]
        for index, identity in enumerate(excluded_identities):
            key=f"excluded{index}";params[key]=identity;excluded.append(f":{key}")
        exclusion=" AND source_identity NOT IN ("+",".join(excluded)+")" if excluded else ""
        statement=text(f"SELECT p.post_metadata,{','.join('g.q'+str(i) for i in range(len(queries)))} FROM {table} p JOIN (SELECT source_identity,min(id) AS id,{','.join(flags)} FROM {table} WHERE ({' OR '.join(conditions)}){exclusion} GROUP BY source_identity ORDER BY source_identity LIMIT :limit) g ON p.id=g.id ORDER BY p.source_identity")
        return statement, params

    def find_transcript_posts(self, queries: list[str], limit: int, excluded_identities: Sequence[str] = ()) -> list[tuple[object, object]]:
        """Discover source metadata from bounded transcript passages, never media bytes."""
        if (type(limit) is not int or not 1 <= limit <= 50 or not isinstance(queries, list)
                or not 1 <= len(queries) <= 20 or any(not isinstance(q, str) or not q.strip() or len(q)>512 for q in queries)):
            raise ValueError("Invalid transcript search bounds.")
        if isinstance(excluded_identities, str) or len(excluded_identities)>100 or any(not isinstance(x, str) for x in excluded_identities):
            raise ValueError("Invalid excluded identities.")
        normalized=list(dict.fromkeys(q.strip() for q in queries))
        statement,params=self._transcript_search_statement(normalized,limit,excluded_identities)
        with self._engine.connect() as connection:
            rows=connection.execute(statement,params).fetchall()
        results: list[tuple[object, object]]=[]
        for row in rows:
            metadata=json.loads(row[0]) if isinstance(row[0], str) else row[0]
            matched=list(row[1]) if self._engine.dialect.name=="postgresql" else [q for index,q in enumerate(normalized) if row[index+1]]
            results.append((metadata,matched))
        return results

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
            # Legacy callers can persist minimal JSON; indexing requires the full shared Post.
            if isinstance(post_json, dict) and {"post_id", "post_url", "platform", "media", "retrieval"} <= post_json.keys():
                self._index_feature_transcript(connection, post_json, features_json)

    def set_job_status(
        self,
        job_id: str,
        status: str,
        error_code: str | None = None,
        error_stage: str | None = None,
        error_message: str | None = None,
        error_retryable: bool = False,
    ) -> None:
        statement = (
            self.jobs.update()
            .where(self.jobs.c.id == job_id)
            .values(
                status=status,
                updated_at=datetime.now(UTC),
                error_code=error_code,
                error_stage=error_stage,
                error_message=error_message,
                error_retryable=error_retryable,
            )
        )
        with self._engine.begin() as connection:
            if connection.execute(statement).rowcount != 1:
                raise LookupError(f"No job row for {job_id!r}")

    def expire_stale_job(self, job_id: str, cutoff: datetime) -> bool:
        """Atomically expire a stalled job without racing a worker's recent heartbeat."""
        statement = self.jobs.update().where(
            self.jobs.c.id == job_id,
            self.jobs.c.status.in_(("running", "ingested")),
            self.jobs.c.updated_at < cutoff,
        ).values(status="failed", updated_at=datetime.now(UTC),
                 error_code="PROCESSING_FAILED", error_stage="run_analysis",
                 error_message="Analysis stopped before completion. Please retry.", error_retryable=True)
        with self._engine.begin() as connection:
            return connection.execute(statement).rowcount == 1

    def touch_job(self, job_id: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(self.jobs.update().where(
                self.jobs.c.id == job_id, self.jobs.c.status.in_(("running", "ingested")),
            ).values(updated_at=datetime.now(UTC)))

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
                "error_message": row["error_message"],
                "error_retryable": bool(row["error_retryable"]),
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
            "error_message": row["error_message"],
            "error_retryable": bool(row["error_retryable"]),
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

    from ..shared.config import load_environment

    load_environment()
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
