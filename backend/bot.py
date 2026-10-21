#
# Voice pipeline — Prosper AI Software Engineer Challenge
#
# The runnable voice agent: WebRTC transport + ElevenLabs STT/TTS + OpenAI LLM,
# driven by a Pipecat Flows node graph. This file is generic — it loads an agent
# definition (JSON) via AgentBuilder and runs it. Swapping the agent is a data
# change (edit/replace the JSON), not a code change.
#
#   /start body.agent (inline draft) | body.agent_id (saved, agents/) | agents/example.json
#       ->  AgentBuilder  ->  Pipecat Flows graph  ->  TrackingFlowManager
#
# Each call picks its agent from the runner's per-call body, so the builder UI can
# test an unsaved draft (or any saved agent) with no restart. Node activations are
# streamed to the client as RTVI server messages (`node_entered`).
#
# Run:  python bot.py   then open http://localhost:7860/client (prebuilt UI) or
#       the builder frontend (frontend/, `npm run dev`)
#

import os
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.frames.frames import EndFrame
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from agent_builder import AgentBuilder
from agent_builder.runtime import ClientReadyGate, TrackingFlowManager
from api import AgentSelectionError, install as install_api, select_agent

# Load .env next to this file, so the bot runs the same from the repo root or backend/.
load_dotenv(Path(__file__).parent / ".env", override=True)


transport_params = {
    "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
}


async def run_bot(
    transport: BaseTransport, runner_args: RunnerArguments, builder: AgentBuilder
) -> None:
    config = builder.config
    logger.info(f"Starting '{config.name}' with {len(config.nodes)} nodes")

    stt = ElevenLabsRealtimeSTTService(api_key=os.environ["ELEVENLABS_API_KEY"])
    tts = ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        settings=ElevenLabsTTSService.Settings(voice=config.voice_id),
    )
    llm = OpenAILLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        settings=OpenAILLMService.Settings(model=config.model),
    )

    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    # Server messages wait for the RTVI client-ready handshake, which may finish
    # before or after the flow starts.
    rtvi_gate = ClientReadyGate(worker.rtvi)

    async def fail_call(reason: str) -> None:
        """Tell the client why the call failed, then end it once that is delivered."""
        logger.error(reason)
        await rtvi_gate.send_error(reason)
        await rtvi_gate.after_delivery(lambda: worker.queue_frame(EndFrame()))

    async def on_transition_failed(error: Exception) -> None:
        await fail_call(f"Node transition failed: {error}")

    flow_manager = TrackingFlowManager(
        llm=llm,
        context_aggregator=context_aggregator,
        worker=worker,
        transport=transport,
        on_node_entered=rtvi_gate.send_server_message,
        on_transition_failed=on_transition_failed,
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected — starting flow at initial node")
        try:
            await flow_manager.initialize(builder.build_initial_node())
        except Exception as e:
            await fail_call(f"Failed to start the agent: {e}")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Entry point invoked by the Pipecat dev runner (and Pipecat Cloud)."""
    # Compile the agent before creating the transport or any service. /start already
    # rejects invalid selections; this guards direct /api/offer calls.
    try:
        builder = select_agent(runner_args.body)
    except AgentSelectionError as e:
        logger.error(f"Not starting call: {e}")
        await _disconnect(runner_args)
        return

    try:
        transport = await create_transport(runner_args, transport_params)
        await run_bot(transport, runner_args, builder)
    except Exception:
        # e.g. a missing API key: release the peer connection so the client
        # sees the call end and can start another.
        logger.exception("Call failed")
        await _disconnect(runner_args)


async def _disconnect(runner_args: RunnerArguments) -> None:
    connection = getattr(runner_args, "webrtc_connection", None)
    if connection is not None:
        await connection.disconnect()


if __name__ == "__main__":
    from pipecat.runner.run import app, main

    install_api(app)
    main()
