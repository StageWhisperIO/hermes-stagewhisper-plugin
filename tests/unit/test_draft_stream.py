from __future__ import annotations

from hermes_stagewhisper_plugin.draft_stream import StreamChunkTracker


def test_the_first_snapshot_opens_the_turn_without_committing_any_text_yet() -> None:
    tracker = StreamChunkTracker()

    chunks = tracker.emit("t1", "Hello", finalize=False)

    assert chunks == [
        {"type": "start", "messageId": "t1"},
        {"type": "text-start", "id": chunks[1]["id"]},
    ]


def test_text_is_committed_once_two_consecutive_snapshots_agree_on_it() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=False)

    chunks = tracker.emit("t1", "Hello world", finalize=False)

    assert len(chunks) == 1
    assert chunks[0]["type"] == "text-delta"
    assert chunks[0]["delta"] == "Hello"


def test_a_plain_append_emits_one_delta_carrying_only_the_new_suffix() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=False)
    tracker.emit("t1", "Hello world", finalize=False)

    chunks = tracker.emit("t1", "Hello world again", finalize=False)

    assert len(chunks) == 1
    assert chunks[0]["delta"] == " world"


def test_a_draft_cursor_that_moves_with_every_snapshot_never_repeats_committed_text() -> None:
    tracker = StreamChunkTracker()
    snapshots = ["He ▉", "Hell ▉", "Hello wor ▉", "Hello world ▉"]

    emitted = []
    for snapshot in snapshots:
        emitted.extend(tracker.emit("t1", snapshot, finalize=False))
    emitted.extend(tracker.emit("t1", "Hello world", finalize=True))

    assert sum(1 for chunk in emitted if chunk["type"] == "text-start") == 1
    text = "".join(chunk["delta"] for chunk in emitted if chunk["type"] == "text-delta")
    assert text == "Hello world"


def test_repeating_the_same_content_commits_it_once_and_then_emits_nothing() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=False)
    tracker.emit("t1", "Hello", finalize=False)

    chunks = tracker.emit("t1", "Hello", finalize=False)

    assert chunks == []


def test_a_rewrite_before_anything_was_committed_emits_no_chunks_at_all() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=False)

    chunks = tracker.emit("t1", "Goodbye", finalize=False)

    assert chunks == []


def test_a_rewrite_of_committed_text_closes_the_current_part_and_opens_a_new_one() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=False)
    tracker.emit("t1", "Hello", finalize=False)
    tracker.emit("t1", "Goodbye", finalize=False)

    chunks = tracker.emit("t1", "Goodbye", finalize=False)

    assert [chunk["type"] for chunk in chunks] == ["text-end", "text-start", "text-delta"]
    text_end, text_start, text_delta = chunks
    assert text_start["id"] != text_end["id"]
    assert text_delta["id"] == text_start["id"]
    assert text_delta["delta"] == "Goodbye"


def test_shrinking_the_content_commits_only_the_shared_prefix() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello world", finalize=False)

    chunks = tracker.emit("t1", "Hello", finalize=False)

    assert [chunk["type"] for chunk in chunks] == ["text-delta"]
    assert chunks[0]["delta"] == "Hello"


def test_finalize_commits_the_whole_snapshot_then_emits_text_end_and_finish() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=False)

    chunks = tracker.emit("t1", "Hello world", finalize=True)

    assert [chunk["type"] for chunk in chunks] == ["text-delta", "text-end", "finish"]
    assert chunks[0]["delta"] == "Hello world"
    assert chunks[-1] == {"type": "finish", "finishReason": "stop"}


def test_finalize_flushes_the_tail_that_was_still_waiting_for_confirmation() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=False)
    tracker.emit("t1", "Hello world", finalize=False)

    chunks = tracker.emit("t1", "Hello world", finalize=True)

    assert chunks[0]["type"] == "text-delta"
    assert chunks[0]["delta"] == " world"


def test_finalize_with_no_part_ever_opened_still_emits_a_finish_chunk() -> None:
    tracker = StreamChunkTracker()

    chunks = tracker.emit("t1", "", finalize=True)

    assert chunks[-1] == {"type": "finish", "finishReason": "stop"}


def test_finalize_forgets_the_turn_so_a_later_call_with_the_same_task_id_starts_fresh() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=True)

    chunks = tracker.emit("t1", "Hi again", finalize=False)

    assert chunks[0] == {"type": "start", "messageId": "t1"}


def test_discarding_a_turn_makes_the_next_call_for_that_task_start_fresh() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=False)

    tracker.discard("t1")
    chunks = tracker.emit("t1", "Hello", finalize=False)

    assert chunks[0] == {"type": "start", "messageId": "t1"}


def test_two_turns_stream_independently_without_sharing_state() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=False)

    chunks = tracker.emit("t2", "Hi", finalize=False)

    assert chunks[0] == {"type": "start", "messageId": "t2"}
    assert chunks[1]["type"] == "text-start"


def test_clear_removes_every_turns_state() -> None:
    tracker = StreamChunkTracker()
    tracker.emit("t1", "Hello", finalize=False)
    tracker.emit("t2", "Hi", finalize=False)

    tracker.clear()
    chunks = tracker.emit("t1", "Hello", finalize=False)

    assert chunks[0] == {"type": "start", "messageId": "t1"}
