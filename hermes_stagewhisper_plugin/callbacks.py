from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


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
