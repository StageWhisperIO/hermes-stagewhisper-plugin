from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hermes_stagewhisper_plugin import delivery as delivery_module
from hermes_stagewhisper_plugin.callbacks import CallbackHandle
from hermes_stagewhisper_plugin.delivery import (
    CallbackAttemptOutcome,
    classify_callback_status,
    retry_callback,
)


CONTRACT_PATH = Path(__file__).resolve().parents[3] / "reply-stream-contract.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["callbackRetryContract"]


def _handle() -> CallbackHandle:
    return CallbackHandle(
        task_id="task-a",
        session_id="session-a",
        user_message_id="message-a",
        chat_id="chat-a",
        callback_url="http://127.0.0.1:9876",
        callback_token="callback-token-32-chars-aaaaaaaa",
    )


def test_the_callback_timeout_matches_the_shared_cross_plugin_contract() -> None:
    assert delivery_module.CALLBACK_TIMEOUT_S * 1000 == CONTRACT["timeoutMilliseconds"]


def test_the_callback_attempt_limit_matches_the_shared_cross_plugin_contract() -> None:
    assert delivery_module.CALLBACK_MAX_ATTEMPTS == CONTRACT["maxAttempts"]


@pytest.mark.parametrize(
    "case", CONTRACT["statusCases"], ids=lambda case: str(case["status"])
)
def test_callback_status_classification_follows_the_shared_cross_plugin_contract(
    case: dict[str, Any],
) -> None:
    assert classify_callback_status(case["status"]).value == case["outcome"]


@pytest.mark.parametrize(
    "case", CONTRACT["transportFailureCases"], ids=lambda case: case["failure"]
)
@pytest.mark.asyncio
async def test_callback_transport_failures_follow_the_shared_cross_plugin_contract(
    case: dict[str, Any],
) -> None:
    attempts = 0

    async def post(handle: CallbackHandle, payload: dict[str, Any]):
        nonlocal attempts
        attempts += 1
        raise ConnectionError(case["failure"])

    delivered = await retry_callback(_handle(), {}, post, sleep=_recorder([]))

    assert delivered is False
    assert attempts == CONTRACT["maxAttempts"]


def _recorder(sink: list[float]):
    async def sleep(seconds: float) -> None:
        sink.append(seconds)

    return sleep


@pytest.mark.parametrize(
    "scenario", CONTRACT["recoveryScenarios"], ids=lambda scenario: scenario["name"]
)
@pytest.mark.asyncio
async def test_callback_recovery_follows_the_shared_cross_plugin_contract(
    scenario: dict[str, Any],
) -> None:
    outcomes = iter(
        CallbackAttemptOutcome(outcome) for outcome in scenario["outcomes"]
    )
    attempts = 0
    backoffs: list[float] = []

    async def post(handle: CallbackHandle, payload: dict[str, Any]):
        nonlocal attempts
        attempts += 1
        return next(outcomes)

    delivered = await retry_callback(_handle(), {}, post, sleep=_recorder(backoffs))

    assert delivered is scenario["expectedDelivered"]
    assert attempts == scenario["expectedAttempts"]
    assert [round(seconds * 1000) for seconds in backoffs] == scenario[
        "expectedBackoffMilliseconds"
    ]
