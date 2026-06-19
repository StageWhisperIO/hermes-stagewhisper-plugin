from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def _install_gateway_stub() -> None:
    if "gateway" in sys.modules and getattr(sys.modules["gateway"], "_stagewhisper_stub", False):
        return

    gateway = types.ModuleType("gateway")
    gateway._stagewhisper_stub = True

    config = types.ModuleType("gateway.config")

    @dataclass
    class Platform:
        name: str

    @dataclass
    class PlatformConfig:
        extra: dict[str, Any] = field(default_factory=dict)

    config.Platform = Platform
    config.PlatformConfig = PlatformConfig

    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")

    class MessageType(Enum):
        TEXT = "text"

    @dataclass
    class SourceEvent:
        chat_id: str
        chat_name: str
        chat_type: str
        user_id: str
        user_name: str
        platform: Any = None

    @dataclass
    class MessageEvent:
        text: str
        message_type: MessageType
        source: SourceEvent
        message_id: str
        raw_message: dict[str, Any] | None = None
        metadata: dict[str, Any] | None = None

    @dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        error: str | None = None

    class BasePlatformAdapter:
        def __init__(self, config: Any, platform: Any) -> None:
            self.config = config
            self.platform = platform
            self.connected = False
            self.handle_message_calls: list[MessageEvent] = []

        def _mark_connected(self) -> None:
            self.connected = True

        def _mark_disconnected(self) -> None:
            self.connected = False

        def build_source(
            self,
            *,
            chat_id: str,
            chat_name: str,
            chat_type: str,
            user_id: str,
            user_name: str,
        ) -> SourceEvent:
            return SourceEvent(
                chat_id=chat_id,
                chat_name=chat_name,
                chat_type=chat_type,
                user_id=user_id,
                user_name=user_name,
                platform=self.platform,
            )

        async def handle_message(self, event: MessageEvent) -> None:
            self.handle_message_calls.append(event)

        async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
            return None

        async def stop_typing(self, chat_id: str) -> None:
            return None

        async def _keep_typing(
            self,
            chat_id: str,
            interval: float = 2.0,
            metadata: Any = None,
            stop_event: Any = None,
        ) -> None:
            return None

        async def interrupt_session_activity(
            self, session_key: str, chat_id: str
        ) -> None:
            return None

    base.MessageType = MessageType
    base.MessageEvent = MessageEvent
    base.SendResult = SendResult
    base.SourceEvent = SourceEvent
    base.BasePlatformAdapter = BasePlatformAdapter

    sys.modules["gateway"] = gateway
    sys.modules["gateway.config"] = config
    sys.modules["gateway.platforms"] = platforms
    sys.modules["gateway.platforms.base"] = base


_install_gateway_stub()
