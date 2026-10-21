// Test call: one PipecatClient per call over SmallWebRTC, driven through the
// Vite proxy (/start, then /sessions/{id}/api/offer). The agent is sent inline
// as a snapshot (`body.agent`), so unsaved drafts can be tested.
//
// Each call gets a fresh client; events from a previous client are ignored, and
// every exit path (hang-up, failed start, server disconnect) releases the mic and
// connection so another call can start.

import { type APIRequest, PipecatClient, type RTVIMessage } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import { useCallback, useEffect, useReducer, useRef } from "react";
import type { AgentConfig } from "./generated/contract";

export type CallStatus = "idle" | "connecting" | "connected" | "disconnecting";

/** Sent by the backend (agent_builder/runtime.py) after a node is activated. */
export interface NodeEntered {
  type: "node_entered";
  from: string | null;
  node: string;
  via: string | null;
  state: Record<string, unknown>;
}

/** Sent when a tool changed the state while the node stayed active. Not a path step. */
export interface StateUpdated {
  type: "state_updated";
  node: string | null;
  via: string;
  state: Record<string, unknown>;
}

export interface TranscriptEntry {
  role: "user" | "bot" | "function";
  text: string;
  open: boolean; // bot output still being appended to this turn
}

interface CallState {
  status: CallStatus;
  outcome: string | null; // how the last call ended
  error: string | null;
  transcript: TranscriptEntry[];
  interim: string; // the user's in-progress (non-final) transcript
  path: NodeEntered[];
  collected: Record<string, unknown>; // the latest state from either event
  spokenSegments: number[]; // bot-output segment ids already in the transcript
  micEnabled: boolean;
  userSpeaking: boolean;
}

type Action =
  | { type: "start" }
  | { type: "connected" }
  | { type: "disconnecting" }
  | { type: "ended"; outcome: string }
  | { type: "error"; message: string }
  | { type: "user_transcript"; text: string; final: boolean }
  | { type: "bot_output"; text: string; segment?: number }
  | { type: "bot_stopped" }
  | { type: "function_call"; text: string }
  | { type: "node_entered"; event: NodeEntered }
  | { type: "state_updated"; event: StateUpdated }
  | { type: "mic"; enabled: boolean }
  | { type: "user_speaking"; speaking: boolean };

const initialState: CallState = {
  status: "idle",
  outcome: null,
  error: null,
  transcript: [],
  interim: "",
  path: [],
  collected: {},
  spokenSegments: [],
  micEnabled: true,
  userSpeaking: false,
};

function closeBotTurn(transcript: TranscriptEntry[]): TranscriptEntry[] {
  const last = transcript.at(-1);
  if (last?.role !== "bot" || !last.open) return transcript;
  return [...transcript.slice(0, -1), { ...last, open: false }];
}

function reducer(state: CallState, action: Action): CallState {
  switch (action.type) {
    case "start":
      return { ...initialState, status: "connecting" };
    case "connected":
      return { ...state, status: "connected" };
    case "disconnecting":
      return { ...state, status: "disconnecting" };
    case "ended":
      return {
        ...state,
        status: "idle",
        outcome: state.error ? "Call failed" : action.outcome,
        interim: "",
        userSpeaking: false,
        transcript: closeBotTurn(state.transcript),
      };
    case "error":
      return { ...state, error: action.message };
    case "user_transcript":
      if (!action.final) return { ...state, interim: action.text };
      return {
        ...state,
        interim: "",
        transcript: [
          ...closeBotTurn(state.transcript),
          { role: "user", text: action.text, open: false },
        ],
      };
    case "bot_output": {
      // A sentence is re-sent with the same segment id as its words are spoken.
      if (action.segment !== undefined) {
        if (state.spokenSegments.includes(action.segment)) return state;
        state = { ...state, spokenSegments: [...state.spokenSegments, action.segment] };
      }
      const last = state.transcript.at(-1);
      if (last?.role === "bot" && last.open) {
        const text = `${last.text} ${action.text}`.trim();
        return { ...state, transcript: [...state.transcript.slice(0, -1), { ...last, text }] };
      }
      return {
        ...state,
        transcript: [...state.transcript, { role: "bot", text: action.text, open: true }],
      };
    }
    case "bot_stopped":
      return { ...state, transcript: closeBotTurn(state.transcript) };
    case "function_call":
      return {
        ...state,
        transcript: [
          ...closeBotTurn(state.transcript),
          { role: "function", text: action.text, open: false },
        ],
      };
    case "node_entered":
      return { ...state, path: [...state.path, action.event], collected: action.event.state };
    case "state_updated":
      return { ...state, collected: action.event.state };
    case "mic":
      return { ...state, micEnabled: action.enabled };
    case "user_speaking":
      return { ...state, userSpeaking: action.speaking };
  }
}

function errorText(message: RTVIMessage): string {
  const data = message.data as { error?: string; message?: string } | undefined;
  return data?.error ?? data?.message ?? "Unknown error";
}

export function useTestCall() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const clientRef = useRef<PipecatClient | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // Release one client's resources (mic, peer connection). Safe to call twice.
  const release = useCallback(async (client: PipecatClient, outcome: string) => {
    if (clientRef.current !== client) return;
    clientRef.current = null;
    try {
      await client.disconnect();
    } catch {
      // Already disconnected; nothing left to release.
    }
    if (audioRef.current) audioRef.current.srcObject = null;
    dispatch({ type: "ended", outcome });
  }, []);

  const start = useCallback(
    async (agent: AgentConfig) => {
      if (clientRef.current) return;
      dispatch({ type: "start" });

      let client: PipecatClient;
      // Ignore events from a client that is no longer the current call.
      const guard =
        <A extends unknown[]>(fn: (...args: A) => void) =>
        (...args: A) => {
          if (clientRef.current === client) fn(...args);
        };

      client = new PipecatClient({
        transport: new SmallWebRTCTransport(),
        enableMic: true,
        enableCam: false,
        callbacks: {
          onError: guard((message) => dispatch({ type: "error", message: errorText(message) })),
          onDisconnected: guard(() => void release(client, "Call ended")),
          onTrackStarted: guard((track, participant) => {
            if (participant?.local || track.kind !== "audio" || !audioRef.current) return;
            audioRef.current.srcObject = new MediaStream([track]);
            void audioRef.current.play().catch(() => undefined);
          }),
          onServerMessage: guard((data) => {
            if (data?.type === "node_entered")
              dispatch({ type: "node_entered", event: data as NodeEntered });
            else if (data?.type === "state_updated")
              dispatch({ type: "state_updated", event: data as StateUpdated });
          }),
          onUserTranscript: guard((data) =>
            dispatch({ type: "user_transcript", text: data.text, final: data.final }),
          ),
          onBotOutput: guard((data) => {
            // Word-level events repeat the sentence-level text as it is spoken.
            if (data.aggregated_by !== "word")
              dispatch({ type: "bot_output", text: data.text, segment: data.segment_id });
          }),
          onBotStoppedSpeaking: guard(() => dispatch({ type: "bot_stopped" })),
          onUserStartedSpeaking: guard(() => dispatch({ type: "user_speaking", speaking: true })),
          onUserStoppedSpeaking: guard(() => dispatch({ type: "user_speaking", speaking: false })),
          onLLMFunctionCallInProgress: guard((data) =>
            dispatch({
              type: "function_call",
              text: `${data.function_name ?? "function"}(${JSON.stringify(data.arguments ?? {})})`,
            }),
          ),
        },
      });
      clientRef.current = client;

      try {
        await client.startBotAndConnect({
          endpoint: "/start",
          // A parsed JSON document; its generated type just doesn't say so.
          requestData: { body: { agent } } as APIRequest["requestData"],
        });
        if (clientRef.current === client) {
          dispatch({ type: "connected" });
          dispatch({ type: "mic", enabled: client.isMicEnabled });
        }
      } catch (e) {
        if (clientRef.current === client) {
          dispatch({ type: "error", message: e instanceof Error ? e.message : String(e) });
          await release(client, "Call failed");
        }
      }
    },
    [release],
  );

  const hangUp = useCallback(async () => {
    const client = clientRef.current;
    if (!client) return;
    dispatch({ type: "disconnecting" });
    await release(client, "Call ended");
  }, [release]);

  const toggleMic = useCallback(() => {
    const client = clientRef.current;
    if (!client) return;
    client.enableMic(!client.isMicEnabled);
    dispatch({ type: "mic", enabled: client.isMicEnabled });
  }, []);

  // Leaving the page mid-call still releases the mic and connection.
  useEffect(() => {
    return () => {
      const client = clientRef.current;
      if (client) void release(client, "Call ended");
    };
  }, [release]);

  return { ...state, start, hangUp, toggleMic, audioRef };
}
