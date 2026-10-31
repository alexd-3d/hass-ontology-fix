"""ON-040: pruning non-tree links from older mesh snapshots (real Memgraph)."""

from __future__ import annotations

from custom_components.ontology import mesh_sync, query_tools
from custom_components.ontology.memgraph_client import MemgraphClient

# relationship codes: 0 parent, 1 child, 2 sibling, 3 none, 4 previous child, None unknown
_RELATIONSHIPS = (0, 1, 2, 3, 4, None)


async def _seed_snapshot(client: MemgraphClient, snapshot_id: str, scanned_at: str) -> None:
    await client.run_query(
        "CREATE (:MeshSnapshot {ha_id: $id, scanned_at: $at, name: $id})",
        {"id": snapshot_id, "at": scanned_at},
    )
    await client.run_query(
        "UNWIND $rows AS row "
        "MATCH (s:MeshSnapshot {ha_id: $id}) "
        "CREATE (s)-[:HAS_LINK]->(:MeshLink {ha_id: row.link_id, from_name: 'A', "
        "to_name: 'B', lqi: 100, relationship: row.rel}) ",
        {
            "id": snapshot_id,
            "rows": [
                {"link_id": f"{snapshot_id}-{i}", "rel": rel}
                for i, rel in enumerate(_RELATIONSHIPS)
            ],
        },
    )
    # Device edges, to prove DETACH DELETE removes them with their links.
    await client.run_query(
        "MERGE (d:Device {ha_id: 'dev-1'}) WITH d "
        "MATCH (:MeshSnapshot {ha_id: $id})-[:HAS_LINK]->(l:MeshLink) "
        "CREATE (l)-[:FROM_DEVICE]->(d), (l)-[:TO_DEVICE]->(d)",
        {"id": snapshot_id},
    )


async def _relationships_in(client: MemgraphClient, snapshot_id: str) -> list:
    rows = await client.run_query(
        "MATCH (:MeshSnapshot {ha_id: $id})-[:HAS_LINK]->(l:MeshLink) "
        "RETURN l.relationship AS rel ORDER BY l.ha_id",
        {"id": snapshot_id},
    )
    return [row["rel"] for row in rows]


async def _seed_three(client: MemgraphClient) -> None:
    await _seed_snapshot(client, "mesh-old1", "2026-01-01T00:00:00+00:00")
    await _seed_snapshot(client, "mesh-old2", "2026-01-02T00:00:00+00:00")
    await _seed_snapshot(client, "mesh-new", "2026-01-03T00:00:00+00:00")


async def test_older_snapshots_keep_only_tree_and_unknown_links(
    memgraph_client: MemgraphClient,
) -> None:
    await _seed_three(memgraph_client)

    deleted = await mesh_sync._async_prune_history_siblings(memgraph_client, "mesh-new")

    assert deleted == 6  # sibling, none, previous-child in each of the two older snapshots
    for old in ("mesh-old1", "mesh-old2"):
        assert await _relationships_in(memgraph_client, old) == [0, 1, None]
    assert await _relationships_in(memgraph_client, "mesh-new") == list(_RELATIONSHIPS)


async def test_prune_leaves_snapshots_devices_and_orphans_alone(
    memgraph_client: MemgraphClient,
) -> None:
    await _seed_three(memgraph_client)

    await mesh_sync._async_prune_history_siblings(memgraph_client, "mesh-new")

    snapshots = await memgraph_client.run_query("MATCH (s:MeshSnapshot) RETURN count(s) AS c")
    devices = await memgraph_client.run_query("MATCH (d:Device) RETURN count(d) AS c")
    orphans = await memgraph_client.run_query(
        "MATCH (l:MeshLink) WHERE NOT (:MeshSnapshot)-[:HAS_LINK]->(l) RETURN count(l) AS c"
    )
    dangling = await memgraph_client.run_query(
        "MATCH (l:MeshLink) WHERE l.relationship IN [2, 3, 4] AND NOT l.ha_id STARTS WITH "
        "'mesh-new' RETURN count(l) AS c"
    )
    assert snapshots == [{"c": 3}]
    assert devices == [{"c": 1}]
    assert orphans == [{"c": 0}]
    assert dangling == [{"c": 0}]


async def test_second_prune_deletes_nothing(memgraph_client: MemgraphClient) -> None:
    await _seed_three(memgraph_client)
    await mesh_sync._async_prune_history_siblings(memgraph_client, "mesh-new")

    assert await mesh_sync._async_prune_history_siblings(memgraph_client, "mesh-new") == 0


async def test_prune_works_across_several_batches(
    memgraph_client: MemgraphClient, monkeypatch
) -> None:
    monkeypatch.setattr(mesh_sync, "_WRITE_BATCH_SIZE", 2)
    await _seed_three(memgraph_client)

    deleted = await mesh_sync._async_prune_history_siblings(memgraph_client, "mesh-new")

    assert deleted == 6
    assert await _relationships_in(memgraph_client, "mesh-old1") == [0, 1, None]


async def test_latest_snapshot_tools_return_the_same_result_after_pruning(
    memgraph_client: MemgraphClient,
) -> None:
    await _seed_three(memgraph_client)
    before = {
        "tree": await query_tools.mesh_link_walls(memgraph_client),
        "all": await query_tools.mesh_link_walls(memgraph_client, include_siblings=True),
        "health": await query_tools.mesh_health(memgraph_client),
    }

    await mesh_sync._async_prune_history_siblings(memgraph_client, "mesh-new")

    assert await query_tools.mesh_link_walls(memgraph_client) == before["tree"]
    assert await query_tools.mesh_link_walls(memgraph_client, include_siblings=True) == before["all"]
    assert await query_tools.mesh_health(memgraph_client) == before["health"]
