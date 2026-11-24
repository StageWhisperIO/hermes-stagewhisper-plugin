from __future__ import annotations

import importlib
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiohttp import web

from .llm_replay import Completion, completion_from_host, without_tool_calls
from .llm_request import ChatRequest

logger = logging.getLogger(__name__)

LLM_API = "chat_completions"
HOST_MODULE = "agent.auxiliary_client"
REQUIRED_PARAMETERS = ("messages", "tools", "extra_body", "max_tokens", "timeout")
CALL_TIMEOUT_SECONDS = 110.0


class HostLlmUnsupported(Exception):
    pass


@dataclass(frozen=True)
class HostModel:
    call: Callable[..., Awaitable[Any]]
    configured_model: Callable[[], Any] | None


def load_host_model() -> HostModel:
    module = importlib.import_module(HOST_MODULE)
    call = getattr(module, "async_call_llm", None)
    if not inspect.iscoroutinefunction(call):
        raise HostLlmUnsupported(f"{HOST_MODULE}.async_call_llm is missing")
    missing = [name for name in REQUIRED_PARAMETERS if name not in inspect.signature(call).parameters]
    if missing:
        raise HostLlmUnsupported(f"{HOST_MODULE}.async_call_llm lacks {', '.join(missing)}")
    configured_model = getattr(module, "_read_main_model", None)
    return HostModel(call, configured_model if callable(configured_model) else None)


class HostLlm:
    def __init__(self, loader: Callable[[], HostModel] = load_host_model) -> None:
        try:
            self._host: HostModel | None = loader()
        except Exception as exc:
            self._host = None
            logger.info("StageWhisper Library model endpoint is off: %s", exc)
        self.refused_by_host = False

    @property
    def available(self) -> bool:
        return self._host is not None and not self.refused_by_host

    def ping_field(self) -> dict[str, str]:
        self.refused_by_host = False
        if self._host is None:
            return {"api": "none"}
        field = {"api": LLM_API}
        model = self._configured_model()
        if model:
            field["model"] = model
        return field

    def _configured_model(self) -> str:
        if self._host is None or self._host.configured_model is None:
            return ""
        try:
            model = self._host.configured_model()
        except Exception:
            return ""
        return model.strip() if isinstance(model, str) else ""

    async def complete(self, request: ChatRequest) -> Completion:
        if self._host is None:
            raise HostLlmUnsupported("the host model is not available")
        arguments: dict[str, Any] = {
            "messages": request.messages,
            "tools": request.tools or None,
            "max_tokens": request.max_tokens,
            "timeout": CALL_TIMEOUT_SECONDS,
        }
        if request.tool_choice not in (None, "auto"):
            arguments["extra_body"] = {"tool_choice": request.tool_choice}
        completion = completion_from_host(await self._host.call(**arguments), self._configured_model() or "hermes")
        return without_tool_calls(completion) if request.tool_choice == "none" else completion


HOST_LLM = web.AppKey("host_llm", HostLlm)
