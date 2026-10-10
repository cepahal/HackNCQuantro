"""Namespaced Tiger claims prevent HTTP inbound/send replay across process restarts.

Retain pending claims after crashes or unknown provider acceptance. They require
operator review, never automatic replay. The in-memory alias remains for tests.
Spectrum retains its independent persistent delivery ledger.
"""

import hashlib
import json
import re
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement

from ..reporting.send_summary import InMemoryDeliveryTracker as DeliveryTracker
from ..shared.errors import ErrorCode, QuantroError, Stage
from ..storage.tiger import TigerStore


class TigerDeliveryTracker:
    def __init__(self, store: TigerStore, namespace: str) -> None:
        if re.fullmatch(r"[a-z_]{1,64}", namespace) is None:
            raise ValueError("Invalid delivery tracking namespace.")
        self._store = store
        self._namespace = namespace

    def _key(self, conversation_id: str, job_id: str) -> str:
        return hashlib.sha256(
            json.dumps(
                [self._namespace, conversation_id, job_id], separators=(",", ":")
            ).encode()
        ).hexdigest()

    def _where(self, conversation_id: str, job_id: str) -> ColumnElement[bool]:
        table = self._store.deliveries
        return (table.c.id == self._key(conversation_id, job_id)) & (
            table.c.namespace == self._namespace
        )

    def _failure(self) -> QuantroError:
        stage = (
            Stage.SEND_SUMMARY
            if self._namespace == "photon_summary"
            else Stage.RUN_ANALYSIS
        )
        return QuantroError(
            ErrorCode.STORAGE_FAILED,
            stage,
            "Message delivery tracking is unavailable.",
            retryable=True,
        )

    def check_and_record(self, conversation_id: str, job_id: str) -> bool:
        table = self._store.deliveries
        statement = (
            self._store._insert(table)
            .values(
                id=self._key(conversation_id, job_id),
                namespace=self._namespace,
                message_id=None,
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[table.c.id])
        )
        try:
            with self._store._engine.begin() as connection:
                return connection.execute(statement).rowcount == 1
        except SQLAlchemyError:
            raise self._failure() from None

    def record_message_id(
        self, conversation_id: str, job_id: str, message_id: str
    ) -> None:
        table = self._store.deliveries
        statement = (
            update(table)
            .where(self._where(conversation_id, job_id), table.c.message_id.is_(None))
            .values(message_id=message_id)
        )
        try:
            with self._store._engine.begin() as connection:
                connection.execute(statement)
        except SQLAlchemyError:
            raise self._failure() from None

    def get_message_id(self, conversation_id: str, job_id: str) -> str | None:
        statement = select(self._store.deliveries.c.message_id).where(
            self._where(conversation_id, job_id)
        )
        try:
            with self._store._engine.connect() as connection:
                return connection.execute(statement).scalar_one_or_none()
        except SQLAlchemyError:
            raise self._failure() from None

    def has_record(self, conversation_id: str, job_id: str) -> bool:
        statement = select(self._store.deliveries.c.id).where(
            self._where(conversation_id, job_id)
        )
        try:
            with self._store._engine.connect() as connection:
                return connection.execute(statement).first() is not None
        except SQLAlchemyError:
            raise self._failure() from None

    def release(self, conversation_id: str, job_id: str) -> None:
        table = self._store.deliveries
        # A recorded provider acceptance must never become eligible for another send.
        statement = delete(table).where(
            self._where(conversation_id, job_id), table.c.message_id.is_(None)
        )
        try:
            with self._store._engine.begin() as connection:
                connection.execute(statement)
        except SQLAlchemyError:
            raise self._failure() from None


__all__ = ["DeliveryTracker", "TigerDeliveryTracker"]
