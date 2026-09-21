"""DataUpdateCoordinator for the Ontology integration.

The four explicit sync services (rebuild, resync, single-entity sync,
validate) are serialized through a single lock plus a one-deep pending
queue, rejecting a third+ concurrent request (FR-013a / research.md §8 /
contracts/services.md).

Event-driven incremental updates (registry changes, debounced primary-state
changes) share the same underlying lock so a write is never concurrent with
a full sync, but they wait their turn instead of being rejected: real
installations routinely have many different entities change primary state
within the same few seconds, and treating that as an error only pushed the
same contention into failed_updates for a later retry to re-race (FR-011,
FR-020).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import (
    graph_builder,
    overrides,
    query_service,
    semantic_classifier,
    user_knowledge,
    validation,
)
from .const import (
    CONF_AUTO_CLASSIFY,
    DEFAULT_AUTO_CLASSIFY,
    DOMAIN,
    GRAPH_REVISION_BUFFER_SIZE,
    HEALTH_ERROR,
    HEALTH_OK,
    HEALTH_UNAVAILABLE,
    SUSTAINED_FAILURE_THRESHOLD,
    SYNC_ACTIVITY_WINDOW_MINUTES,
)
from .memgraph_client import MemgraphClient
from .redact import redact_exception

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass
class GraphChangeEvent:
    """A sanitized, property-name-only change envelope published after a successful write."""

    revision: int
    kind: str  # "upsert" | "remove" | "reconcile"
    node_ids: list[str]
    relationship_ids: list[str]
    changed_properties: list[str]  # Names only, never values
    occurred_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "kind": self.kind,
            "node_ids": self.node_ids,
            "relationship_ids": self.relationship_ids,
            "changed_properties": self.changed_properties,
            "occurred_at": self.occurred_at,
        }


class GraphChangeBuffer:
    """Bounded revision buffer with subscriber callbacks for live graph updates (US3).

    Monotonically increments revision on each publish call.  Bounded to
    ``max_size`` events using a deque; oldest events are evicted when full.
    Callers use ``events_since`` to replay or detect a buffer gap (returns
    ``None`` when the requested revision is no longer in the buffer).
    """

    def __init__(self, max_size: int = GRAPH_REVISION_BUFFER_SIZE) -> None:
        self.max_size = max_size
        self._revision = 0
        self._events: deque[GraphChangeEvent] = deque(maxlen=max_size)
        self._subscribers: dict[str, Callable[[GraphChangeEvent], None]] = {}

    @property
    def current_revision(self) -> int:
        return self._revision

    def _next_revision(self) -> int:
        self._revision += 1
        return self._revision

    def _publish(self, event: GraphChangeEvent) -> GraphChangeEvent:
        self._events.append(event)
        # ON-004: renamed from `callback` - this file now also imports HA's
        # `callback` decorator (for async_publish_sync_activity), which that
        # name was shadowing.
        for subscriber in list(self._subscribers.values()):
            try:
                subscriber(event)
            except Exception:  # noqa: BLE001
                pass
        return event

    def publish_upsert(
        self,
        node_ids: list[str],
        relationship_ids: list[str],
        changed_properties: list[str],
    ) -> GraphChangeEvent:
        """Publish an upsert event after a successful write."""
        return self._publish(
            GraphChangeEvent(
                revision=self._next_revision(),
                kind="upsert",
                node_ids=list(node_ids),
                relationship_ids=list(relationship_ids),
                changed_properties=list(changed_properties),
                occurred_at=datetime.now(UTC).isoformat(),
            )
        )

    def publish_remove(
        self,
        node_ids: list[str],
        relationship_ids: list[str],
    ) -> GraphChangeEvent:
        """Publish a remove event after nodes/relationships are deleted."""
        return self._publish(
            GraphChangeEvent(
                revision=self._next_revision(),
                kind="remove",
                node_ids=list(node_ids),
                relationship_ids=list(relationship_ids),
                changed_properties=[],
                occurred_at=datetime.now(UTC).isoformat(),
            )
        )

    def publish_reconcile(self) -> GraphChangeEvent:
        """Publish a reconcile event (e.g. after full rebuild/resync)."""
        return self._publish(
            GraphChangeEvent(
                revision=self._next_revision(),
                kind="reconcile",
                node_ids=[],
                relationship_ids=[],
                changed_properties=[],
                occurred_at=datetime.now(UTC).isoformat(),
            )
        )

    def events_since(self, revision: int) -> list[GraphChangeEvent] | None:
        """Return events after ``revision``, or ``None`` when the buffer cannot bridge.

        Returns ``None`` when:
        - the requested revision is beyond the current revision (future revision), or
        - there is a gap: events between ``revision`` and the oldest buffered event
          have been evicted (``revision < oldest_revision - 1``).

        If ``oldest_revision == revision + 1``, all events since ``revision`` are
        still present and we return them.
        """
        if revision > self._revision:
            return None
        if not self._events:
            return []
        oldest_revision = self._events[0].revision
        if revision < oldest_revision - 1:
            return None
        return [event for event in self._events if event.revision > revision]

    def subscribe(
        self, subscriber_id: str, callback: Callable[[GraphChangeEvent], None]
    ) -> None:
        """Register a callback to be invoked on every new event."""
        self._subscribers[subscriber_id] = callback

    def unsubscribe(self, subscriber_id: str) -> None:
        """Remove a subscriber callback; silently ignored if not registered."""
        self._subscribers.pop(subscriber_id, None)


@dataclass
class PendingUpdate:
    """A single queued incremental-update callable awaiting its turn."""

    kind: str
    func: Any
    args: tuple = ()
    attempts: int = 0


@dataclass
class OntologyState:
    """Observable state surfaced by the health sensors (User Story 6)."""

    health: str = HEALTH_UNAVAILABLE
    node_count: int = 0
    relationship_count: int = 0
    last_sync: str | None = None
    last_error: str | None = None
    schema_version: str | None = None
    consecutive_failures: int = 0
    failed_updates: list[dict[str, Any]] = field(default_factory=list)
    validation_findings: dict[str, int] = field(default_factory=dict)
    # ON-004: rolling sync-load snapshot, recomputed periodically from the
    # cheap bucket counters below - see OntologyCoordinator._activity_*.
    sync_activity_batches: int = 0
    sync_activity_events: int = 0
    sync_activity_avg_batch_size: float = 0.0
    sync_activity_last_batch_ms: float | None = None


class OperationInProgress(Exception):
    """Raised when a sync is requested while the single pending slot is
    already occupied (FR-013a, research.md §8)."""


class OntologyCoordinator(DataUpdateCoordinator[OntologyState]):
    """Owns the Memgraph client and serializes all graph-write operations."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: MemgraphClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN)
        self.hass = hass
        self.entry = entry
        self.memgraph_client = client
        self.state = OntologyState()
        self._lock = asyncio.Lock()
        self._waiting = False
        self.change_buffer = GraphChangeBuffer()
        # Optional callbacks wired by __init__.py to repairs.py (User Story 9).
        self.on_sustained_failure: Any = None
        self.on_failure_cleared: Any = None
        # ON-004: one counter bucket per minute, circular over the tracked
        # window. Recording is a single list-index increment (no per-event
        # timestamp storage/pruning); __init__.py publishes a snapshot into
        # `self.state.sync_activity_*` on a fixed timer (see
        # async_publish_sync_activity), decoupled from event/batch volume.
        self._activity_event_buckets = [0] * SYNC_ACTIVITY_WINDOW_MINUTES
        self._activity_batch_buckets = [0] * SYNC_ACTIVITY_WINDOW_MINUTES
        self._activity_synced_buckets = [0] * SYNC_ACTIVITY_WINDOW_MINUTES
        self._activity_bucket_minute: int | None = None

    async def _async_update_data(self) -> OntologyState:
        """Initial full sync: build the graph directly, no clear step (T038)."""
        try:
            await self._execute_full_sync(clear_first=False)
        except Exception as err:  # noqa: BLE001 - convert to UpdateFailed for HA
            raise UpdateFailed(redact_exception(err)) from err
        return self.state

    def _record_success(self) -> None:
        self.state.health = HEALTH_OK
        self.state.last_error = None
        self.state.consecutive_failures = 0
        self.state.last_sync = datetime.now(UTC).isoformat()
        if self.on_failure_cleared:
            self.on_failure_cleared()
        self._notify_state_changed()

    def _record_failure(self, err: Exception) -> None:
        self.state.health = HEALTH_ERROR
        self.state.last_error = redact_exception(err)
        self.state.consecutive_failures += 1
        if (
            self.state.consecutive_failures >= SUSTAINED_FAILURE_THRESHOLD
            and self.on_sustained_failure
        ):
            self.on_sustained_failure()
        self._notify_state_changed()

    def _notify_state_changed(self) -> None:
        """Push the current state to sensor entities.

        This integration never calls ``async_config_entry_first_refresh``/
        ``async_refresh`` (the initial sync runs as a background task per
        T038, and incremental updates bypass the coordinator's own update
        cycle entirely), so ``DataUpdateCoordinator`` would otherwise never
        notify its listeners. Without this, the sensor entities freeze at
        whatever ``self.state`` was when they were first added to hass
        (typically all-zero counts) and never reflect subsequent syncs.
        """
        self.async_set_updated_data(self.state)

    # -- Sync-activity tracking (ON-004) -------------------------------------
    #
    # Cheap-by-construction: recording is one list-index increment (no
    # timestamp log, nothing to prune), and the sensor's HA state is only
    # published on a fixed timer (see async_publish_sync_activity, wired by
    # __init__.py) rather than on every event/batch - so the metric's own
    # overhead can't scale with how busy the integration is.

    def _activity_roll(self, now_minute: int) -> None:
        """Zero out any buckets that have aged out of the tracked window
        since the last recorded minute, including the case where nothing at
        all happened for longer than the window (so the snapshot decays back
        to zero instead of showing stale data forever)."""
        last = self._activity_bucket_minute
        if last is None:
            self._activity_bucket_minute = now_minute
            return
        elapsed = now_minute - last
        if elapsed <= 0:
            return  # Same minute (or a clock oddity) - nothing to roll.
        window = SYNC_ACTIVITY_WINDOW_MINUTES
        for step in range(1, min(elapsed, window) + 1):
            idx = (last + step) % window
            self._activity_event_buckets[idx] = 0
            self._activity_batch_buckets[idx] = 0
            self._activity_synced_buckets[idx] = 0
        self._activity_bucket_minute = now_minute

    def record_sync_event(self) -> None:
        """Count one raw `state_changed` event accepted by the debouncer,
        before batching - the "pressure" figure, independent of how many
        events end up coalesced into a single batch."""
        now_minute = int(time.time() // 60)
        self._activity_roll(now_minute)
        self._activity_event_buckets[now_minute % SYNC_ACTIVITY_WINDOW_MINUTES] += 1

    def record_sync_batch(self, duration_ms: float, batch_size: int) -> None:
        """Count one executed sync batch, how many entities it covered, and
        its wall-clock duration (the last two only need a plain +=/assign,
        no extra bucket beyond what's already being rolled)."""
        now_minute = int(time.time() // 60)
        self._activity_roll(now_minute)
        idx = now_minute % SYNC_ACTIVITY_WINDOW_MINUTES
        self._activity_batch_buckets[idx] += 1
        self._activity_synced_buckets[idx] += batch_size
        self.state.sync_activity_last_batch_ms = round(duration_ms, 1)

    @callback
    def async_publish_sync_activity(self, _now: datetime | None = None) -> None:
        """Recompute the rolling snapshot and push it to the sensor (called on
        a fixed interval by __init__.py, not on every event/batch)."""
        now_minute = int(time.time() // 60)
        self._activity_roll(now_minute)
        events = sum(self._activity_event_buckets)
        batches = sum(self._activity_batch_buckets)
        synced = sum(self._activity_synced_buckets)
        self.state.sync_activity_events = events
        self.state.sync_activity_batches = batches
        self.state.sync_activity_avg_batch_size = round(synced / batches, 1) if batches else 0.0
        self._notify_state_changed()

    async def _refresh_counts(self) -> None:
        """Refresh the node/relationship count sensors (User Story 6)."""
        node_rows = await self.memgraph_client.run_query("MATCH (n) RETURN count(n) AS c")
        rel_rows = await self.memgraph_client.run_query("MATCH ()-[r]->() RETURN count(r) AS c")
        self.state.node_count = node_rows[0]["c"] if node_rows else 0
        self.state.relationship_count = rel_rows[0]["c"] if rel_rows else 0

    async def _run_serialized(self, func: Any, *args: Any, **kwargs: Any) -> None:
        """Serialize execution through a single lock plus a one-deep pending
        slot: a second concurrent request is queued, a third is rejected
        (FR-013a, research.md §8). Used only by the four explicit sync
        services (rebuild/resync/sync_entity/validate) - see
        `_run_incremental` for event-driven updates."""
        if self._lock.locked():
            if self._waiting:
                raise OperationInProgress("An ontology sync operation is already in progress")
            self._waiting = True
        try:
            async with self._lock:
                self._waiting = False
                await func(*args, **kwargs)
        finally:
            self._waiting = False

    async def _run_incremental(self, func: Any, *args: Any, **kwargs: Any) -> None:
        """Serialize a registry/state-change-driven incremental update through
        the same lock as the heavy sync services, but wait for a turn instead
        of instantly rejecting.

        Unlike `_run_serialized`, this has no one-deep pending-slot limit:
        `asyncio.Lock` is FIFO, so any number of concurrently-arriving
        incremental updates simply queue up and run one at a time, without
        ever raising `OperationInProgress` or starving a pending heavy
        operation.
        """
        async with self._lock:
            await func(*args, **kwargs)

    async def async_run_operation(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run an arbitrary operation through the same single-pending-slot
        serialization as rebuild/resync/sync_entity/validate (FR-013a).

        Generic entry point reused by classify/query/validate/refresh/
        import-export (User Stories 1, 2, 5, 6, 7) so none of them ever run
        concurrently with each other, with rebuild/resync, or with an
        in-flight incremental update (contracts/services.md).
        """
        result_box: dict[str, Any] = {}

        async def _wrapped(*a: Any, **kw: Any) -> None:
            result_box["value"] = await func(*a, **kw)

        await self._run_serialized(_wrapped, *args, **kwargs)
        return result_box.get("value")

    # -- Full-graph operations (User Story 4) -----------------------------

    async def _execute_full_sync(self, *, clear_first: bool) -> None:
        try:
            overrides_payload: dict[str, Any] | None = None
            if clear_first:
                # `clear_generated_graph` deletes every `home_assistant`/
                # `generated` sourced node via DETACH DELETE, which also
                # removes any `source = "user"` override relationship
                # incident to that node (a relationship cannot outlive its
                # endpoints). Round-trip overrides across the clear so
                # rebuild preservation stays unconditional (FR-006, FR-025,
                # Constitution Principle V).
                overrides_payload = await overrides.async_export_overrides(self.memgraph_client)
                await graph_builder.clear_generated_graph(self.memgraph_client)
            auto_classify = self.entry.options.get(CONF_AUTO_CLASSIFY, DEFAULT_AUTO_CLASSIFY)
            await graph_builder.build_full_graph(
                self.hass, self.memgraph_client, auto_classify=auto_classify
            )
            if overrides_payload is not None:
                await overrides.async_import_overrides(self.memgraph_client, overrides_payload)
            await user_knowledge.async_reconcile_energy_roles(
                self.hass, self.memgraph_client
            )
            self.state.schema_version = await graph_builder.get_schema_version(self.memgraph_client)
            await self._refresh_counts()
        except Exception as err:  # noqa: BLE001
            self._record_failure(err)
            raise
        else:
            self._record_success()
            self.change_buffer.publish_reconcile()

    async def async_rebuild(self) -> None:
        """Clear integration-owned data, then rebuild the full ontology (T038)."""
        await self._run_serialized(self._execute_full_sync, clear_first=True)

    async def async_resync(self) -> None:
        """Re-read HA registries and MERGE in place, without clearing (FR-016)."""
        await self._run_serialized(self._execute_full_sync, clear_first=False)

    # -- Single-entity operations (User Story 5/7) -------------------------

    async def _execute_entity_sync(
        self,
        entity_id: str,
        context: graph_builder.EntitySyncContext | None = None,
    ) -> None:
        try:
            await graph_builder.update_entity(
                self.hass, self.memgraph_client, entity_id, context
            )
            await user_knowledge.async_reconcile_energy_roles(
                self.hass, self.memgraph_client, entity_id
            )
            await self._refresh_counts()
        except Exception as err:  # noqa: BLE001
            self._record_failure(err)
            raise
        else:
            self._record_success()
            # Determine whether this was an upsert or remove based on HA state.
            from homeassistant.helpers import entity_registry as _er
            registry = _er.async_get(self.hass)
            node_id = f"Entity:{entity_id}"
            if registry.entities.get(entity_id) is None and self.hass.states.get(entity_id) is None:
                self.change_buffer.publish_remove([node_id], [])
            else:
                self.change_buffer.publish_upsert([node_id], [], ["state", "ha_id"])

    async def _execute_entity_sync_batch(
        self, entities: dict[str, graph_builder.EntitySyncContext | None]
    ) -> dict[str, Exception | None]:
        """Sync a batch of entities in one pass, refreshing node/relationship
        counts once for the whole batch instead of once per entity (ON-002) -
        and, since ON-007, only when the batch could actually have changed
        those counts. Since ON-008, the energy-role binding repair follows
        the same once-per-batch, gated-on-need shape.

        `_refresh_counts()` runs two full-graph Cypher scans (`MATCH (n)
        RETURN count(n)` / `MATCH ()-[r]->() RETURN count(r)`). A plain
        value/attribute update on an Entity node that already exists cannot
        change either count - `update_entity`'s MERGE calls for the
        Domain/Device relationships are idempotent no-ops once those edges
        exist - so the only way this batch's writes can move the counts is
        the entity-gone branch in `update_entity` (`_delete_node`, the
        "deleted mid-flight" edge case). Rescanning the whole graph on every
        batch regardless was the dominant remaining CPU cost after ON-002's
        debounce/batching: confirmed live (2026-09-21) by disabling the
        integration entirely - Memgraph CPU dropped from a sustained ~23% to
        ~0% and host load from ~0.7 back to the pre-ontology ~0.15-0.20
        baseline, with the fast power/energy sensors never going idle long
        enough for a batch to contain zero pending entities.

        `user_knowledge.async_repair_energy_role_bindings()` is a second,
        similarly unconditional, full-graph-scope operation - it rebuilds
        *every* EnergyRoleAssignment's binding, not just this batch's
        entities. `async_reconcile_energy_roles()` used to call it once per
        entity in this loop (via `async_reconcile_energy_role_for_entity`,
        which only touches the one entity's own assignment, plus the shared
        repair this method now gates separately - ON-008, found while
        investigating why host load stayed above the zero-traffic A/B
        baseline even after ON-007). It only needs to run when this batch
        could plausibly have left a binding stale: an entity gained/kept an
        inferred role (its own binding is already fresh from the upsert
        itself, but another assignment's binding could reference an Entity
        node that got deleted/recreated elsewhere in the same batch) or an
        entity was removed outright (same `removed` set used for the counts
        refresh above).

        Each entity's own graph write is still isolated in its own
        try/except so one bad entity can't abort the rest of the batch -
        failures are reported per-entity via the returned map, mirroring
        `_execute_entity_sync`'s single-entity error handling.
        """
        results: dict[str, Exception | None] = {}
        any_role_assigned = False
        for entity_id, context in entities.items():
            try:
                await graph_builder.update_entity(
                    self.hass, self.memgraph_client, entity_id, context
                )
                if await user_knowledge.async_reconcile_energy_role_for_entity(
                    self.hass, self.memgraph_client, entity_id
                ):
                    any_role_assigned = True
            except Exception as err:  # noqa: BLE001
                results[entity_id] = err
            else:
                results[entity_id] = None

        succeeded = [entity_id for entity_id, err in results.items() if err is None]
        if not succeeded:
            if results:
                self._record_failure(next(iter(results.values())))
            return results

        registry = er.async_get(self.hass)
        removed = {
            entity_id
            for entity_id in succeeded
            if registry.entities.get(entity_id) is None
            and self.hass.states.get(entity_id) is None
        }

        if removed:
            try:
                await self._refresh_counts()
            except Exception as err:  # noqa: BLE001
                # The per-entity writes above still succeeded; only the
                # shared counts refresh failed. Surface it against each
                # entity that otherwise succeeded rather than silently
                # dropping it - retry will just re-run the counts refresh.
                self._record_failure(err)
                for entity_id in succeeded:
                    results[entity_id] = err
                return results

        if any_role_assigned or removed:
            try:
                await user_knowledge.async_repair_energy_role_bindings(
                    self.memgraph_client
                )
            except Exception as err:  # noqa: BLE001
                # Same reasoning as the counts-refresh failure above: the
                # per-entity writes (including each entity's own role
                # upsert/removal) already succeeded, only the shared
                # binding repair failed - surface it so a retry re-runs it.
                self._record_failure(err)
                for entity_id in succeeded:
                    results[entity_id] = err
                return results

        self._record_success()
        for entity_id in succeeded:
            node_id = f"Entity:{entity_id}"
            if entity_id in removed:
                self.change_buffer.publish_remove([node_id], [])
            else:
                self.change_buffer.publish_upsert([node_id], [], ["state", "ha_id"])
        return results

    async def async_sync_entity(self, entity_id: str) -> None:
        """Refresh a single entity node/relationships (FR-016).

        Raises ``ValueError`` if the entity does not exist in HA at all
        (contracts/services.md `ontology.sync_entity`).
        """
        registry = er.async_get(self.hass)
        if registry.entities.get(entity_id) is None and self.hass.states.get(entity_id) is None:
            raise ValueError(f"Entity {entity_id} does not exist")
        await self._run_serialized(self._execute_entity_sync, entity_id)

    # -- Validation (User Story 7/8) ---------------------------------------

    async def _execute_validate(self) -> None:
        try:
            await self.memgraph_client.test_connection()
            current_version = await graph_builder.get_schema_version(self.memgraph_client)
            self.state.schema_version = current_version
            self.state.validation_findings = await validation.async_run_validation(
                self.hass, self.memgraph_client
            )
        except Exception as err:  # noqa: BLE001
            self._record_failure(err)
            raise
        else:
            self._record_success()

    async def async_validate(self) -> None:
        """Run the full 9-category validation engine (FR-014, User Story 5).

        Never raises solely due to a schema-version mismatch: `schema_mismatch`
        is now one ordinary finding among nine, not a fatal condition (this
        supersedes v1's connectivity-only check per contracts/services.md).
        """
        await self._run_serialized(self._execute_validate)

    # -- Semantic classification (User Story 1/6) ---------------------------

    async def async_classify(self) -> int:
        """Run a full-graph classification pass (User Story 1)."""
        return await self.async_run_operation(self._execute_semantic_refresh, None)

    async def _execute_semantic_refresh(self, entity_id: str | None) -> int:
        classified = await semantic_classifier.async_refresh_semantics(
            self.hass, self.memgraph_client, entity_id
        )
        await user_knowledge.async_reconcile_energy_roles(
            self.hass, self.memgraph_client, entity_id
        )
        return classified

    async def async_refresh_semantics(self, entity_id: str | None = None) -> int:
        """Recalculate inferred classifications for one entity or all (User Story 6)."""
        return await self.async_run_operation(self._execute_semantic_refresh, entity_id)

    # -- Read-only query service (User Story 2) ------------------------------

    async def async_query(
        self, cypher: str, parameters: dict[str, Any] | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """Validate and execute a read-only Cypher query (User Story 2)."""
        return await self.async_run_operation(
            query_service.execute_query, self.memgraph_client, cypher, parameters, limit
        )

    # -- User-managed overrides export/import (User Story 7) ----------------

    async def async_export_overrides(self) -> dict[str, Any]:
        """Export every `source = "user"` override relationship (User Story 7)."""
        return await self.async_run_operation(
            overrides.async_export_overrides, self.memgraph_client
        )

    async def async_import_overrides(self, payload: Any) -> int:
        """Validate and import a previously exported overrides payload (User Story 7)."""
        return await self.async_run_operation(
            overrides.async_import_overrides, self.memgraph_client, payload
        )

    # -- Event-driven incremental updates (User Story 5) --------------------

    def _track_failed_update(self, kind: str, target_id: str, err: Exception) -> None:
        """Mark a failed incremental update as failed/pending, never dropped
        (FR-020, T046a)."""
        for item in self.state.failed_updates:
            if item["kind"] == kind and item["id"] == target_id:
                item["attempts"] += 1
                item["error"] = redact_exception(err)
                self._notify_state_changed()
                return
        self.state.failed_updates.append(
            {"kind": kind, "id": target_id, "attempts": 1, "error": redact_exception(err)}
        )
        self._notify_state_changed()

    def _clear_failed_update(self, kind: str, target_id: str) -> None:
        self.state.failed_updates = [
            item
            for item in self.state.failed_updates
            if not (item["kind"] == kind and item["id"] == target_id)
        ]
        self._notify_state_changed()

    async def async_handle_entity_change(
        self,
        entity_id: str,
        context: graph_builder.EntitySyncContext | None = None,
    ) -> None:
        """Entry point for debounced `state_changed`/entity-registry events."""
        try:
            await self._run_incremental(self._execute_entity_sync, entity_id, context)
        except Exception as err:  # noqa: BLE001 - tracked, never dropped (FR-020)
            self._track_failed_update("entity", entity_id, err)
        else:
            self._clear_failed_update("entity", entity_id)

    async def async_handle_entity_changes_batch(
        self, entities: dict[str, graph_builder.EntitySyncContext | None]
    ) -> None:
        """Entry point for a batched group of debounced `state_changed`
        events (ON-002; see `event_listener.StateChangeDebouncer`).

        Syncs every entity in the batch and refreshes node/relationship
        counts once for the whole batch rather than once per entity. Takes
        the lock directly (mirroring `_run_incremental`'s FIFO wait-don't-
        reject semantics) rather than going through `_run_incremental`
        itself, because it needs the per-entity result map back and
        `_run_incremental`'s generic `func(*args, **kwargs)` signature
        doesn't return one.
        """
        if not entities:
            return
        start = time.monotonic()
        async with self._lock:
            results = await self._execute_entity_sync_batch(entities)
        # ON-004: cheap - one existing time.monotonic() call plus two int
        # increments (record_sync_batch), no extra I/O or HA state write.
        self.record_sync_batch((time.monotonic() - start) * 1000, len(entities))
        for entity_id, err in results.items():
            if err is not None:
                self._track_failed_update("entity", entity_id, err)
            else:
                self._clear_failed_update("entity", entity_id)

    async def async_handle_device_change(self, device_id: str) -> None:
        """Entry point for device-registry update/remove events."""
        try:
            await self._run_incremental(
                graph_builder.update_device, self.hass, self.memgraph_client, device_id
            )
        except Exception as err:  # noqa: BLE001
            self._track_failed_update("device", device_id, err)
        else:
            self._clear_failed_update("device", device_id)

    async def async_handle_area_change(self, area_id: str) -> None:
        """Entry point for area-registry update/remove events."""
        try:
            await self._run_incremental(
                graph_builder.update_area, self.hass, self.memgraph_client, area_id
            )
        except Exception as err:  # noqa: BLE001
            self._track_failed_update("area", area_id, err)
        else:
            self._clear_failed_update("area", area_id)

    async def async_retry_failed_updates(self) -> None:
        """Retry every tracked failed/pending update (FR-020, T046a)."""
        for item in list(self.state.failed_updates):
            if item["kind"] == "entity":
                await self.async_handle_entity_change(item["id"])
            elif item["kind"] == "device":
                await self.async_handle_device_change(item["id"])
            elif item["kind"] == "area":
                await self.async_handle_area_change(item["id"])
