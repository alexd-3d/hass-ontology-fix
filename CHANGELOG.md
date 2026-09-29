# Changelog

All notable changes to the Home Assistant Ontology integration. Version numbers
below track `custom_components/ontology/manifest.json`. See
`memgraph_addon/CHANGELOG.md` for the separate Memgraph add-on/Docker image
changelog.

## 4.5.0

### Added

- **`ontology.mesh_health` service.** A one-call summary of your Zigbee network from the latest scan: routing parents (and any whose children all report an unmeasured link, a sign of a dead or unreachable router), devices with only weak neighbors, the coordinator's link quality, and weak links that the floor plan does not explain. Findings describe the last scan, not live state.
- **Zigbee mesh analysis guide** (`docs/zigbee-mesh-analysis.md`): how to read the mesh data and a quick-diagnosis workflow.

## 4.4.1

### Added

- **Zigbee coordinator device option.** If your Zigbee coordinator is a separate
  device from Zigbee2MQTT's own "Bridge" (for example a network Zigbee adapter),
  pick it in the integration options. Links to the coordinator then use that
  device's floor-plan position, so distance, walls and floor slabs are worked
  out for them too. Leaving it empty keeps the previous behavior.

## 4.4.0

Ontology schema version: `3.3.0` → `3.4.0` (additive; existing graphs remain
valid and are flagged for a routine resync via the existing
`schema_version_mismatch` validation finding).

### Added

- **Zigbee links are now connected to devices.** Each recorded mesh link points
  to the Home Assistant devices at both ends, so the graph can answer which
  devices in a room or area are weakly connected. Links also record whether
  the neighbor is a parent, child or sibling, which is what separates real
  routing paths from the many "audible neighbor" entries every router lists.
- **`ontology.mesh_link_walls` service.** Lists the latest Zigbee links, weakest
  first, with the walls between the two devices (from the floor plan), the
  distance, and an estimated loss. Walls and floor slabs count for more when the
  signal crosses them at an angle. Only parent/child links are shown by default
  (`include_siblings` adds the rest), and a link quality of 0 is flagged as
  "not measured". Needs the optional floor-plan and Zigbee mesh data above.
- Two new options, **floor-to-floor height** and **floor slab attenuation**, used
  for links between floors.

## 4.3.1

### Fixed

- Floor-plan walls, Zigbee mesh snapshots and mesh links now show up in the
  Explorer with readable names, their own colors and icons, and can be
  found by search - previously they appeared as unlabeled gray "Other" nodes.

## 4.3.0

Ontology schema version: `3.2.0` → `3.3.0` (additive; existing graphs remain
valid and are flagged for a routine resync via the existing
`schema_version_mismatch` validation finding).

### Added

- **Nightly Zigbee mesh snapshot.** If the `mqtt` integration is set up,
  a new "Scan Zigbee mesh" button (and matching `ontology.scan_zigbee_mesh`
  service) records the current Zigbee mesh topology and link quality into
  the graph, talking to Zigbee2MQTT directly over MQTT. Also runs
  automatically once a night (configurable hour, or disable the nightly
  run entirely and keep only the manual button/service). Each scan is kept
  as its own snapshot so link-quality history stays queryable, pruned past
  a configurable retention window (30 days by default).
- `sensor.mesh_scan_duration` diagnostic sensor for the new scan.

## 4.2.0

Ontology schema version: `3.1.0` → `3.2.0` (additive; existing graphs remain
valid and are flagged for a routine resync via the existing
`schema_version_mismatch` validation finding).

### Added

- **Optional floor-plan integration with ha-spatial-context.** If the
  optional `spatial_context` integration is installed, a new "Resync spatial
  layout" button (and matching `ontology.sync_spatial_layout` service) pulls
  wall material/thickness/attenuation and exact device pin coordinates from
  its floor plan into the graph. Installs without spatial_context are
  unaffected — the sync is a no-op.
- `sensor.spatial_sync_duration` diagnostic sensor for the new sync.
- Two new validation checks: a floor plan not yet mapped in spatial_context,
  and an entity on a mapped floor that isn't pinned yet.

## 4.1.1

### Fixed

- **Search matched only a whole literal phrase.** `ontology.search`'s `term`
  compared the entire input as one literal substring against each name/ID,
  so a natural multi-word query (e.g. "back motion") matched nothing unless
  it appeared verbatim as a single substring somewhere. The term is now
  split into words, each of which must match independently, so multi-word
  queries work as expected. Matching itself is unchanged for single-word
  terms and stays a literal, language-agnostic substring match.

## 4.1.0

Forked from `hannovdm/hass-ontology` at `v4.0.0`. This is the fork's first
tracked release, consolidating every stabilization, performance, and Explorer
fix made since the fork — the integration version was not bumped for any of
the individual changes below, so they are documented together here.

Ontology schema version: `3.0.0` → `3.1.0` (additive; existing graphs remain
valid and are flagged for a routine resync via the existing
`schema_version_mismatch` validation finding).

### Added

- Configurable state-change sync debounce (`state_change_debounce_seconds`)
  and entity/domain exclusion options, so noisy fast-changing sensors can be
  excluded from triggering a graph sync at all.
- `sensor.sync_activity` diagnostic sensor exposing per-batch sync duration,
  batch size, and debounce-event counters.
- CI workflow guarding the GraphQL add-on's dependency graph, after a
  build-breaking dependency conflict was found already merged upstream.
- **Manufacturer-based classification and a new `Camera` type.** Semantic
  classification can now match an entity by its device's manufacturer, in
  addition to the existing domain/device-class/keyword matching. Camera and
  NVR hardware is classified accordingly: AI object-detection sensors are no
  longer misclassified by keyword coincidence (e.g. a "vehicle detected"
  binary sensor is no longer tagged as a `Vehicle`) while retaining any
  legitimate secondary classification (e.g. `OccupancySensor` for a motion
  detector).
- **Node health indicators.** Nodes that are currently unavailable or
  reporting a low battery level are now highlighted with a distinct color and
  icon in both the graph view and the node list, making degraded devices
  identifiable at a glance.

### Fixed

- **Validation report inflated by two self-inflicted findings.** Every
  validation finding's own internal graph relationship was written without
  a `source` property, which the validator's own relationship-integrity
  check then flagged as a defect on every run - accounting for the large
  majority of all reported errors. Separately, the duplicate-entity check
  compared entities by display name only, without regard to domain, so a
  single physical device exposing two entities under one friendly name
  (e.g. a dimmer's `switch` and `light` entities) was reported as a naming
  collision. Both are fixed; only genuine naming collisions and real graph
  defects are now reported.
- **Sync overload under fast-changing sensors.** State-change-triggered graph
  synchronization was debounced per entity rather than in aggregate: a house
  with several sensors updating every 1-3 seconds produced near-continuous
  resyncs and sustained elevated host load. Synchronization is now batched
  across all entities pending within a single debounce window, with the
  batch serialized against concurrent runs.
- **GraphQL Explorer permanently unavailable.** Numeric query parameters
  (result limits, pagination bounds) were serialized as floating-point
  values by the JavaScript GraphQL driver, while the graph engine requires
  integer parameters for list-slicing and limit clauses — every Explorer
  query failed before reaching the database. Parameters are now explicitly
  typed as integers.
- **Explorer showing an incomplete node tree on expansion.** Node-expansion
  requests shared a single fixed request budget across an entire expansion
  cascade; a single entity-heavy device could exhaust that shared budget and
  prevent every other device in the same area from expanding. Removed the
  redundant per-entity expansion step responsible for the overconsumption.
- **Explorer node/relationship expansion failing on every call.** A
  relationship's source-class value was persisted in a different case than
  the GraphQL schema's enum declaration, causing response serialization to
  throw on the first relationship any expansion query returned. Values are
  now normalized before serialization, with unrecognized values degrading
  gracefully instead of failing the request.
- **Options changes causing an intermittent reload race.** Saving
  integration options through the config flow silently discarded the
  just-saved values immediately after persisting them, triggering a second,
  redundant reload that raced the first and occasionally produced graph
  transaction conflicts. The options flow now completes without triggering
  the redundant automatic reset.
- **Unnecessary full-graph scans on every sync batch.** Two full-graph count
  queries ran unconditionally on every sync batch regardless of whether
  either count could have changed, and a full rebuild of every energy-role
  relationship binding ran once per entity rather than once per batch. Both
  are now skipped unless a relevant change actually occurred within that
  batch, reducing typical batch duration roughly sixfold and eliminating
  essentially all idle database load.
- Eight semantic classification types were missing from the GraphQL
  node-type mapping and rendered as generic, untyped nodes in the Explorer,
  affecting roughly one node in five graph-wide. Each type now resolves to
  its own icon and color.
- Semantic classification nodes were created without a display name, so the
  Explorer showed only their raw internal identifier. They now inherit the
  underlying entity's friendly name.
- Removed legend text describing an "unavailable nodes are dimmed" behavior
  that had never been implemented.

## Pre-fork history

Not tracked in this file — see the upstream project's history at
`hannovdm/hass-ontology`.
