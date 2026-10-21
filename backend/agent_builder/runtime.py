#
# Runtime adapter — reports confirmed node activations to the browser.
#
#   FlowManager._set_node  ->  activation confirmed  ->  node_entered  ->  RTVI client
#
# Pipecat Flows has no public node-change callback, and an edge handler returning
# (result, next_node) only *proposes* a transition. So TrackingFlowManager hooks the
# point inside _set_node where the node is active but its post-actions (e.g.
# end_conversation) have not run yet. Verified against pipecat-ai-flows 1.4.0; the
# integration tests in tests/test_runtime.py pin this behaviour.
#

import json
from typing import Any, Awaitable, Callable, Optional

from loguru import logger
from pipecat_flows import FlowManager

NodeEnteredCallback = Callable[[dict], Awaitable[None]]
TransitionFailedCallback = Callable[[Exception], Awaitable[None]]


def _json_snapshot(state: dict) -> dict:
    """A JSON-safe copy of the flow state, so later mutation can't leak into a sent event."""
    return json.loads(json.dumps(state, default=str))


class TrackingFlowManager(FlowManager):
    """A FlowManager that reports each node it actually activates.

    `on_node_entered` receives `{type: "node_entered", from, node, via, state}`:
    `from` is the previous node and `via` the function whose transition led here
    (both None for the initial node). It is called once per activation, after the
    node's context and tools are in place and before its post-actions run, so a
    terminal node is reported before end_conversation shuts the call down. A node
    whose activation fails is never reported.
    """

    def __init__(
        self,
        *,
        on_node_entered: NodeEnteredCallback,
        on_transition_failed: Optional[TransitionFailedCallback] = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self._on_node_entered = on_node_entered
        self._on_transition_failed = on_transition_failed
        self._via: Optional[str] = None
        self._entering: Optional[dict] = None  # the activation in progress, until reported

    async def _execute_transition(self, transition_info: dict[str, Any]) -> None:
        self._via = transition_info.get("function_name")
        try:
            await super()._execute_transition(transition_info)
        except Exception as e:
            if self._on_transition_failed is None:
                raise
            await self._on_transition_failed(e)
        finally:
            self._via = None

    async def _set_node(self, node_id: str, node_config) -> None:
        self._entering = {"from": self._current_node, "node": node_id, "via": self._via}
        try:
            await super()._set_node(node_id, node_config)
            # Nodes without post-actions are reported here, once activation returned.
            await self._report_entry()
        finally:
            self._entering = None

    async def _execute_actions(self, pre_actions=None, post_actions=None) -> None:
        # _set_node runs pre-actions before activation and post-actions after it
        # (as separate calls), so reaching post-actions confirms the node is active.
        if pre_actions:
            await super()._execute_actions(pre_actions=pre_actions)
        if post_actions:
            await self._report_entry()
            await super()._execute_actions(post_actions=post_actions)

    async def _report_entry(self) -> None:
        entry, self._entering = self._entering, None
        if entry is None:
            return
        await self._on_node_entered(
            {"type": "node_entered", **entry, "state": _json_snapshot(self.state)}
        )


class ClientReadyGate:
    """Holds server messages until the RTVI client is ready, then sends them in order.

    Flow initialization (on transport connect) and the RTVI client-ready handshake
    can complete in either order; messages sent before the client is ready would be
    lost, so they are queued here instead.
    """

    def __init__(self, rtvi):
        self._rtvi = rtvi
        self._ready = False
        self._pending: list[tuple[str, Any]] = []
        self._on_flushed: list[Callable[[], Awaitable[None]]] = []
        rtvi.add_event_handler("on_client_ready", self._handle_client_ready)

    @property
    def ready(self) -> bool:
        return self._ready

    async def send_server_message(self, data: dict) -> None:
        await self._send("server_message", data)

    async def send_error(self, message: str) -> None:
        await self._send("error", message)

    async def after_delivery(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run `callback` once everything queued so far has been sent."""
        if self._ready:
            await callback()
        else:
            self._on_flushed.append(callback)

    async def _send(self, kind: str, payload: Any) -> None:
        if self._ready:
            await self._deliver(kind, payload)
        else:
            self._pending.append((kind, payload))

    async def _deliver(self, kind: str, payload: Any) -> None:
        if kind == "error":
            await self._rtvi.send_error(payload)
        else:
            await self._rtvi.send_server_message(payload)

    async def _handle_client_ready(self, rtvi) -> None:
        if self._ready:
            return
        self._ready = True
        pending, self._pending = self._pending, []
        for kind, payload in pending:
            await self._deliver(kind, payload)
        callbacks, self._on_flushed = self._on_flushed, []
        for callback in callbacks:
            try:
                await callback()
            except Exception as e:
                logger.error(f"Post-delivery callback failed: {e}")
