# Analysing a Zigbee mesh with the ontology

A guide for people and agents on getting a quick, evidence-based picture of a
Zigbee2MQTT network from the ontology graph. Everything here is read-only.

## What you need

| Piece | Why | Required? |
| --- | --- | --- |
| `mqtt` integration + Zigbee2MQTT | The scan asks Z2M for its network map over MQTT | Yes |
| At least one **Scan Zigbee mesh** run (button, `ontology.scan_zigbee_mesh`, or the nightly job) | Records a `MeshSnapshot` with all `MeshLink`s | Yes |
| `ha-spatial-context` + **Resync spatial layout** | Wall/pin geometry, so links get distance, walls and slabs | Optional, but it turns "this link is weak" into "and here is why" |
| Options **floor-to-floor height** and **floor slab attenuation** | Cross-floor distance and slab loss | Optional (defaults are generic; set your own) |
| Option **Zigbee coordinator device** | Set it when the coordinator radio is a separate HA device from Z2M's "Bridge" (e.g. a network adapter), so coordinator links use that device's floor-plan position | Optional |

Snapshots are kept for the configured retention (30 days by default). A scan
takes a couple of minutes.

## Fastest path: `ontology.mesh_health`

One call answers the usual questions. Response service, so pass
`return_response: true`.

```yaml
action: ontology.mesh_health
data:
  weak_lqi: 40   # optional, default 40
  limit: 20      # optional
response_variable: health
```

It returns, from the latest snapshot:

- `parents` and `suspect_parents` - routing parents ranked by child count, and the
  ones whose children **all** report an unmeasured link (LQI 0). That pattern
  usually means a dead or unreachable router that neighbors still list.
- `weak_devices` - devices whose best measured neighbor is at or below `weak_lqi`.
- `coordinator` - number, average and minimum LQI of the links that hear the coordinator.
- `unexplained_weak_links` - weak measured same-floor links the floor plan does *not*
  explain (short, lightly obstructed path). These are the ones worth investigating.
  Links through a floor slab are left out (the slab is the explanation) and only
  counted in `summary.weak_cross_floor_links`.
- `summary` counts, and `warnings` when some links could not be checked
  against the floor plan (a device is not pinned).

## Drill down: `ontology.mesh_link_walls`

Per-link detail, weakest first. By default only parent/child links are listed;
add `include_siblings: true` for the full neighbor table.

Each link carries: `lqi`, `lqi_measured` (false when LQI is 0), `relationship_name`
(parent/child/sibling), `same_floor`, `distance_m` (3D, using pin heights and the
floor height), `walls_crossed` + `wall_attenuation_db` (same floor), `slabs_crossed`
+ `slab_attenuation_db` (across floors), `free_space_loss_db`, `expected_loss_db`.
Wall and slab loss are scaled up when the path crosses them at a slant, capped at 3x.
A path that goes through a door or window drawn in the floor plan is priced as that
opening (3 dB for a door, 2 dB for a window) instead of the wall around it;
`openings_crossed` counts those (they are still part of `walls_crossed`).

## Reading the data correctly

- **Most links are noise.** A Zigbee neighbor table lists every audible
  router, so `sibling` links are the bulk (roughly 95% on a 60-device network). The
  real routing tree is the `parent`/`child` links. Hence the default filter.
- **LQI 0 means "not measured", not "worst".** A parent with LQI 0 is a red
  flag; a sibling with LQI 0 usually is not.
- **Link direction:** in these snapshots the link's *source* is the neighbor and
  the *target* owns the table; for `relationship = parent` the source is the
  parent of the target.
- **`expected_loss_db` is not comparable to LQI in dB** (LQI is not linear). Use it to
  rank and to spot outliers: high LQI with huge expected loss, or a low LQI with a short,
  clear path.
- **Snapshots are not live.** The graph shows the state at the last scan. Before
  calling a device dead, check Home Assistant for `unavailable`. A device that
  is `unavailable` yet still listed as a parent by others (with LQI 0) is a
  "ghost" in the neighbor tables.
- **Unpinned devices have no geometry.** Their links show null distance/walls.
  Pin them in ha-spatial-context and resync.

## Useful Cypher (via `ontology.query`)

Latest snapshot, parents by child count:

```cypher
MATCH (s:MeshSnapshot) WITH s ORDER BY s.scanned_at DESC LIMIT 1
MATCH (s)-[:HAS_LINK]->(l:MeshLink) WHERE l.relationship_name = 'parent'
RETURN l.from_name AS parent, count(*) AS children,
       sum(CASE WHEN l.lqi = 0 THEN 1 ELSE 0 END) AS unmeasured
ORDER BY children DESC
```

Links of one device, with the Device nodes they resolve to:

```cypher
MATCH (l:MeshLink)-[:TO_DEVICE]->(d:Device {name: $name})
OPTIONAL MATCH (l)-[:FROM_DEVICE]->(n:Device)
RETURN n.name, l.lqi, l.relationship_name ORDER BY l.lqi
```

Graph model: `(:MeshSnapshot)-[:HAS_LINK]->(:MeshLink)`, and each `MeshLink` has
`FROM_DEVICE` / `TO_DEVICE` edges to `Device` nodes. Wall geometry is
`(:Wall)-[:ON_FLOOR]->(:Floor)`; device positions are `PINNED_ON_FLOOR` edges
(`x`, `y`, `z`) from an `Entity` to its `Floor`.

## Suggested workflow for an agent

1. Confirm a recent snapshot exists (`mesh_health` returns empty with a warning otherwise).
2. Call `ontology.mesh_health`. Report `suspect_parents` and `unexplained_weak_links` first.
3. For each suspect device, check its Home Assistant availability and last-seen
   before concluding anything.
4. Drill into specific devices with `mesh_link_walls` or the Cypher above.
5. State the snapshot time, since findings describe the last scan.
