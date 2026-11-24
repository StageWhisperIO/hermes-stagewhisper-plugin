from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_stagewhisper_plugin.streams import (
    DURABLE_STATUSES,
    TRANSIENT_STATUSES,
    ReplyStreams,
)


CONTRACT_PATH = Path(__file__).resolve().parents[3] / "reply-stream-contract.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _payload(task_id: str) -> dict[str, str]:
    return {"task_id": task_id, "text": f"reply for {task_id}"}


def _drained_task_ids(streams: ReplyStreams, subscriber: Any) -> list[str]:
    return [entry.payload["task_id"] for entry in streams.drain(subscriber).entries]


def test_a_stream_reply_is_captured_even_when_nobody_is_listening() -> None:
    streams = ReplyStreams()

    streams.capture_durable("session-a", _payload("t1"))

    arriving_later = streams.subscribe("session-a")
    assert _drained_task_ids(streams, arriving_later) == ["t1"]


def test_a_notice_status_reply_is_captured_as_durable_and_not_rewritten_into_an_error() -> None:
    streams = ReplyStreams()
    payload = {"task_id": "t1", "status": "notice", "reply_text": "Compressing context."}

    captured = streams.capture_durable("session-a", payload)

    assert captured is True
    retained = streams.retained("session-a")
    assert len(retained) == 1
    assert retained[0].payload["status"] == "notice"


def test_two_clients_on_the_same_session_both_receive_a_captured_reply() -> None:
    streams = ReplyStreams()
    first = streams.subscribe("session-a")
    second = streams.subscribe("session-a")

    streams.capture_durable("session-a", _payload("t1"))

    assert _drained_task_ids(streams, first) == ["t1"]
    assert _drained_task_ids(streams, second) == ["t1"]


def test_a_reply_for_one_session_never_reaches_another_session() -> None:
    streams = ReplyStreams()
    listener_a = streams.subscribe("session-a")
    listener_b = streams.subscribe("session-b")

    streams.capture_durable("session-a", _payload("t1"))

    assert _drained_task_ids(streams, listener_a) == ["t1"]
    assert _drained_task_ids(streams, listener_b) == []


def test_disconnect_state_never_controls_whether_a_stream_reply_is_retained() -> None:
    streams = ReplyStreams()
    disconnected = streams.subscribe("session-a")
    streams.unsubscribe(disconnected)

    streams.capture_durable("session-a", _payload("t1"))

    reconnected = streams.subscribe("session-a")
    assert streams.has_listener("session-a") is True
    assert _drained_task_ids(streams, reconnected) == ["t1"]


def test_the_backlog_for_each_session_is_bounded() -> None:
    streams = ReplyStreams(backlog_per_session=3)
    for index in range(6):
        streams.capture_durable("session-a", _payload(f"t{index}"))

    kept = [entry.payload["task_id"] for entry in streams.retained("session-a")]
    assert kept == ["t3", "t4", "t5"]


def test_the_number_of_sessions_with_retained_replies_is_bounded() -> None:
    streams = ReplyStreams(max_sessions=2)
    streams.capture_durable("session-a", _payload("t1"))
    streams.capture_durable("session-b", _payload("t2"))
    streams.capture_durable("session-c", _payload("t3"))

    assert streams.retained("session-a") == []
    assert len(streams.retained("session-b")) == 1
    assert len(streams.retained("session-c")) == 1


def test_an_oversized_stream_reply_is_never_captured_or_delivered() -> None:
    streams = ReplyStreams(backlog_bytes_per_session=64, max_event_bytes=64)
    subscriber = streams.subscribe("session-a")

    captured = streams.capture_durable("session-a", _payload("x" * 128))
    drained = streams.drain(subscriber)

    assert captured is False
    assert drained.entries == []


def test_a_later_oversized_reply_does_not_erase_previously_retained_replies() -> None:
    streams = ReplyStreams(backlog_bytes_per_session=4096, max_event_bytes=64)
    streams.capture_durable("session-a", _payload("t1"))

    captured = streams.capture_durable("session-a", _payload("x" * 128))

    assert captured is False
    assert [entry.payload["task_id"] for entry in streams.retained("session-a")] == ["t1"]


def test_a_fresh_client_receives_every_currently_retained_reply() -> None:
    streams = ReplyStreams(backlog_per_session=1)
    for index in range(5):
        streams.capture_durable("session-a", _payload(f"t{index}"))

    drained = streams.drain(streams.subscribe("session-a"))

    assert [entry.payload["task_id"] for entry in drained.entries] == ["t4"]


def test_a_subscriber_is_dropped_once_its_pending_queue_exceeds_the_backlog_cap_instead_of_growing_without_bound() -> None:
    streams = ReplyStreams(backlog_per_session=2)
    subscriber = streams.subscribe("session-a")

    for index in range(6):
        streams.capture_durable("session-a", _payload(f"t{index}"))

    assert subscriber.dropped is True
    assert streams.drain(subscriber).entries == []
    assert [entry.payload["task_id"] for entry in streams.retained("session-a")] == [
        "t4",
        "t5",
    ]


def test_a_subscriber_dropped_for_overflowing_its_queue_recovers_everything_still_retained_by_reconnecting() -> None:
    streams = ReplyStreams(backlog_per_session=2)
    subscriber = streams.subscribe("session-a")
    for index in range(6):
        streams.capture_durable("session-a", _payload(f"t{index}"))
    assert subscriber.dropped is True

    reconnected = streams.subscribe("session-a")
    assert _drained_task_ids(streams, reconnected) == ["t4", "t5"]


def test_a_subscriber_is_dropped_once_its_pending_byte_total_exceeds_the_backlog_byte_cap() -> None:
    streams = ReplyStreams(
        backlog_per_session=100, backlog_bytes_per_session=200, max_event_bytes=200
    )
    subscriber = streams.subscribe("session-a")

    streams.capture_durable("session-a", _payload("a" * 80))
    assert subscriber.dropped is False
    streams.capture_durable("session-a", _payload("b" * 80))
    streams.capture_durable("session-a", _payload("c" * 80))

    assert subscriber.dropped is True


def test_a_subscriber_is_dropped_once_its_distinct_transient_tasks_exceed_the_backlog_cap() -> None:
    streams = ReplyStreams(backlog_per_session=2)
    subscriber = streams.subscribe("session-a")

    streams.publish_transient("session-a", {"task_id": "t1", "status": "typing"})
    streams.publish_transient("session-a", {"task_id": "t2", "status": "typing"})
    assert subscriber.dropped is False
    streams.publish_transient("session-a", {"task_id": "t3", "status": "typing"})

    assert subscriber.dropped is True


def test_transient_progress_is_live_only_and_never_enters_the_durable_backlog() -> None:
    streams = ReplyStreams()
    subscriber = streams.subscribe("session-a")

    assert streams.publish_transient(
        "session-a", {"task_id": "t1", "status": "typing"}
    )

    drained = streams.drain(subscriber)
    assert [json.loads(payload) for payload in drained.transient_payloads] == [
        {"task_id": "t1", "status": "typing"}
    ]
    assert streams.retained("session-a") == []


def test_repeated_progress_for_one_task_is_coalesced_until_the_stream_drains() -> None:
    streams = ReplyStreams()
    subscriber = streams.subscribe("session-a")

    streams.publish_transient(
        "session-a", {"task_id": "t1", "status": "typing", "label": "first"}
    )
    streams.publish_transient(
        "session-a", {"task_id": "t1", "status": "tool_call", "label": "second"}
    )

    drained = streams.drain(subscriber)
    assert [json.loads(payload) for payload in drained.transient_payloads] == [
        {
            "task_id": "t1",
            "status": "tool_call",
            "label": "second",
        }
    ]


def test_a_durable_answer_removes_queued_progress_for_the_same_task() -> None:
    streams = ReplyStreams()
    subscriber = streams.subscribe("session-a")
    streams.publish_transient(
        "session-a", {"task_id": "t1", "status": "typing"}
    )

    streams.capture_durable(
        "session-a", {"task_id": "t1", "status": "message", "reply_text": "done"}
    )

    drained = streams.drain(subscriber)
    assert drained.transient_payloads == []
    assert [entry.payload["task_id"] for entry in drained.entries] == ["t1"]


def test_the_number_of_streams_per_session_is_bounded() -> None:
    streams = ReplyStreams(max_subscribers_per_session=2)

    assert streams.subscribe("session-a") is not None
    assert streams.subscribe("session-a") is not None
    assert streams.subscribe("session-a") is None
    assert streams.subscribe("session-b") is not None


def test_the_total_number_of_open_streams_is_bounded() -> None:
    streams = ReplyStreams(max_subscribers_per_session=10, max_subscribers_total=3)

    accepted = [streams.subscribe(f"session-{index}") for index in range(5)]

    assert sum(subscriber is not None for subscriber in accepted) == 3
    assert accepted[3] is None and accepted[4] is None


def test_unsubscribing_twice_does_not_corrupt_the_stream_budget() -> None:
    streams = ReplyStreams(max_subscribers_total=2)
    subscriber = streams.subscribe("session-a")
    streams.unsubscribe(subscriber)
    streams.unsubscribe(subscriber)

    assert streams.subscribe("session-a") is not None
    assert streams.subscribe("session-b") is not None
    assert streams.subscribe("session-c") is None


def test_a_refused_stream_does_not_leave_a_dangling_listener() -> None:
    streams = ReplyStreams(max_subscribers_total=1)
    streams.subscribe("session-a")

    assert streams.subscribe("session-b") is None
    assert streams.has_listener("session-b") is False


def test_a_stream_slot_can_be_reused_after_its_client_disconnects() -> None:
    streams = ReplyStreams(max_subscribers_total=1)
    first = streams.subscribe("session-a")
    assert streams.subscribe("session-b") is None

    streams.unsubscribe(first)

    assert streams.subscribe("session-b") is not None


def test_stream_status_belongs_to_durable_statuses_and_never_to_transient_statuses() -> None:
    assert "stream" in DURABLE_STATUSES
    assert "stream" not in TRANSIENT_STATUSES


def test_every_stream_chunk_is_captured_as_durable_and_retained_in_arrival_order() -> None:
    streams = ReplyStreams()

    first = streams.capture_durable(
        "session-a",
        {
            "task_id": "t1",
            "status": "stream",
            "chunk": {"type": "text-delta", "id": "p1", "delta": "Hel"},
        },
    )
    second = streams.capture_durable(
        "session-a",
        {
            "task_id": "t1",
            "status": "stream",
            "chunk": {"type": "text-delta", "id": "p1", "delta": "lo"},
        },
    )

    assert first is True
    assert second is True
    retained = streams.retained("session-a")
    assert [entry.payload["chunk"]["delta"] for entry in retained] == ["Hel", "lo"]


def test_stream_chunks_for_the_same_task_are_never_coalesced_into_the_latest_one() -> None:
    streams = ReplyStreams()
    subscriber = streams.subscribe("session-a")

    streams.capture_durable(
        "session-a",
        {"task_id": "t1", "status": "stream", "chunk": {"type": "text-delta", "delta": "a"}},
    )
    streams.capture_durable(
        "session-a",
        {"task_id": "t1", "status": "stream", "chunk": {"type": "text-delta", "delta": "b"}},
    )

    drained = streams.drain(subscriber)
    assert [entry.payload["chunk"]["delta"] for entry in drained.entries] == ["a", "b"]


def test_publish_transient_refuses_a_stream_status_payload() -> None:
    streams = ReplyStreams()

    accepted = streams.publish_transient(
        "session-a", {"task_id": "t1", "status": "stream", "chunk": {"type": "finish"}}
    )

    assert accepted is False


def test_forgetting_a_session_removes_its_retained_replies() -> None:
    streams = ReplyStreams()
    streams.capture_durable("session-a", _payload("t1"))

    streams.forget("session-a")

    assert streams.retained("session-a") == []


@pytest.mark.parametrize(
    "scenario", CONTRACT["unicodeByteScenarios"], ids=lambda scenario: scenario["name"]
)
def test_unicode_payload_sizing_follows_the_shared_cross_plugin_contract(
    scenario: dict[str, Any],
) -> None:
    streams = ReplyStreams(
        max_event_bytes=scenario["maxEventBytes"],
        backlog_bytes_per_session=scenario["backlogBytesPerSession"],
    )

    results = [
        streams.capture_durable(
            "session-a",
            {
                "task_id": capture["taskId"],
                "text": capture["textUnit"] * capture["repeat"],
            },
        )
        for capture in scenario["captures"]
    ]

    retained = streams.retained("session-a")
    assert results == scenario["expectedCaptureResults"]
    assert [entry.size_bytes for entry in retained] == scenario["expectedSizes"]
    assert [
        entry.payload["task_id"] for entry in retained
    ] == scenario["expectedTaskIds"]


def test_a_sessions_retained_replies_are_dropped_once_the_retention_ceiling_elapses_since_its_last_activity() -> None:
    clock = [0.0]
    streams = ReplyStreams(backlog_retention_seconds=1.0, now=lambda: clock[0])

    streams.capture_durable("session-a", _payload("t1"))
    clock[0] += 1.5
    streams.capture_durable("session-b", _payload("t2"))

    assert streams.retained("session-a") == []
    assert len(streams.retained("session-b")) == 1


def test_a_sessions_retained_replies_stay_alive_while_it_remains_within_the_retention_ceiling() -> None:
    clock = [0.0]
    streams = ReplyStreams(backlog_retention_seconds=1.0, now=lambda: clock[0])

    streams.capture_durable("session-a", _payload("t1"))
    clock[0] += 0.5
    streams.capture_durable("session-a", _payload("t2"))
    clock[0] += 0.5
    streams.capture_durable("session-b", _payload("t3"))

    assert [entry.payload["task_id"] for entry in streams.retained("session-a")] == [
        "t1",
        "t2",
    ]


@pytest.mark.asyncio
async def test_a_waiting_stream_is_woken_when_a_reply_is_captured() -> None:
    streams = ReplyStreams()
    subscriber = streams.subscribe("session-a")
    subscriber.wakeup.clear()
    waiter = asyncio.create_task(subscriber.wakeup.wait())
    await asyncio.sleep(0)

    streams.capture_durable("session-a", _payload("t1"))

    await asyncio.wait_for(waiter, timeout=1)
    assert _drained_task_ids(streams, subscriber) == ["t1"]


def test_a_reconnecting_subscriber_only_replays_entries_it_has_not_seen() -> None:
    streams = ReplyStreams(epoch="e1")
    streams.capture_durable("session-a", _payload("t1"))
    streams.capture_durable("session-a", _payload("t2"))

    first = streams.subscribe("session-a")
    assert first is not None
    seen = streams.drain(first).entries
    assert [entry.payload["task_id"] for entry in seen] == ["t1", "t2"]
    streams.unsubscribe(first)

    streams.capture_durable("session-a", _payload("t3"))

    resumed = streams.subscribe("session-a", seen[-1].event_id)
    assert resumed is not None
    assert _drained_task_ids(streams, resumed) == ["t3"]


def test_a_cursor_at_the_newest_entry_replays_nothing() -> None:
    streams = ReplyStreams(epoch="e1")
    streams.capture_durable("session-a", _payload("t1"))

    first = streams.subscribe("session-a")
    assert first is not None
    seen = streams.drain(first).entries
    streams.unsubscribe(first)

    resumed = streams.subscribe("session-a", seen[0].event_id)
    assert resumed is not None
    assert _drained_task_ids(streams, resumed) == []


def test_a_cursor_from_a_different_server_run_replays_the_whole_backlog() -> None:
    streams = ReplyStreams(epoch="e2")
    streams.capture_durable("session-a", _payload("t1"))
    streams.capture_durable("session-a", _payload("t2"))

    resumed = streams.subscribe("session-a", "e1-0.9999")
    assert resumed is not None
    assert _drained_task_ids(streams, resumed) == ["t1", "t2"]


def test_a_missing_or_malformed_cursor_replays_the_whole_backlog() -> None:
    streams = ReplyStreams(epoch="e1")
    streams.capture_durable("session-a", _payload("t1"))

    for cursor in (None, "nonsense", "e1-0.notanumber"):
        subscriber = streams.subscribe("session-a", cursor)
        assert subscriber is not None
        assert _drained_task_ids(streams, subscriber) == ["t1"]


def test_every_entry_gets_a_distinct_id_carrying_the_server_run() -> None:
    streams = ReplyStreams(epoch="e1")
    streams.capture_durable("session-a", _payload("t1"))
    streams.capture_durable("session-b", _payload("t2"))

    first = streams.subscribe("session-a")
    second = streams.subscribe("session-b")
    assert first is not None and second is not None

    assert streams.drain(first).entries[0].event_id == "e1-0.0"
    assert streams.drain(second).entries[0].event_id == "e1-1.0"


def test_a_cursor_minted_for_another_session_never_suppresses_this_one() -> None:
    streams = ReplyStreams(epoch="e1")
    for task_id in ("b1", "b2", "b3"):
        streams.capture_durable("session-busy", _payload(task_id))
    streams.capture_durable("session-quiet", _payload("q1"))

    busy = streams.subscribe("session-busy")
    assert busy is not None
    foreign_cursor = streams.drain(busy).entries[-1].event_id

    resumed = streams.subscribe("session-quiet", foreign_cursor)
    assert resumed is not None
    assert _drained_task_ids(streams, resumed) == ["q1"]


def test_a_cursor_pointing_past_the_newest_retained_entry_is_ignored() -> None:
    streams = ReplyStreams(epoch="e1")
    streams.capture_durable("session-a", _payload("t1"))

    first = streams.subscribe("session-a")
    assert first is not None
    token = streams.drain(first).entries[0].event_id.rpartition(".")[0]

    resumed = streams.subscribe("session-a", f"{token}.9999")
    assert resumed is not None
    assert _drained_task_ids(streams, resumed) == ["t1"]


def test_a_cursor_predating_evicted_entries_replays_everything_still_retained() -> None:
    streams = ReplyStreams(epoch="e1", backlog_per_session=3)
    streams.capture_durable("session-a", _payload("t1"))

    first = streams.subscribe("session-a")
    assert first is not None
    stale_cursor = streams.drain(first).entries[0].event_id

    for task_id in ("t2", "t3", "t4", "t5"):
        streams.capture_durable("session-a", _payload(task_id))

    resumed = streams.subscribe("session-a", stale_cursor)
    assert resumed is not None
    assert _drained_task_ids(streams, resumed) == ["t3", "t4", "t5"]
