"""Unit tests for the exact-predecessor ontology schema migration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.ontology.const import (
    HA_ID_INDEXED_LABELS,
    SCHEMA_PREVIOUS_VERSION,
    SCHEMA_SINGLETON_ID,
    SCHEMA_VERSION,
)
from custom_components.ontology.memgraph_client import CannotConnect
from custom_components.ontology.schema_migrations import migrate_schema_if_supported

_ALL_LABELS = tuple(dict.fromkeys(HA_ID_INDEXED_LABELS))


def _all_indexed_rows() -> list[dict[str, object]]:
    return [
        {"index type": "label+property", "label": label, "property": ["ha_id"], "count": 0}
        for label in _ALL_LABELS
    ]


def _create_index_queries(client: AsyncMock) -> list[str]:
    return [
        call.args[0]
        for call in client.run_query.await_args_list
        if call.args[0].startswith("CREATE INDEX")
    ]


class _Transaction:
    def __init__(self, version: str | None) -> None:
        self.version = version
        self.queries: list[tuple[str, dict[str, object]]] = []

    async def run(self, query: str, parameters: dict[str, object] | None = None):
        self.queries.append((query, parameters or {}))
        if "RETURN s.version AS version" in query:
            return _Result(self.version)
        if "target_version" in (parameters or {}):
            self.version = str((parameters or {})["target_version"])
        return _Result(None)


class _Result:
    def __init__(self, version: str | None) -> None:
        self._version = version

    async def single(self):
        return None if self._version is None else {"version": self._version}


@pytest.mark.parametrize("version", [None, SCHEMA_VERSION])
async def test_migration_is_noop_for_empty_or_current_schema(version: str | None) -> None:
    client = AsyncMock()
    version_rows = [] if version is None else [{"version": version}]
    client.run_query.side_effect = [version_rows, _all_indexed_rows()]

    assert await migrate_schema_if_supported(client) == version
    client.execute_write.assert_not_awaited()
    assert _create_index_queries(client) == []


async def test_migration_advances_only_exact_predecessor_in_one_transaction() -> None:
    transaction = _Transaction(SCHEMA_PREVIOUS_VERSION)
    client = AsyncMock()

    async def execute_write(work):
        return await work(transaction)

    client.run_query.side_effect = [
        [{"version": SCHEMA_PREVIOUS_VERSION}],
        [],
    ] + [[] for _ in _ALL_LABELS]
    client.execute_write.side_effect = execute_write

    assert await migrate_schema_if_supported(client) == SCHEMA_VERSION
    assert transaction.version == SCHEMA_VERSION
    assert "CREATE INDEX ON :SupplyAssociation(ha_id)" in _create_index_queries(client)
    assert "CREATE INDEX ON :EnergyRoleAssignment(ha_id)" in _create_index_queries(client)
    assert transaction.queries[-1][1] == {
        "schema_id": SCHEMA_SINGLETON_ID,
        "expected_version": SCHEMA_PREVIOUS_VERSION,
        "target_version": SCHEMA_VERSION,
        "migrated_at": transaction.queries[-1][1]["migrated_at"],
    }
    assert transaction.queries[-1][0].rstrip().endswith("s.version = $target_version")


async def test_migration_leaves_unsupported_predecessor_for_mismatch_repair() -> None:
    client = AsyncMock()
    client.run_query.return_value = [{"version": "1.9.0"}]

    assert await migrate_schema_if_supported(client) == "1.9.0"
    client.execute_write.assert_not_awaited()
    client.run_query.assert_awaited_once()


async def test_migration_callback_failure_propagates_for_rollback() -> None:
    client = AsyncMock()
    client.run_query.side_effect = [
        [{"version": SCHEMA_PREVIOUS_VERSION}],
        _all_indexed_rows(),
    ]
    client.execute_write.side_effect = RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        await migrate_schema_if_supported(client)


async def test_every_ha_id_label_gets_exactly_one_index_when_none_exist() -> None:
    client = AsyncMock()
    client.run_query.side_effect = [[], []] + [[] for _ in _ALL_LABELS]

    await migrate_schema_if_supported(client)

    assert _create_index_queries(client) == [
        f"CREATE INDEX ON :{label}(ha_id)" for label in _ALL_LABELS
    ]


async def test_only_missing_indexes_are_created() -> None:
    present = _all_indexed_rows()[:5]
    missing = _ALL_LABELS[5:]
    client = AsyncMock()
    client.run_query.side_effect = [[], present] + [[] for _ in missing]

    await migrate_schema_if_supported(client)

    assert _create_index_queries(client) == [
        f"CREATE INDEX ON :{label}(ha_id)" for label in missing
    ]


async def test_index_on_other_property_does_not_count_as_ha_id_index() -> None:
    rows = _all_indexed_rows()
    rows[0] = {**rows[0], "property": ["name"]}
    client = AsyncMock()
    client.run_query.side_effect = [[], rows, []]

    await migrate_schema_if_supported(client)

    assert _create_index_queries(client) == [f"CREATE INDEX ON :{_ALL_LABELS[0]}(ha_id)"]


async def test_one_failed_index_does_not_stop_the_rest_or_raise() -> None:
    client = AsyncMock()
    client.run_query.side_effect = (
        [[], []] + [RuntimeError("transient")] + [[] for _ in _ALL_LABELS[1:]]
    )

    assert await migrate_schema_if_supported(client) is None
    assert len(_create_index_queries(client)) == len(_ALL_LABELS)


async def test_connection_errors_during_index_creation_propagate() -> None:
    client = AsyncMock()
    client.run_query.side_effect = [[], [], CannotConnect("down")]

    with pytest.raises(CannotConnect):
        await migrate_schema_if_supported(client)


def test_indexed_label_list_is_the_large_labels_only() -> None:
    # Large or bulk-looked-up labels are indexed ...
    assert {"Entity", "Device", "MeshLink", "ValidationFinding"} <= set(HA_ID_INDEXED_LABELS)
    # ... the two pre-existing energy indexes stay ...
    assert {"SupplyAssociation", "EnergyRoleAssignment"} <= set(HA_ID_INDEXED_LABELS)
    # ... and small labels are deliberately not indexed (index memory buys nothing).
    assert not {"Home", "Floor", "Area", "Domain", "Integration", "Label", "Wall"} & set(
        HA_ID_INDEXED_LABELS
    )
    assert len(_ALL_LABELS) == len(HA_ID_INDEXED_LABELS)
