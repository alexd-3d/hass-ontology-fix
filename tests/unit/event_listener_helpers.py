"""Shared helpers for the state-change listener unit tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def make_coordinator() -> MagicMock:
    """A coordinator stand-in shaped like what StateChangeDebouncer uses.

    ``entry.options`` must be a real mapping (the debouncer reads its debounce
    and exclusion settings from it), ``record_sync_event`` is synchronous, and
    the two entry points are awaited.
    """
    coordinator = MagicMock()
    coordinator.entry.options = {}
    coordinator.async_handle_entity_change = AsyncMock()
    coordinator.async_handle_entity_changes_batch = AsyncMock()
    return coordinator
