from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .models import is_allowed_callback_url


@dataclass
class CallbackHandle:
    task_id: str
    session_id: str
    user_message_id: str | None
    callback_url: str
    callback_token: str
    chat_id: str
    terminated: bool = False
    delivered: bool = False
    is_reasoning: bool = False
    last_send_at: float = 0.0
    last_activity_at: float = 0.0

    @property
    def uses_callback(self) -> bool:
        return bool(self.callback_url)

    @property
    def callback_is_usable(self) -> bool:
        return self.uses_callback and is_allowed_callback_url(self.callback_url)


class ClosedTurns:
    def __init__(self, capacity: int = 64) -> None:
        self._capacity = max(1, capacity)
        self._by_task: "OrderedDict[str, CallbackHandle]" = OrderedDict()

    def remember(self, handle: CallbackHandle) -> None:
        self._by_task[handle.task_id] = handle
        self._by_task.move_to_end(handle.task_id)
        while len(self._by_task) > self._capacity:
            self._by_task.popitem(last=False)

    def by_task(self, task_id: str) -> CallbackHandle | None:
        return self._by_task.get(task_id)

    def by_chat(self, chat_id: str) -> CallbackHandle | None:
        for handle in reversed(self._by_task.values()):
            if handle.chat_id == chat_id:
                return handle
        return None

    def __len__(self) -> int:
        return len(self._by_task)


class IdempotencyCache:
    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = max(1, capacity)
        self._store: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    def get(self, task_id: str) -> dict[str, Any] | None:
        if task_id in self._store:
            self._store.move_to_end(task_id)
            return self._store[task_id]
        return None

    def put(self, task_id: str, payload: dict[str, Any]) -> None:
        if task_id in self._store:
            self._store.move_to_end(task_id)
            self._store[task_id] = payload
            return
        self._store[task_id] = payload
        while len(self._store) > self._capacity:
            self._store.popitem(last=False)

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._store

    def __len__(self) -> int:
        return len(self._store)
