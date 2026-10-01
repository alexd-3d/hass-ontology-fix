"""Tests for the managed Memgraph write-transaction boundary."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from neo4j.exceptions import ServiceUnavailable, TransientError

from custom_components.ontology.memgraph_client import (
    RETRY_MAX_ATTEMPTS,
    CannotConnect,
    MemgraphClient,
)


class _ManagedSession:
    """Small execute_write double that exposes commit/rollback outcomes."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.transaction = MagicMock()
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> _ManagedSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute_write(self, work: Any) -> Any:
        if self.failure is not None:
            raise self.failure
        try:
            result = await work(self.transaction)
        except BaseException:
            self.rolled_back = True
            raise
        self.committed = True
        return result


def _client_with_session(session: _ManagedSession) -> MemgraphClient:
    client = MemgraphClient("localhost", 7687)
    driver = MagicMock()
    driver.session.return_value = session
    client._driver = driver
    return client


async def test_execute_write_returns_result_and_commits() -> None:
    session = _ManagedSession()
    client = _client_with_session(session)

    async def _work(transaction: object) -> str:
        assert transaction is session.transaction
        return "written"

    assert await client.execute_write(_work) == "written"
    assert session.committed is True
    assert session.rolled_back is False


async def test_execute_write_rolls_back_and_preserves_callback_exception() -> None:
    session = _ManagedSession()
    client = _client_with_session(session)

    async def _work(_transaction: object) -> None:
        raise ValueError("invalid migration step")

    with pytest.raises(ValueError, match="invalid migration step"):
        await client.execute_write(_work)

    assert session.committed is False
    assert session.rolled_back is True


async def test_execute_write_normalizes_driver_availability_failure() -> None:
    client = _client_with_session(_ManagedSession(ServiceUnavailable("bolt unavailable")))

    with pytest.raises(CannotConnect):
        await client.execute_write(MagicMock())


async def test_execute_write_preserves_cancellation() -> None:
    session = _ManagedSession()
    client = _client_with_session(session)

    async def _work(_transaction: object) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client.execute_write(_work)

    assert session.committed is False
    assert session.rolled_back is True


async def test_run_query_with_retry_retries_transaction_conflicts(monkeypatch) -> None:
    monkeypatch.setattr("custom_components.ontology.memgraph_client.RETRY_INITIAL_DELAY_SECONDS", 0)
    client = MemgraphClient("localhost", 7687)
    calls = 0

    async def _run_query(query: str, parameters: Any = None) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientError("Cannot resolve conflicting transactions")
        return [{"ok": True}]

    monkeypatch.setattr(client, "run_query", _run_query)

    assert await client.run_query_with_retry("RETURN 1") == [{"ok": True}]
    assert calls == 3


async def test_run_query_with_retry_raises_transaction_conflict_when_exhausted(
    monkeypatch,
) -> None:
    monkeypatch.setattr("custom_components.ontology.memgraph_client.RETRY_INITIAL_DELAY_SECONDS", 0)
    client = MemgraphClient("localhost", 7687)
    calls = 0

    async def _run_query(query: str, parameters: Any = None) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        raise TransientError("Cannot resolve conflicting transactions")

    monkeypatch.setattr(client, "run_query", _run_query)

    with pytest.raises(TransientError):
        await client.run_query_with_retry("RETURN 1")
    assert calls == RETRY_MAX_ATTEMPTS
