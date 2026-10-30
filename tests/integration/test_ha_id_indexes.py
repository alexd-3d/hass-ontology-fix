"""ON-035: `ha_id` lookups use label+property indexes (real Memgraph)."""

from __future__ import annotations

from custom_components.ontology import graph_builder
from custom_components.ontology.const import HA_ID_INDEXED_LABELS, LABEL_ENTITY
from custom_components.ontology.memgraph_client import MemgraphClient
from custom_components.ontology.schema_migrations import migrate_schema_if_supported


async def _indexed_ha_id_labels(client: MemgraphClient) -> set[str]:
    rows = await client.run_query("SHOW INDEX INFO")
    return {row["label"] for row in rows if row.get("property") == ["ha_id"]}


async def _plan(client: MemgraphClient, query: str) -> str:
    rows = await client.run_query(f"EXPLAIN {query}")
    return "\n".join(str(next(iter(row.values()))) for row in rows)


async def test_setup_creates_an_ha_id_index_for_every_label(
    memgraph_client: MemgraphClient,
) -> None:
    await migrate_schema_if_supported(memgraph_client)
    await migrate_schema_if_supported(memgraph_client)  # idempotent

    assert set(HA_ID_INDEXED_LABELS) <= await _indexed_ha_id_labels(memgraph_client)


async def test_entity_ha_id_lookup_uses_the_index(memgraph_client: MemgraphClient) -> None:
    await migrate_schema_if_supported(memgraph_client)

    plan = await _plan(
        memgraph_client, f"MERGE (n:{LABEL_ENTITY} {{ha_id: 'sensor.test_power'}}) RETURN n"
    )

    assert "ScanAllByLabelPropert" in plan, plan
    assert "ScanAllByLabel (" not in plan, plan


async def test_indexes_survive_clearing_the_generated_graph(
    memgraph_client: MemgraphClient,
) -> None:
    await migrate_schema_if_supported(memgraph_client)
    await graph_builder.merge_node(memgraph_client, LABEL_ENTITY, "sensor.test_power", {})

    await graph_builder.clear_generated_graph(memgraph_client)

    assert set(HA_ID_INDEXED_LABELS) <= await _indexed_ha_id_labels(memgraph_client)
