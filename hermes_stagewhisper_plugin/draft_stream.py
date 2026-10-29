from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class _TurnStreamState:
    part_id: str = ""
    part_open: bool = False
    started: bool = False
    sent_content: str = ""
    last_snapshot: str = ""


def _common_prefix(left: str, right: str) -> str:
    limit = min(len(left), len(right))
    shared = 0
    while shared < limit and left[shared] == right[shared]:
        shared += 1
    return left[:shared]


class StreamChunkTracker:
    def __init__(self) -> None:
        self._turns: dict[str, _TurnStreamState] = {}

    def emit(self, task_id: str, content: str, *, finalize: bool) -> list[dict[str, Any]]:
        chunks = self._advance(task_id, content, settled=finalize)
        if finalize:
            chunks.extend(self._close(task_id))
        return chunks

    def discard(self, task_id: str) -> None:
        self._turns.pop(task_id, None)

    def clear(self) -> None:
        self._turns.clear()

    def _advance(self, task_id: str, content: str, *, settled: bool) -> list[dict[str, Any]]:
        state = self._turns.get(task_id)
        if state is None:
            state = _TurnStreamState()
            self._turns[task_id] = state

        chunks: list[dict[str, Any]] = []
        if not state.started:
            chunks.append({"type": "start", "messageId": task_id})
            state.started = True

        settled_content = content if settled else _common_prefix(state.last_snapshot, content)
        state.last_snapshot = content

        restarting = settled_content and not settled_content.startswith(state.sent_content)
        if not state.part_open or restarting:
            if state.part_open:
                chunks.append({"type": "text-end", "id": state.part_id})
            state.part_id = uuid.uuid4().hex
            state.part_open = True
            state.sent_content = ""
            chunks.append({"type": "text-start", "id": state.part_id})

        delta = settled_content[len(state.sent_content):]
        if delta:
            chunks.append({"type": "text-delta", "id": state.part_id, "delta": delta})
            state.sent_content = settled_content

        return chunks

    def _close(self, task_id: str) -> list[dict[str, Any]]:
        state = self._turns.pop(task_id, None)
        chunks: list[dict[str, Any]] = []
        if state is not None and state.part_open:
            chunks.append({"type": "text-end", "id": state.part_id})
        chunks.append({"type": "finish", "finishReason": "stop"})
        return chunks
