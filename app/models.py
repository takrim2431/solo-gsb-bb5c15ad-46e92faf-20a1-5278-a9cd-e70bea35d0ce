"""Pydantic schemas for the calibration timeline service.

Time coordinates are nanoseconds since an arbitrary batch-local epoch.
Intervals are half-open ``[start_ns, end_ns)`` so that abutting fragments do
not share any sample instant.
"""

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A revision root uses this literal as its (missing) parent.
ROOT_PARENT = "ROOT"

VersionId = Annotated[str, Field(min_length=1, max_length=200)]
BatchId = Annotated[str, Field(min_length=1, max_length=200)]
SensorId = Annotated[str, Field(min_length=1, max_length=200)]
IdemKey = Annotated[str, Field(min_length=1, max_length=200)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MeasurementEvent(StrictModel):
    """One measurement-fragment revision delivered to the ingest endpoint."""

    batch_id: BatchId
    sensor_id: SensorId
    revision_id: VersionId
    # Parent revision for this sensor; the literal "ROOT" marks a causal root.
    parent_id: VersionId
    # Idempotency key; the same event payload is accepted exactly once in
    # effect and every repeated submission is acknowledged as a duplicate.
    idempotency_key: IdemKey
    start_ns: int = Field(ge=0)
    end_ns: int = Field(ge=1)
    # Calibration value for the fragment; required unless the fragment is
    # withdrawn (withdrawn fragments erase instead of writing a value).
    value: Optional[float] = None
    unit: Optional[str] = Field(default=None, max_length=40)
    withdrawn: bool = False

    @model_validator(mode="after")
    def _check_interval_and_value(self) -> "MeasurementEvent":
        if self.revision_id == ROOT_PARENT:
            raise ValueError(f"revision_id may not be the reserved value {ROOT_PARENT!r}")
        if self.end_ns <= self.start_ns:
            raise ValueError("end_ns must be strictly greater than start_ns")
        if not self.withdrawn and self.value is None:
            raise ValueError("value is required when withdrawn is false")
        return self


class TimelineSegment(StrictModel):
    start_ns: int
    end_ns: int
    state: Literal["calibrated", "withdrawn"]
    # Populated for calibrated segments; null on withdrawn (erased) spans.
    value: Optional[float] = None
    unit: Optional[str] = None
    revision_id: str


class Gap(StrictModel):
    """A missing revision that blocks one or more known children."""

    batch_id: str
    sensor_id: str
    missing_revision_id: str
    awaited_by: list[str]
    # Revision that referenced the missing parent (may repeat across gaps).
    referenced_from: str


class SensorTimeline(StrictModel):
    sensor_id: str
    grounded: bool
    segments: list[TimelineSegment]
    segments_hash: str
    covered_start_ns: Optional[int] = None
    covered_end_ns: Optional[int] = None


class BatchResult(StrictModel):
    batch_id: str
    sensors: list[SensorTimeline]
    gaps: list[Gap]
    revisions_total: int
    revisions_grounded: int
    revisions_blocked: int
    revisions_invalid: int
    # True when every known revision is grounded (no gaps, no cycles).
    complete: bool
    timeline_hash: str


class SamplePointResponse(StrictModel):
    """Effective calibration state at one queried nanosecond of one sensor."""

    batch_id: str
    sensor_id: str
    timestamp_ns: int
    # calibrated: a grounded, value-bearing segment wins the fold at the point;
    # withdrawn:   a tombstone segment wins (value/unit are null);
    # uncovered:   no grounded segment covers the instant;
    # blocked:     a revision covering the instant is waiting on missing
    #              ancestors, so the eventual winner cannot yet be determined.
    state: Literal["calibrated", "withdrawn", "uncovered", "blocked"]
    # Populated only for calibrated points; null otherwise.
    value: Optional[float] = None
    unit: Optional[str] = None
    # Winning revision (last applied in deterministic fold order); null unless
    # the state is calibrated/withdrawn.
    revision_id: Optional[str] = None
    # Ordered parent chain from revision_id back to the causal root, e.g.
    # ["v3", "v2", "v1", "ROOT"]; empty unless the point is resolved.
    parent_chain: list[str] = Field(default_factory=list)
    # Distinct not-yet-uploaded revisions that block every covering revision;
    # populated only for blocked points (sorted, ready for backfill).
    missing_ancestors: list[str] = Field(default_factory=list)


class CompareSide(StrictModel):
    """Effective folded state one side contributes to a compare interval."""

    state: Literal["calibrated", "withdrawn"]
    # Populated for calibrated spans; null on withdrawn (erased) spans.
    value: Optional[float] = None
    unit: Optional[str] = None


class CompareInterval(StrictModel):
    """One half-open diff interval over the union of both folded timelines."""

    start_ns: int
    end_ns: int
    # equal:          both sides agree (both uncovered counts as agreement);
    # left_only:      only the left batch covers the span;
    # right_only:     only the right batch covers the span;
    # value_mismatch: both calibrated but value or unit differ;
    # state_mismatch: one side calibrated, the other withdrawn.
    kind: Literal["equal", "left_only", "right_only", "value_mismatch", "state_mismatch"]
    # Effective payload of each side on this span; null where uncovered.
    left: Optional[CompareSide] = None
    right: Optional[CompareSide] = None


class IncompleteReason(StrictModel):
    """Why one side of a comparison cannot be considered final yet."""

    side: Literal["left", "right"]
    batch_id: str
    # gaps:              revisions blocked by not-yet-uploaded ancestors;
    # invalid_revisions: revisions in or below a parent cycle.
    reason: Literal["gaps", "invalid_revisions"]
    detail: str
    # Blocked or invalid revisions excluded from the fold (sorted).
    affected_revisions: list[str] = Field(default_factory=list)
    # For reason="gaps": distinct missing parents to backfill (sorted).
    missing_revision_ids: list[str] = Field(default_factory=list)


class CompareResponse(StrictModel):
    """Half-open interval diff of one sensor across two batches."""

    sensor_id: str
    left_batch_id: str
    right_batch_id: str
    # Hashes of each side's folded segments (matching the per-sensor
    # segments_hash of the batch result) for cross-checking.
    left_segments_hash: str
    right_segments_hash: str
    # True when either side has gaps or invalid revisions, so backfilling
    # may still change the diff.
    incomplete: bool
    incomplete_reasons: list[IncompleteReason] = Field(default_factory=list)
    # Contiguous, time-sorted partition of the union coverage span;
    # adjacent intervals merge when kind and both side payloads match.
    intervals: list[CompareInterval] = Field(default_factory=list)


class QualityGateRequest(StrictModel):
    """Parameters for one quality-gate evaluation of a folded sensor timeline."""

    sensor_id: SensorId
    # Inclusive allowed-value bounds: a calibrated value v passes iff
    # min_value <= v <= max_value. NaN/inf bounds are rejected.
    min_value: float = Field(
        allow_inf_nan=False,
        description="Inclusive lower bound of the allowed calibrated value.",
    )
    max_value: float = Field(
        allow_inf_nan=False,
        description="Inclusive upper bound of the allowed calibrated value.",
    )
    # A contiguous out-of-bounds run is reported as a violation only when
    # its duration reaches this many nanoseconds.
    min_duration_ns: int = Field(
        ge=1,
        description="Minimum duration (ns) an out-of-bounds run must reach to "
        "be reported as a violation.",
    )
    # How missing-data gaps (withdrawn/uncovered/blocked) affect the verdict:
    # "ignore" only records them; "fail" fails the gate when any gap exists.
    missing_policy: Literal["ignore", "fail"] = Field(
        description="ignore: record gaps without failing; fail: any gap fails the gate.",
    )

    @model_validator(mode="after")
    def _check_bounds(self) -> "QualityGateRequest":
        if self.min_value > self.max_value:
            raise ValueError("min_value must be less than or equal to max_value")
        return self


class QualityGateViolation(StrictModel):
    """One merged out-of-bounds run whose duration reached the threshold."""

    start_ns: int
    end_ns: int
    duration_ns: int
    # Extreme calibrated values observed inside the run.
    min_value: float
    max_value: float


class QualityGateGap(StrictModel):
    """One merged missing-data span; gaps never merge into violation runs."""

    start_ns: int
    end_ns: int
    duration_ns: int
    state: Literal["withdrawn", "uncovered", "blocked"]
    # Distinct not-yet-uploaded terminal ancestors; populated for blocked
    # spans (sorted, ready for backfill), empty otherwise.
    missing_ancestors: list[str] = Field(default_factory=list)


class QualityGateResponse(StrictModel):
    """Deterministic quality-gate verdict for one sensor of one batch."""

    batch_id: str
    sensor_id: str
    status: Literal["pass", "fail"]
    # Half-open scan window: the union span of grounded and blocked
    # revisions; both null when nothing is scannable (e.g. all revisions
    # are invalid cycle members).
    scanned_start_ns: Optional[int] = None
    scanned_end_ns: Optional[int] = None
    violations: list[QualityGateViolation] = Field(default_factory=list)
    gaps: list[QualityGateGap] = Field(default_factory=list)
    # SHA-256 over the evaluated sensor timeline (folded segments plus
    # blocked spans); changes iff the gate input changes.
    timeline_hash: str


class IngestResponse(StrictModel):
    status: Literal["accepted", "duplicate", "conflict"]
    idempotency_key: str
    batch_id: str
    sensor_id: str
    revision_id: str
    result: BatchResult


class PreviewChangeInterval(StrictModel):
    """One half-open span whose effective state/value/unit changes if submitted.

    Revision provenance is ignored: a span whose only difference is the
    winning ``revision_id`` is not reported.
    """

    start_ns: int
    end_ns: int
    # added:   uncovered before, covered after;
    # removed: covered before, uncovered after;
    # changed: covered on both sides but state/value/unit differ.
    kind: Literal["added", "removed", "changed"]
    # Effective payload on the span before/after the hypothetical submission;
    # null where that side is uncovered.
    before: Optional[CompareSide] = None
    after: Optional[CompareSide] = None


class PreviewResponse(StrictModel):
    """Non-mutating impact preview of one measurement-fragment revision."""

    # submittable: the event would be newly accepted;
    # duplicate:   the exact event (or its revision content) is already stored.
    status: Literal["submittable", "duplicate"]
    idempotency_key: str
    batch_id: str
    sensor_id: str
    revision_id: str
    # Batch timeline hash before and after the hypothetical submission.
    before_timeline_hash: str
    after_timeline_hash: str
    # Full deterministic batch results for both snapshots, for convenience.
    before: BatchResult
    after: BatchResult
    # Half-open intervals of this sensor whose effective state/value/unit
    # changes (provenance-only changes are excluded); empty for duplicates.
    changed_intervals: list[PreviewChangeInterval] = Field(default_factory=list)
    # Missing-parent gaps that would appear / disappear for this sensor.
    gaps_added: list[Gap] = Field(default_factory=list)
    gaps_removed: list[Gap] = Field(default_factory=list)


class ResetResponse(StrictModel):
    status: Literal["reset"]
    batches_removed: int
    events_removed: int


class HealthResponse(StrictModel):
    status: Literal["ok"]
    storage: Literal["memory"]
    version: str
