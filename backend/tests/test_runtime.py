"""TrackingFlowManager and ClientReadyGate, driven through the real FlowManager
transition path with fake LLM/transport plumbing (no network)."""

import copy
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import EndFrame, LLMSetToolsFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.openai.llm import OpenAILLMService
from pipecat_flows import FlowInitializationError

from agent_builder import AgentBuilder
from agent_builder.runtime import ClientReadyGate, TrackingFlowManager
from tests.helpers import EXAMPLE


class Call:
    """A FlowManager wired to a worker whose queued frames land in `timeline`."""

    def __init__(self, config: dict = EXAMPLE):
        self.builder = AgentBuilder.from_dict(copy.deepcopy(config))
        self.timeline: list = []  # queued frames and node_entered events, in order
        self.failures: list[Exception] = []

        worker = PipelineWorker(Pipeline([]), enable_rtvi=False)

        async def queue_frame(frame, direction=None):
            self.timeline.append(frame)

        async def queue_frames(frames, direction=None):
            self.timeline.extend(frames)

        worker.queue_frame = queue_frame
        worker.queue_frames = queue_frames

        async def on_node_entered(event):
            self.timeline.append(event)

        async def on_transition_failed(error):
            self.failures.append(error)

        self.flow_manager = TrackingFlowManager(
            llm=OpenAILLMService(api_key="test", model="gpt-4o"),
            context_aggregator=LLMContextAggregatorPair(LLMContext()),
            worker=worker,
            on_node_entered=on_node_entered,
            on_transition_failed=on_transition_failed,
        )

    @property
    def events(self) -> list[dict]:
        return [e for e in self.timeline if isinstance(e, dict)]

    async def start(self):
        await self.flow_manager.initialize(self.builder.build_initial_node())

    async def call_function(self, name: str, arguments: dict):
        """Invoke a tool the way the LLM service does, including the context-updated hook."""
        tools = [f for f in self.timeline if isinstance(f, LLMSetToolsFrame)][-1].tools
        schema = next(t for t in tools.standard_tools if t.name == name)
        results = []

        async def result_callback(result, *, properties=None):
            results.append(result)
            if properties and properties.on_context_updated:
                await properties.on_context_updated()

        await schema.handler(SimpleNamespace(arguments=arguments, result_callback=result_callback))
        return results[0]


async def test_initial_node_reported_after_activation():
    call = Call()
    await call.start()

    assert call.events == [
        {"type": "node_entered", "from": None, "node": "greeting", "via": None, "state": {}}
    ]
    # Context and tools were queued before the entry was reported.
    entry = call.timeline.index(call.events[0])
    assert any(isinstance(f, LLMSetToolsFrame) for f in call.timeline[:entry])


async def test_transition_reports_from_via_and_state():
    call = Call()
    await call.start()
    await call.call_function("choose_intent", {"intent": "book"})

    assert call.events[-1] == {
        "type": "node_entered",
        "from": "greeting",
        "node": "collect_details",
        "via": "choose_intent",
        "state": {"intent": "book"},
    }


async def test_state_is_a_snapshot():
    call = Call()
    await call.start()
    await call.call_function("choose_intent", {"intent": "book"})
    call.flow_manager.state["intent"] = "cancel"

    assert call.events[-1]["state"] == {"intent": "book"}


async def test_terminal_node_reported_before_shutdown():
    call = Call()
    await call.start()
    await call.call_function("choose_intent", {"intent": "book"})
    await call.call_function("record_details", {"full_name": "Ada", "reason": "checkup"})
    await call.call_function("select_time", {"slot": "Tuesday 10 AM"})

    assert [e["node"] for e in call.events] == [
        "greeting",
        "collect_details",
        "offer_times",
        "confirm",
    ]
    assert call.events[-1]["state"] == {
        "intent": "book",
        "full_name": "Ada",
        "reason": "checkup",
        "slot": "Tuesday 10 AM",
    }
    end = next(i for i, f in enumerate(call.timeline) if isinstance(f, EndFrame))
    assert call.timeline.index(call.events[-1]) < end


async def test_failed_transition_is_not_reported():
    call = Call()
    await call.start()

    original = call.flow_manager._update_llm_context

    async def broken(**kwargs):
        raise RuntimeError("context update failed")

    call.flow_manager._update_llm_context = broken
    await call.call_function("choose_intent", {"intent": "book"})

    assert [e["node"] for e in call.events] == ["greeting"]
    assert call.flow_manager.current_node == "greeting"
    assert len(call.failures) == 1

    # The adapter is not left mid-transition: the next activation is attributed correctly.
    call.flow_manager._update_llm_context = original
    await call.call_function("choose_intent", {"intent": "book"})
    assert call.events[-1]["from"] == "greeting"
    assert call.events[-1]["via"] == "choose_intent"


async def test_failed_initialization_is_not_reported():
    call = Call()

    async def broken(**kwargs):
        raise RuntimeError("context update failed")

    call.flow_manager._update_llm_context = broken
    with pytest.raises(FlowInitializationError):
        await call.start()

    assert call.events == []


async def test_node_with_pre_and_post_actions_reported_once_between_them():
    call = Call()

    def marker(label):
        async def handler(action, flow_manager):
            call.timeline.append(label)

        return handler

    # Custom actions run inline (tts_say would wait for the pipeline to finish speaking).
    node = call.builder.build_initial_node()
    node["pre_actions"] = [{"type": "pre_marker", "handler": marker("pre")}]
    node["post_actions"] = [{"type": "post_marker", "handler": marker("post")}]
    await call.flow_manager.initialize(node)

    assert len(call.events) == 1
    entry = call.timeline.index(call.events[0])
    assert call.timeline.index("pre") < entry < call.timeline.index("post")


# ---- ClientReadyGate ---------------------------------------------------------


class FakeRTVI:
    def __init__(self):
        self.handlers = {}
        self.sent: list[tuple[str, object]] = []

    def add_event_handler(self, name, handler):
        self.handlers.setdefault(name, []).append(handler)

    async def set_client_ready(self):
        for handler in self.handlers.get("on_client_ready", []):
            await handler(self)

    async def send_server_message(self, data):
        self.sent.append(("server_message", data))

    async def send_error(self, error):
        self.sent.append(("error", error))


async def test_gate_flow_starts_before_client_ready():
    rtvi = FakeRTVI()
    gate = ClientReadyGate(rtvi)
    call = Call()
    call.flow_manager._on_node_entered = gate.send_server_message
    await call.start()

    assert rtvi.sent == []
    await rtvi.set_client_ready()
    assert [(kind, data["node"]) for kind, data in rtvi.sent] == [("server_message", "greeting")]


async def test_gate_client_ready_before_flow_starts():
    rtvi = FakeRTVI()
    gate = ClientReadyGate(rtvi)
    await rtvi.set_client_ready()
    call = Call()
    call.flow_manager._on_node_entered = gate.send_server_message
    await call.start()

    assert [(kind, data["node"]) for kind, data in rtvi.sent] == [("server_message", "greeting")]


async def test_gate_runs_after_delivery_callbacks_once_flushed():
    rtvi = FakeRTVI()
    gate = ClientReadyGate(rtvi)
    ended = []

    async def end():
        ended.append(list(rtvi.sent))

    await gate.send_error("boom")
    await gate.after_delivery(end)
    assert ended == []

    await rtvi.set_client_ready()
    await rtvi.set_client_ready()  # a repeated handshake doesn't resend or re-run
    assert ended == [[("error", "boom")]]
    assert rtvi.sent == [("error", "boom")]
