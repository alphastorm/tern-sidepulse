"""The local coordinator: observer generations, leases, outcome notices and per-pane rows.

One process per user holds the singleton lock and serializes every state change. Complete
window snapshots contribute rows keyed by host and pane; the transport owns delivery, including
a crashed predecessor's rows. Labels stay in memory and leave it only in SidePulse reports (unless
config.json sets ``"labels": false``), never in logs, acknowledgements or status.
"""
from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from . import PROTOCOL_SCHEMA, VERSION
from .clock import Clock
from .jsonio import load_json
from .limits import Limits
from .reducer import (
    KNOWN_STATES,
    MODES,
    NOTICE_FAILED,
    NOTICE_KINDS,
    STATE_IDLE,
    Observation,
    Row,
    RowKey,
    notice_clears,
    pane_mode,
    reduce_rows,
    sanitize_label,
)
from .transport import SidePulseTransport, iso_utc

OPS = ("register", "snapshot", "status", "enable", "disable", "resync")
COVERAGE_COMPLETE = "complete"
COVERAGE_UNKNOWN = "unknown"
MAX_WINDOW_KEY = 64
MAX_PANE_ID = 64
MAX_NOTICE_ID = 64
DEFAULT_HOST = "local"
MAX_HOST_KEY = 128
MAX_LABEL_INPUT = 1024
MAX_RETIRED_NOTICES = 1024
MAX_RETIRE_REASONS = 256
DIAG_KEYS = (
    "omp",
    "non_omp",
    "unclassified",
    "host_unavailable",
    "unmapped",
    "exited",
    "unknown_state",
    "program_missing",
    "wrapped",
    "too_many",
    "budget_pauses",
    "helper_failures",
    "label_tab",
    "label_title",
    "label_none",
    "labels_unavailable",
    "labels_dropped",
    "host_fallback",
    "encode_failed",
)
DIAG_MAX = 100_000


class ProtocolError(Exception):
    """A structured rejection; ``detail`` is always a fixed message, never a client value."""

    def __init__(self, error: str, detail: str | None = None, **extra) -> None:
        super().__init__(error)
        self.error = error
        self.detail = detail
        self.extra = extra

    def to_dict(self) -> dict:
        body = {"ok": False, "error": self.error}
        if self.detail:
            body["detail"] = self.detail
        body.update(self.extra)
        return body


@dataclass
class NoticeTrack:
    notice_id: str
    kind: str  # fixed at first acceptance; sets the duration
    started: float  # monotonic, set once
    voided: bool = False


@dataclass
class ObserverState:
    key: str
    generation: int
    seq: int
    expires_at: float
    observations: dict[RowKey, Observation] = field(default_factory=dict)
    notices: dict[RowKey, NoticeTrack] = field(default_factory=dict)  # one track per row key
    retired_notices: "OrderedDict[tuple[RowKey, str], None]" = field(default_factory=OrderedDict)
    applied: int = 0
    coverage: str = COVERAGE_COMPLETE
    uncertain: bool = False
    last_ack: dict = field(default_factory=dict)
    snapshots: int = 0
    diag: dict = field(default_factory=dict)


def observer_key(observer: object) -> str:
    if not isinstance(observer, dict):
        raise ProtocolError("bad_request", "observer must be an object")
    parent_pid = observer.get("parent_pid")
    window_key = observer.get("window_key", "")
    if isinstance(parent_pid, bool) or not isinstance(parent_pid, int) or parent_pid <= 0:
        raise ProtocolError("bad_request", "observer.parent_pid must be a positive integer")
    if not isinstance(window_key, str) or len(window_key) > MAX_WINDOW_KEY or not window_key.isprintable():
        raise ProtocolError("bad_request", "observer.window_key must be a short printable string")
    return f"{parent_pid}:{window_key}"


def parse_observations(raw: object, limit: int) -> tuple[dict[RowKey, Observation], int]:
    if not isinstance(raw, list):
        raise ProtocolError("bad_request", "observations must be an array")
    if len(raw) > limit:
        raise ProtocolError("too_many_observations", "observation limit exceeded", limit=limit)
    result: dict[RowKey, Observation] = {}
    labels_rejected = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ProtocolError("bad_request", "observation must be an object")
        pane = item.get("pane")
        if isinstance(pane, bool) or not isinstance(pane, (int, str)):
            raise ProtocolError("bad_request", "observation.pane must be a number or string")
        pane_id = str(pane)
        if not pane_id or len(pane_id) > MAX_PANE_ID:
            raise ProtocolError("bad_request", "observation.pane is too long")
        state = item.get("state")
        if not isinstance(state, str) or state not in KNOWN_STATES:
            raise ProtocolError("bad_request", "observation.state is not a Tern agent state")
        notice = item.get("notice")
        notice_id = notice_kind = None
        if notice is not None:
            if not isinstance(notice, dict):
                raise ProtocolError("bad_request", "observation.notice must be an object")
            raw_id = notice.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
                raise ProtocolError("bad_request", "notice.id must be a number or string")
            notice_id = str(raw_id)
            if not notice_id or len(notice_id) > MAX_NOTICE_ID:
                raise ProtocolError("bad_request", "notice.id is too long")
            notice_kind = notice.get("kind")
            if not isinstance(notice_kind, str) or notice_kind not in NOTICE_KINDS:
                raise ProtocolError("bad_request", "notice.kind must be done or failed")
        host = item.get("host")
        if host is None:
            host = DEFAULT_HOST
        elif (
            not isinstance(host, str)
            or not 1 <= len(host) <= MAX_HOST_KEY
            or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in host)
        ):
            raise ProtocolError("bad_request", "observation.host must be a short host key")
        raw_label = item.get("label")
        label = None
        if raw_label is not None:
            if isinstance(raw_label, str) and len(raw_label) <= MAX_LABEL_INPUT:
                label = sanitize_label(raw_label)
            else:
                labels_rejected += 1
        # Duplicate row keys within one snapshot keep the first observation and its label.
        obs = Observation(pane_id, state, notice_id, notice_kind, host, label)
        result.setdefault(obs.key, obs)
    return result, labels_rejected


def parse_diag(raw: object) -> dict:
    """Allow-listed integer counters only; anything else is dropped, never echoed."""
    if not isinstance(raw, dict):
        return {}
    diag = {}
    for key in DIAG_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        diag[key] = max(0, min(DIAG_MAX, int(value)))
    return diag


class Coordinator:
    def __init__(
        self,
        *,
        clock: Clock,
        transport: SidePulseTransport,
        limits: Limits,
        disabled: bool,
        persist_disabled: Callable[[bool], None],
        log: Callable[[str], None],
    ) -> None:
        self.clock = clock
        self.transport = transport
        self.limits = limits
        self.disabled = disabled
        self.persist_disabled = persist_disabled
        self.log = log
        self.epoch = os.urandom(8).hex()
        self.started_at = clock.monotonic()
        self.started_wall = clock.wall()
        self.generation_counter = 0
        self.tokens: dict[int, tuple[str, float]] = {}
        self.observers: dict[str, ObserverState] = {}
        self.retire_reason: "OrderedDict[int, str]" = OrderedDict()
        self.highest_activated: dict[str, int] = {}
        self.rows: tuple[Row, ...] = ()
        self.apply_counter = 0
        self.empty_since: float | None = clock.monotonic()
        self.last_observation_at: float | None = None
        self.last_wall = clock.wall()
        self.last_mono = clock.monotonic()
        self.discontinuities = 0
        self.counters = {
            "requests": 0,
            "rejected": 0,
            "snapshots": 0,
            "expired_observers": 0,
            "notices_expired": 0,
            "internal_errors": 0,
            "labels_rejected": 0,
        }
        self.stop_requested = False
        self.stop_reason: str | None = None

    # ---- generations ----------------------------------------------------------------------

    def is_retired(self, generation: int) -> bool:
        """Issued, but neither outstanding nor live: a tombstone derived rather than stored."""
        if generation < 1 or generation > self.generation_counter or generation in self.tokens:
            return False
        return all(observer.generation != generation for observer in self.observers.values())

    def _note_retired(self, generation: int, reason: str) -> None:
        self.retire_reason[generation] = reason
        self.retire_reason.move_to_end(generation)
        while len(self.retire_reason) > MAX_RETIRE_REASONS:
            self.retire_reason.popitem(last=False)

    def _prune_highest(self, key: str) -> None:
        if key in self.observers:
            return
        if any(owner == key for owner, _ in self.tokens.values()):
            return
        self.highest_activated.pop(key, None)

    # ---- requests -------------------------------------------------------------------------

    def handle(self, request: object) -> dict:
        self.counters["requests"] += 1
        op = request.get("op") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict):
                raise ProtocolError("bad_request", "request must be a JSON object")
            if request.get("schema") != PROTOCOL_SCHEMA:
                raise ProtocolError("unsupported_schema", "schema must be 1", schema=PROTOCOL_SCHEMA)
            if op not in OPS:
                raise ProtocolError("unknown_op", "op must be one of " + ", ".join(OPS))
            self.observe_clock()
            response = getattr(self, f"op_{op}")(request)
            response.setdefault("ok", True)
            response["epoch"] = self.epoch
            return response
        except ProtocolError as exc:
            self.counters["rejected"] += 1
            self.recompute(self.clock.monotonic())  # an expiry noticed on the way to the rejection takes effect now
            body = exc.to_dict()
            body["epoch"] = self.epoch
            return body
        except Exception as exc:  # noqa: BLE001 - one bad request must never take the coordinator down
            self.counters["internal_errors"] += 1
            self.log(f"internal error in op {op}: {type(exc).__name__} at {crash_site(exc)}")
            return {"ok": False, "error": "internal", "epoch": self.epoch}

    def op_register(self, request: dict) -> dict:
        if self.disabled:
            raise ProtocolError("disabled", "tern-sidepulse is disabled; run enable first")
        client_version = request.get("client_version")
        if isinstance(client_version, str) and client_version != VERSION:
            # The plugin was updated under a running coordinator: step aside so the new code takes over.
            self.log(f"client version {client_version[:32]!r} differs from {VERSION}; stopping")
            self.request_stop("version_mismatch")
            raise ProtocolError("version_mismatch", "coordinator is restarting with the new version", version=VERSION)
        key = observer_key(request.get("observer"))
        if len(self.observers) >= self.limits.max_observers and key not in self.observers:
            raise ProtocolError("too_many_observers", "observer limit exceeded", limit=self.limits.max_observers)
        now = self.clock.monotonic()
        while len(self.tokens) >= self.limits.max_tokens:
            oldest = min(self.tokens, key=lambda token: self.tokens[token][1])
            owner, _ = self.tokens.pop(oldest)
            self._note_retired(oldest, "evicted")
            self._prune_highest(owner)
        self.generation_counter += 1
        token = self.generation_counter
        self.tokens[token] = (key, now)
        if not self.observers:
            self.empty_since = now  # a registering window delays the drain
        return {
            "generation": token,
            "observer_key": key,
            "lease_seconds": self.limits.lease,
            "notice_seconds": {"done": self.limits.done_notice, "failed": self.limits.failed_notice},
            "coordinator_pid": os.getpid(),
            "version": VERSION,
        }

    def op_snapshot(self, request: dict) -> dict:
        if self.disabled:
            raise ProtocolError("disabled", "tern-sidepulse is disabled; run enable first")
        if request.get("epoch") != self.epoch:
            raise ProtocolError("epoch_mismatch", "the coordinator restarted; register again")
        key = observer_key(request.get("observer"))
        generation = request.get("generation")
        seq = request.get("seq")
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ProtocolError("bad_request", "generation must be an integer")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise ProtocolError("bad_request", "seq must be a positive integer")
        coverage = request.get("coverage")
        if coverage not in (COVERAGE_COMPLETE, COVERAGE_UNKNOWN):
            raise ProtocolError("bad_request", "coverage must be complete or unknown")
        observations, labels_rejected = parse_observations(request.get("observations", []), self.limits.max_observations)
        self.counters["labels_rejected"] += labels_rejected
        diag = parse_diag(request.get("diag"))

        now = self.clock.monotonic()
        self.expire(now)
        observer = self.observers.get(key)
        activated = False
        if observer is not None and observer.generation == generation:
            if seq == observer.seq:
                duplicate = dict(observer.last_ack)
                duplicate["duplicate"] = True
                duplicate["renewed"] = False
                return duplicate
            if seq < observer.seq:
                raise ProtocolError("seq_not_increasing", "seq is not above the last accepted seq")
        elif generation in self.tokens:
            owner, _issued = self.tokens[generation]
            if owner != key:
                raise ProtocolError("unknown_token", "that generation belongs to another observer")
            highest = self.highest_activated.get(key)
            if highest is not None and generation < highest:
                del self.tokens[generation]
                self._note_retired(generation, "stale")
                raise ProtocolError("stale_generation", "generation is below the live generation; register again")
            if observer is None and len(self.observers) >= self.limits.max_observers:
                raise ProtocolError("too_many_observers", "observer limit exceeded", limit=self.limits.max_observers)
            if observer is not None:
                self._note_retired(observer.generation, "replaced")
                self.log(f"observer {key}: generation {observer.generation} replaced by {generation}")
            del self.tokens[generation]
            self.highest_activated[key] = generation
            observer = ObserverState(key=key, generation=generation, seq=seq, expires_at=now + self.limits.lease)
            self.observers[key] = observer
            activated = True
        elif self.is_retired(generation):
            reason = self.retire_reason.get(generation, "retired")
            raise ProtocolError("expired_generation", f"generation was {reason}; register again")
        else:
            raise ProtocolError("unknown_token", "generation was never issued by this coordinator")

        observer.seq = seq
        observer.snapshots += 1
        if diag:
            observer.diag = diag
        renewed = False
        if coverage == COVERAGE_COMPLETE:
            self.apply_counter += 1
            observer.applied = self.apply_counter
            self.apply_observations(observer, observations, now)
            observer.expires_at = now + self.limits.lease
            observer.uncertain = False
            observer.coverage = COVERAGE_COMPLETE
            renewed = True
            self.last_observation_at = now
            self.counters["snapshots"] += 1
        else:
            observer.coverage = COVERAGE_UNKNOWN
        self.recompute(now)
        ack = {
            "accepted": True,
            "activated": activated,
            "renewed": renewed,
            "lease_remaining": round(max(0.0, observer.expires_at - now), 3),
            "rows": self.row_counts(),
            "delivery": self.transport.delivery_summary(),
        }
        observer.last_ack = ack
        return dict(ack)

    def op_status(self, _request: dict) -> dict:
        return {"status": self.status()}

    def op_disable(self, _request: dict) -> dict:
        self.disabled = True
        for observer in list(self.observers.values()):
            self._note_retired(observer.generation, "disabled")
        self.observers.clear()
        for token in list(self.tokens):
            self._note_retired(token, "disabled")
        self.tokens.clear()
        self.highest_activated.clear()
        persisted = True
        try:
            self.persist_disabled(True)
        except OSError as exc:
            persisted = False
            self.log(f"could not persist the disabled latch: {type(exc).__name__}")
        self.recompute(self.clock.monotonic())
        self.log("disabled: observers revoked, neutral requested")
        return {
            "disabled": True,
            "persisted": persisted,
            "rows": self.row_counts(),
            "summary": self.rows_summary(),
            "delivery": self.transport.delivery_summary(),
        }

    def op_enable(self, _request: dict) -> dict:
        self.disabled = False
        persisted = True
        try:
            self.persist_disabled(False)
        except OSError as exc:
            persisted = False
            self.log(f"could not clear the disabled latch: {type(exc).__name__}")
        self.log("enabled: fresh registrations accepted")
        return {"disabled": False, "persisted": persisted}

    def op_resync(self, _request: dict) -> dict:
        self.recompute(self.clock.monotonic())
        self.transport.publish(self.rows, force=True)
        self.transport.tick()
        return {"rows": self.row_counts(), "summary": self.rows_summary(), "delivery": self.transport.delivery_summary()}

    # ---- state ----------------------------------------------------------------------------

    def apply_observations(self, observer: ObserverState, observations: dict[RowKey, Observation], now: float) -> None:
        """A complete snapshot replaces the observer's observations; a notice's deadline is fixed when first seen."""
        for key, obs in observations.items():
            track = observer.notices.get(key)
            if notice_clears(obs.state):
                # The pane resumed: whatever notice it had, or arrives with, can never show later.
                if track is not None:
                    track.voided = True
                if obs.notice_id is not None and (track is None or track.notice_id != obs.notice_id):
                    if track is not None:
                        self._retire_notice(observer, key, track.notice_id)
                    observer.notices[key] = NoticeTrack(obs.notice_id, obs.notice_kind or NOTICE_FAILED, now, voided=True)
                continue
            if obs.notice_id is None:
                continue  # an observation without a notice leaves any track inert; re-inclusion cannot restart it
            if track is None or track.notice_id != obs.notice_id:
                if track is not None:
                    self._retire_notice(observer, key, track.notice_id)
                track = NoticeTrack(obs.notice_id, obs.notice_kind or NOTICE_FAILED, now)
                if (key, obs.notice_id) in observer.retired_notices:
                    track.voided = True
                observer.notices[key] = track
        for key in [key for key in observer.notices if key not in observations]:
            self._retire_notice(observer, key, observer.notices[key].notice_id)
            del observer.notices[key]
        observer.observations = observations

    def _retire_notice(self, observer: ObserverState, key: RowKey, notice_id: str) -> None:
        observer.retired_notices[(key, notice_id)] = None
        observer.retired_notices.move_to_end((key, notice_id))
        while len(observer.retired_notices) > MAX_RETIRED_NOTICES:
            observer.retired_notices.popitem(last=False)

    def _duration(self, kind: str) -> float:
        return self.limits.failed_notice if kind == NOTICE_FAILED else self.limits.done_notice

    def notice_active(self, observer: ObserverState, obs: Observation, now: float) -> str | None:
        """Pure: the notice kind still inside its deadline for this observation, else None."""
        if obs.notice_id is None:
            return None
        track = observer.notices.get(obs.key)
        if track is None or track.voided or track.notice_id != obs.notice_id:
            return None
        if now >= track.started + self._duration(track.kind):
            return None
        return track.kind

    def _sweep_notices(self, now: float) -> None:
        for observer in self.observers.values():
            for track in observer.notices.values():
                if not track.voided and now >= track.started + self._duration(track.kind):
                    track.voided = True
                    self.counters["notices_expired"] += 1

    def expire(self, now: float) -> None:
        for key, observer in list(self.observers.items()):
            if now >= observer.expires_at:
                self._note_retired(observer.generation, "expired")
                del self.observers[key]
                self._prune_highest(key)
                self.counters["expired_observers"] += 1
                self.log(f"observer {key}: generation {observer.generation} lease expired")

    def observe_clock(self) -> None:
        """Detect sleep or a stalled loop before any output: uncertain leases, voided notices."""
        now = self.clock.monotonic()
        wall = self.clock.wall()
        wall_delta = wall - self.last_wall
        mono_delta = now - self.last_mono
        self.last_wall = wall
        self.last_mono = now
        if wall_delta - mono_delta > self.limits.discontinuity or mono_delta > self.limits.discontinuity + self.limits.reaper:
            self.discontinuities += 1
            self.log(f"clock discontinuity: wall +{wall_delta:.1f}s, monotonic +{mono_delta:.1f}s; leases uncertain")
            for observer in self.observers.values():
                observer.uncertain = True
                for track in observer.notices.values():
                    track.voided = True

    def recompute(self, now: float) -> None:
        self.expire(now)
        self._sweep_notices(now)
        contributions = []
        for observer in self.observers.values():
            if observer.uncertain:
                continue
            for obs in observer.observations.values():
                mode = pane_mode(obs.state, self.notice_active(observer, obs, now))
                if mode is not None:
                    contributions.append((obs.key, mode, observer.applied, obs.label))
        self.rows = () if self.disabled else reduce_rows(contributions)
        self.transport.publish(self.rows)
        if self.observers:
            self.empty_since = None
        elif self.empty_since is None:
            self.empty_since = now
        self.transport.tick()

    def next_notice_deadline(self) -> float | None:
        deadlines = []
        for observer in self.observers.values():
            for obs in observer.observations.values():
                if obs.notice_id is None or obs.state != STATE_IDLE:
                    continue
                track = observer.notices.get(obs.key)
                if track is None or track.voided or track.notice_id != obs.notice_id:
                    continue
                deadlines.append(track.started + self._duration(track.kind))
        return min(deadlines) if deadlines else None

    def tick(self) -> str | None:
        """Reaper tick: clocks, leases, notices, retries, and the exit decision. Returns "exit" when done."""
        self.observe_clock()
        now = self.clock.monotonic()
        self.recompute(now)
        if self.stop_requested:
            return "exit"
        if self.observers:
            return None
        idle_for = now - (self.empty_since if self.empty_since is not None else now)
        needs_neutral = self.transport.undelivered_neutral()
        if not needs_neutral and idle_for >= self.limits.exit_grace:
            return "exit"
        if idle_for >= self.limits.final_grace:
            self.log("final grace elapsed with undelivered neutral: " + ", ".join(needs_neutral))
            return "exit"
        return None

    def wake_after(self, now: float) -> float:
        """Seconds until the next thing the reaper must do."""
        due = [self.limits.reaper]
        next_send = self.transport.next_due()
        if next_send is not None:
            due.append(max(0.0, next_send - now))
        deadline = self.next_notice_deadline()
        if deadline is not None:
            due.append(max(0.0, deadline - now))
        for observer in self.observers.values():
            due.append(max(0.0, observer.expires_at - now))
        return max(0.01, min(due))

    def request_stop(self, reason: str = "signal") -> None:
        self.stop_requested = True
        self.stop_reason = reason

    def shutdown(self) -> None:
        """Clear every row this install may show, once, on the way out; bounded by the transport's tick budget.

        This is the last attempt this process will ever make, so retry spacing after a recent
        failure must not skip it. Scopes it cannot clear stay in the ledger for the next coordinator.
        """
        for observer in list(self.observers.values()):
            self._note_retired(observer.generation, "shutdown")
        self.observers.clear()
        self.rows = ()
        self.transport.withdraw()
        self.transport.tick(immediate=True)

    # ---- reporting ------------------------------------------------------------------------

    def row_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.mode] = counts.get(row.mode, 0) + 1
        return {mode: counts[mode] for mode in MODES if mode in counts}

    def rows_summary(self) -> str:
        if not self.rows:
            return "no rows"
        counts = ", ".join(f"{mode} {count}" for mode, count in self.row_counts().items())
        return f"{len(self.rows)} rows: {counts}"

    def status(self) -> dict:
        now = self.clock.monotonic()
        observers = []
        for observer in self.observers.values():
            by_state: dict[str, int] = {}
            notices = []
            for obs in observer.observations.values():
                by_state[obs.state] = by_state.get(obs.state, 0) + 1
                kind = self.notice_active(observer, obs, now)
                if kind:
                    notices.append({"pane": obs.pane, "kind": kind})
            observers.append(
                {
                    "key": observer.key,
                    "generation": observer.generation,
                    "seq": observer.seq,
                    "coverage": observer.coverage,
                    "uncertain": observer.uncertain,
                    "lease_remaining": round(max(0.0, observer.expires_at - now), 3),
                    "snapshots": observer.snapshots,
                    "observations": by_state,
                    "active_notices": notices,
                    "diag": dict(observer.diag),
                }
            )
        return {
            "version": VERSION,
            "schema": PROTOCOL_SCHEMA,
            "pid": os.getpid(),
            "epoch": self.epoch,
            "started_at": iso_utc(self.started_wall),
            "uptime_seconds": round(now - self.started_at, 3),
            "disabled": self.disabled,
            "rows": self.row_counts(),
            "rows_total": len(self.rows),
            "rows_labelled": sum(row.label is not None for row in self.rows),
            "observers": observers,
            "tokens_outstanding": len(self.tokens),
            "generations_issued": self.generation_counter,
            "last_observation_age": None if self.last_observation_at is None else round(now - self.last_observation_at, 3),
            "discontinuities": self.discontinuities,
            "counters": dict(self.counters),
            "stop_reason": self.stop_reason,
            "transport": self.transport.to_dict(),
            "limits": self.limits.to_dict(),
        }


def crash_site(exc: BaseException) -> str:
    """``file:line`` of the innermost frame, without importing ``traceback`` on the hot path."""
    tb = exc.__traceback__
    if tb is None:
        return "?"
    while tb.tb_next is not None:
        tb = tb.tb_next
    return f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"


def decode_request(raw: bytes) -> object:
    try:
        return load_json(raw)
    except ValueError as exc:
        raise ProtocolError("malformed_json", "request is not valid UTF-8 JSON") from exc
