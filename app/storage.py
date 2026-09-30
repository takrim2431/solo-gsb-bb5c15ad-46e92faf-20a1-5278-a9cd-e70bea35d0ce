"""In-memory storage and deterministic timeline engine.

Rules (contract implemented in this module)
-------------------------------------------

* Events form a per-(batch, sensor) causal DAG. Each revision has exactly one
  parent; ``"ROOT"`` denotes a causal root.
* Events may arrive in any order, overlap in time, and be re-sent. A revision
  whose parent has not been seen is **blocked**: it (and everything descending
  from it) is excluded from the effective timeline, while every missing parent
  is reported as a backfillable gap.
* Withdrawn revisions are tombstones: applied in the same causal order they
  erase every calibrated value under their interval.
* Application order for timeline folding is strictly deterministic:
  causal depth ascending, then ``revision_id`` ascending lexicographically
  (the later/larger id wins on same-depth overlaps).
  Later applications overwrite earlier ones on overlapping time spans.
* Half-open ``[start_ns, end_ns)`` intervals guarantee abutting fragments
  share no sample instant.
* Idempotency: an event with a known idempotency key is either an exact
  duplicate (accepted, no state change) or a conflicting replay (rejected).
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .models import ROOT_PARENT


class IdempotencyConflict(Exception):
    """An idempotency key was reused with a different payload."""


@dataclass(frozen=True)
class StoredEvent:
    batch_id: str
    sensor_id: str
    revision_id: str
    parent_id: str
    idempotency_key: str
    start_ns: int
    end_ns: int
    value: Optional[float]
    unit: Optional[str]
    withdrawn: bool


@dataclass
class BatchStore:
    events: dict[str, StoredEvent] = field(default_factory=dict)
    # revision_id -> owning sensor, for stable parent lookups
    revision_sensor: dict[str, str] = field(default_factory=dict)
    # idempotency_key -> canonical payload string
    idempotency: dict[str, str] = field(default_factory=dict)


class EventStore:
    """Thread-safe, process-local, resettable event store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._batches: dict[str, BatchStore] = defaultdict(BatchStore)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # -- mutation -----------------------------------------------------------

    def add(self, event: StoredEvent) -> str:
        """Insert an event.

        Returns ``"accepted"`` or ``"duplicate"``. Raises
        :class:`IdempotencyConflict` on a key reused with other content.
        """
        with self._lock:
            return apply_event(self._batches[event.batch_id], event)

    def reset(self) -> tuple[int, int]:
        with self._lock:
            batches_removed = len(self._batches)
            events_removed = sum(len(b.events) for b in self._batches.values())
            self._batches.clear()
            return batches_removed, events_removed

    # -- reads --------------------------------------------------------------

    def has_batch(self, batch_id: str) -> bool:
        with self._lock:
            return batch_id in self._batches and bool(self._batches[batch_id].events)

    def get_batch(self, batch_id: str) -> Optional[BatchStore]:
        with self._lock:
            return self._batches.get(batch_id)

    def snapshot_batch(self, batch_id: str) -> BatchStore:
        """Detached copy of one batch (an empty store when it does not exist).

        The copy shares the immutable :class:`StoredEvent` values but owns
        fresh mappings, so a preview may "apply" its candidate event without
        touching the live store. Call while holding :attr:`lock`.
        """
        batch = self._batches.get(batch_id)
        if batch is None:
            return BatchStore()
        return projected_batch(batch)

    def stats(self) -> tuple[int, int]:
        with self._lock:
            return len(self._batches), sum(
                len(b.events) for b in self._batches.values()
            )


# ---------------------------------------------------------------------------
# Deterministic serialization helpers
# ---------------------------------------------------------------------------


def apply_event(batch: BatchStore, event: StoredEvent) -> str:
    """Apply the ingest/idempotency rules of ``EventStore.add`` to one batch.

    Returns ``"accepted"`` or ``"duplicate"`` and raises
    :class:`IdempotencyConflict` on a key (or same revision id) reused with
    different content. Lock-free so both the live ingest path and the
    write-free preview projection share exactly the same rules.
    """
    canonical = canonical_event(event)

    previous = batch.idempotency.get(event.idempotency_key)
    if previous is not None:
        if previous == canonical:
            return "duplicate"
        raise IdempotencyConflict(event.idempotency_key)

    existing = batch.events.get(event.revision_id)
    if existing is not None:
        # Same revision_id submitted under a new idempotency key.
        if canonical_event(existing) != canonical:
            raise IdempotencyConflict(event.idempotency_key)
        # Identical content but different key: still an exact replay;
        # register the new key so both forms deduplicate going forward.
        batch.idempotency[event.idempotency_key] = canonical
        return "duplicate"

    batch.events[event.revision_id] = event
    batch.revision_sensor[event.revision_id] = event.sensor_id
    batch.idempotency[event.idempotency_key] = canonical
    return "accepted"


def projected_batch(batch: BatchStore) -> BatchStore:
    """Shallow detached copy of one batch, for preview projections.

    Mapping contents are copied (inserts during a preview never reach the
    live store); the contained :class:`StoredEvent` values are immutable and
    shared safely.
    """
    return BatchStore(
        events=dict(batch.events),
        revision_sensor=dict(batch.revision_sensor),
        idempotency=dict(batch.idempotency),
    )


def canonical_event(event: StoredEvent) -> str:
    """Canonical representation used for idempotent payload comparison."""
    value = "null" if event.value is None else repr(float(event.value))
    unit = "null" if event.unit is None else event.unit
    return "|".join(
        [
            event.batch_id,
            event.sensor_id,
            event.revision_id,
            event.parent_id,
            str(event.start_ns),
            str(event.end_ns),
            value,
            unit,
            "1" if event.withdrawn else "0",
        ]
    )
