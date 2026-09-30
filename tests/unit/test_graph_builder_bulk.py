"""Bulk UNWIND helpers used by the full sync (ON-023)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.ontology.graph_builder import (
    merge_nodes_bulk,
    merge_relationships_bulk,
)


def _sent_rows(client: AsyncMock) -> list[list[dict]]:
    return [call.args[1]["rows"] for call in client.run_query_with_retry.await_args_list]


async def test_nodes_bulk_stamps_source_and_updated_at_on_every_row() -> None:
    client = AsyncMock()
    await merge_nodes_bulk(client, "Area", [("a1", {"name": "Kitchen"}), ("a2", {"name": None})])

    query = client.run_query_with_retry.await_args.args[0]
    assert query.startswith("UNWIND $rows AS row MERGE (n:Area {ha_id: row.ha_id})")
    rows = _sent_rows(client)[0]
    assert [r["ha_id"] for r in rows] == ["a1", "a2"]
    for row in rows:
        assert row["properties"]["source"] == "home_assistant"
        assert row["properties"]["updated_at"]
    assert rows[0]["properties"]["name"] == "Kitchen"


async def test_nodes_bulk_does_not_mutate_callers_properties() -> None:
    props = {"name": "Kitchen"}
    await merge_nodes_bulk(AsyncMock(), "Area", [("a1", props)])
    assert props == {"name": "Kitchen"}


@pytest.mark.parametrize(
    ("count", "expected_batches"),
    [(0, 0), (1, 1), (250, 1), (251, 2), (500, 2), (501, 3)],
)
async def test_nodes_bulk_splits_at_250_rows(count: int, expected_batches: int) -> None:
    client = AsyncMock()
    await merge_nodes_bulk(client, "Entity", [(f"e{i}", {}) for i in range(count)])

    batches = _sent_rows(client)
    assert len(batches) == expected_batches
    assert all(len(batch) <= 250 for batch in batches)
    # Nothing lost or duplicated across the batch boundary.
    assert [r["ha_id"] for batch in batches for r in batch] == [f"e{i}" for i in range(count)]


async def test_nodes_bulk_empty_list_sends_no_query() -> None:
    client = AsyncMock()
    await merge_nodes_bulk(client, "Area", [])
    client.run_query_with_retry.assert_not_awaited()


async def test_relationships_bulk_builds_match_merge_query_and_splits() -> None:
    client = AsyncMock()
    pairs = [(f"d{i}", f"e{i}") for i in range(300)]
    await merge_relationships_bulk(client, "Device", "HAS_ENTITY", "Entity", pairs)

    query = client.run_query_with_retry.await_args_list[0].args[0]
    assert "MATCH (a:Device {ha_id: row.from_id}), (b:Entity {ha_id: row.to_id})" in query
    assert "MERGE (a)-[r:HAS_ENTITY]->(b)" in query
    batches = _sent_rows(client)
    assert [len(b) for b in batches] == [250, 50]
    assert batches[1][-1] == {"from_id": "d299", "to_id": "e299"}
    params = client.run_query_with_retry.await_args_list[0].args[1]
    assert params["source"] == "home_assistant"


async def test_relationships_bulk_empty_list_sends_no_query() -> None:
    client = AsyncMock()
    await merge_relationships_bulk(client, "Device", "HAS_ENTITY", "Entity", [])
    client.run_query_with_retry.assert_not_awaited()


@pytest.mark.parametrize(
    "call",
    [
        lambda c: merge_nodes_bulk(c, "Bad Label}", [("x", {})]),
        lambda c: merge_relationships_bulk(c, "Device", "HAS-ENTITY", "Entity", [("a", "b")]),
        lambda c: merge_relationships_bulk(c, "Device", "HAS_ENTITY", "En tity", [("a", "b")]),
    ],
)
async def test_bulk_helpers_reject_unsafe_labels(call) -> None:
    client = AsyncMock()
    with pytest.raises(ValueError, match="Unsafe Cypher"):
        await call(client)
    client.run_query_with_retry.assert_not_awaited()
