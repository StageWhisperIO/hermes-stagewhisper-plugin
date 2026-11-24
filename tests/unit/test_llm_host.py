from __future__ import annotations

import sys
import types

import pytest

from hermes_stagewhisper_plugin.llm_host import HostLlm, HostLlmUnsupported, HostModel, load_host_model

from .llm_support import FakeHostModel


@pytest.fixture
def fake_agent_module(monkeypatch):
    agent = types.ModuleType("agent")
    auxiliary = types.ModuleType("agent.auxiliary_client")
    agent.auxiliary_client = auxiliary
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary)
    return auxiliary


def test_the_loader_takes_async_call_llm_and_the_configured_model_reader(fake_agent_module) -> None:
    async def async_call_llm(task=None, *, messages, tools=None, max_tokens=None, timeout=None, extra_body=None):
        return None

    fake_agent_module.async_call_llm = async_call_llm
    fake_agent_module._read_main_model = lambda: "deepseek-chat"
    host = load_host_model()
    assert host.call is async_call_llm
    assert host.configured_model() == "deepseek-chat"


def test_the_loader_refuses_an_async_call_llm_that_cannot_take_tools(fake_agent_module) -> None:
    async def async_call_llm(task=None, *, messages, max_tokens=None, timeout=None):
        return None

    fake_agent_module.async_call_llm = async_call_llm
    with pytest.raises(HostLlmUnsupported, match="tools"):
        load_host_model()


def test_the_loader_refuses_a_synchronous_or_missing_function(fake_agent_module) -> None:
    with pytest.raises(HostLlmUnsupported):
        load_host_model()
    fake_agent_module.async_call_llm = lambda **kwargs: None
    with pytest.raises(HostLlmUnsupported):
        load_host_model()


def test_the_loader_fails_without_hermes_agent_installed(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "agent", None)
    with pytest.raises(ImportError):
        load_host_model()


def test_the_check_runs_once_at_construction_and_never_calls_the_model() -> None:
    fake = FakeHostModel()
    host_llm = HostLlm(loader=lambda: HostModel(fake, None))
    assert host_llm.available
    assert host_llm.ping_field() == {"api": "chat_completions"}
    assert fake.calls == []


def test_a_configured_model_reader_that_fails_leaves_the_model_out_of_the_ping() -> None:
    def broken_reader():
        raise RuntimeError("config unreadable")

    host_llm = HostLlm(loader=lambda: HostModel(FakeHostModel(), broken_reader))
    assert host_llm.ping_field() == {"api": "chat_completions"}
