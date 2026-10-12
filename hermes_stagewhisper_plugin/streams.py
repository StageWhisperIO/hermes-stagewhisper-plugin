from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque

logger = logging.getLogger(__name__)

DEFAULT_BACKLOG_PER_SESSION = 1024
DEFAULT_BACKLOG_BYTES_PER_SESSION = 4 * 1024 * 1024
DEFAULT_MAX_EVENT_BYTES = 256 * 1024
DEFAULT_MAX_SESSIONS = 256
DEFAULT_MAX_SUBSCRIBERS_PER_SESSION = 4
DEFAULT_MAX_SUBSCRIBERS_TOTAL = 64
DEFAULT_BACKLOG_RETENTION_SECONDS = 30 * 60.0
DURABLE_STATUSES = {"completed", "errored", "message", "silent", "notice", "stream"}
TRANSIENT_STATUSES = {"typing", "tool_call"}


@dataclass
class QueuedReply:
    payload: dict[str, Any]
    serialized_payload: bytes
    size_bytes: int
    event_id: str = ""
    sequence: int = 0


@dataclass
class Drained:
    entries: list[QueuedReply]
    transient_payloads: list[bytes]


@dataclass
class SessionBacklog:
    token: str = ""
    entries: Deque[QueuedReply] = field(default_factory=deque)
    retained_bytes: int = 0
    updated_at: float = 0.0
    next_sequence: int = 0


@dataclass(eq=False)
class Subscriber:
    session_id: str
    pending: Deque[QueuedReply] = field(default_factory=deque)
    pending_bytes: int = 0
    transient_payloads: "OrderedDict[str, bytes]" = field(
        default_factory=OrderedDict
    )
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    dropped: bool = False


class ReplyStreams:
    def __init__(
        self,
        backlog_per_session: int = DEFAULT_BACKLOG_PER_SESSION,
        backlog_bytes_per_session: int = DEFAULT_BACKLOG_BYTES_PER_SESSION,
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_subscribers_per_session: int = DEFAULT_MAX_SUBSCRIBERS_PER_SESSION,
        max_subscribers_total: int = DEFAULT_MAX_SUBSCRIBERS_TOTAL,
        backlog_retention_seconds: float = DEFAULT_BACKLOG_RETENTION_SECONDS,
        now: Callable[[], float] = time.monotonic,
        epoch: str | None = None,
    ) -> None:
        self._backlog_per_session = max(1, backlog_per_session)
        self._backlog_bytes_per_session = max(1, backlog_bytes_per_session)
        self._max_event_bytes = min(
            max(1, max_event_bytes), self._backlog_bytes_per_session
        )
        self._max_sessions = max(1, max_sessions)
        self._max_subscribers_per_session = max(1, max_subscribers_per_session)
        self._max_subscribers_total = max(1, max_subscribers_total)
        self._backlog_retention_seconds = max(0.0, backlog_retention_seconds)
        self._now = now
        self._backlogs: "OrderedDict[str, SessionBacklog]" = OrderedDict()
        self._subscribers: dict[str, set[Subscriber]] = {}
        self._subscriber_count = 0
        self._epoch = epoch if epoch is not None else secrets.token_hex(8)
        self._next_backlog_serial = 0

    def _replay_floor(
        self, backlog: SessionBacklog | None, last_event_id: str | None
    ) -> int | None:
        if backlog is None or not last_event_id:
            return None
        token, separator, raw_sequence = last_event_id.rpartition(".")
        if not separator or token != backlog.token:
            return None
        try:
            sequence = int(raw_sequence)
        except ValueError:
            return None

        if not backlog.entries or sequence > backlog.entries[-1].sequence:
            return None
        return sequence

    def subscribe(
        self, session_id: str, last_event_id: str | None = None
    ) -> Subscriber | None:
        self._prune_expired()
        listeners = self._subscribers.get(session_id, set())
        if (
            len(listeners) >= self._max_subscribers_per_session
            or self._subscriber_count >= self._max_subscribers_total
        ):
            return None

        backlog = self._backlogs.get(session_id)
        floor = self._replay_floor(backlog, last_event_id)
        replayed = [
            entry
            for entry in (backlog.entries if backlog is not None else ())
            if floor is None or entry.sequence > floor
        ]
        subscriber = Subscriber(
            session_id=session_id,
            pending=deque(replayed),
            pending_bytes=sum(entry.size_bytes for entry in replayed),
        )
        self._subscribers.setdefault(session_id, set()).add(subscriber)
        self._subscriber_count += 1
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        listeners = self._subscribers.get(subscriber.session_id)
        if listeners is None or subscriber not in listeners:
            return
        listeners.discard(subscriber)
        self._subscriber_count -= 1
        if not listeners:
            self._subscribers.pop(subscriber.session_id, None)

    def has_listener(self, session_id: str) -> bool:
        return bool(self._subscribers.get(session_id))

    def capture_durable(self, session_id: str, payload: dict[str, Any]) -> bool:
        status = payload.get("status")
        if status is not None and status not in DURABLE_STATUSES:
            return False
        serialized_payload = serialize_payload(payload)
        size_bytes = len(serialized_payload)
        if size_bytes > self._max_event_bytes:
            return False
        self._append(session_id, payload, serialized_payload, size_bytes)
        return True

    def publish_transient(self, session_id: str, payload: dict[str, Any]) -> bool:
        if payload.get("status") not in TRANSIENT_STATUSES:
            return False
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return False
        serialized_payload = serialize_payload(payload)
        if len(serialized_payload) > self._max_event_bytes:
            return False
        for subscriber in self._subscribers.get(session_id, ()):
            if subscriber.dropped:
                continue
            subscriber.transient_payloads[task_id] = serialized_payload
            subscriber.transient_payloads.move_to_end(task_id)
            if len(subscriber.transient_payloads) > self._backlog_per_session:
                self._drop_subscriber(subscriber)
            subscriber.wakeup.set()
        return True

    def drain(self, subscriber: Subscriber) -> Drained:
        entries = list(subscriber.pending)
        subscriber.pending.clear()
        subscriber.pending_bytes = 0
        transient_payloads = list(subscriber.transient_payloads.values())
        subscriber.transient_payloads.clear()
        return Drained(entries=entries, transient_payloads=transient_payloads)

    def retained(self, session_id: str) -> list[QueuedReply]:
        backlog = self._backlogs.get(session_id)
        return list(backlog.entries) if backlog is not None else []

    def forget(self, session_id: str) -> None:
        self._invalidate(session_id)

    def _drop_subscriber(self, subscriber: Subscriber) -> None:
        subscriber.dropped = True
        subscriber.pending.clear()
        subscriber.pending_bytes = 0
        subscriber.transient_payloads.clear()

    def _prune_expired(self) -> None:
        cutoff = self._now() - self._backlog_retention_seconds
        for session_id in list(self._backlogs):
            backlog = self._backlogs.get(session_id)
            if backlog is not None and backlog.updated_at < cutoff:
                self._invalidate(session_id)

    def _append(
        self,
        session_id: str,
        payload: dict[str, Any],
        serialized_payload: bytes,
        size_bytes: int,
    ) -> None:
        self._prune_expired()
        backlog = self._backlogs.get(session_id)
        if backlog is None:
            serial = self._next_backlog_serial
            self._next_backlog_serial += 1
            backlog = SessionBacklog(
                token=f"{self._epoch}-{serial}", updated_at=self._now()
            )
            self._backlogs[session_id] = backlog
        else:
            backlog.updated_at = self._now()
            self._backlogs.move_to_end(session_id)

        sequence = backlog.next_sequence
        backlog.next_sequence += 1
        entry = QueuedReply(
            payload=payload,
            serialized_payload=serialized_payload,
            size_bytes=size_bytes,
            event_id=f"{backlog.token}.{sequence}",
            sequence=sequence,
        )
        backlog.entries.append(entry)
        backlog.retained_bytes += size_bytes
        task_id = payload.get("task_id")
        for subscriber in self._subscribers.get(session_id, ()):
            if subscriber.dropped:
                continue
            subscriber.pending.append(entry)
            subscriber.pending_bytes += size_bytes
            if isinstance(task_id, str):
                subscriber.transient_payloads.pop(task_id, None)
            if (
                len(subscriber.pending) > self._backlog_per_session
                or subscriber.pending_bytes > self._backlog_bytes_per_session
            ):
                if payload.get("status") == "stream":
                    logger.warning(
                        "StageWhisper subscriber dropped mid-stream for session %s (task %s)",
                        session_id,
                        task_id,
                    )
                self._drop_subscriber(subscriber)
            subscriber.wakeup.set()
        while (
            len(backlog.entries) > self._backlog_per_session
            or backlog.retained_bytes > self._backlog_bytes_per_session
        ):
            discarded = backlog.entries.popleft()
            backlog.retained_bytes -= discarded.size_bytes
            if discarded.payload.get("status") == "stream":
                logger.warning(
                    "StageWhisper stream chunk evicted from backlog for session %s (task %s)",
                    session_id,
                    discarded.payload.get("task_id"),
                )
        while len(self._backlogs) > self._max_sessions:
            oldest_session_id = next(iter(self._backlogs))
            self._invalidate(oldest_session_id)

    def _invalidate(self, session_id: str) -> None:
        self._backlogs.pop(session_id, None)


def serialize_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
