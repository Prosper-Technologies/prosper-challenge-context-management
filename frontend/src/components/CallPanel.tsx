// The test-call drawer: controls, refusals, the path taken, collected state and
// the live transcript. The canvas highlights the active node and traced edges
// from the same `node_entered` events (via the store's trace).

import { useEffect, useRef } from "react";
import type { CallRefusal } from "../editor/session";
import { useEditor } from "../editor/store";
import type { NodeEntered, useTestCall } from "../testCall";
import { Button, IssueList } from "./fields";

export type Call = ReturnType<typeof useTestCall>;

/** Keep the editor locked until a started call is torn down, then release it,
 * and mirror the call's path onto the canvas. */
export function useCallLock(call: Call) {
  const setLocked = useEditor((s) => s.setLocked);
  const setTrace = useEditor((s) => s.setTrace);
  const active = useRef(false);

  useEffect(() => {
    if (call.status !== "idle") {
      active.current = true;
    } else if (active.current) {
      active.current = false;
      setLocked(false);
    }
  }, [call.status, setLocked]);

  useEffect(() => {
    setTrace(call.path, call.status !== "idle");
  }, [call.path, call.status, setTrace]);
}

export default function CallPanel({
  call,
  refusal,
  starting,
  onStart,
  onClose,
}: {
  call: Call;
  refusal: CallRefusal | null;
  starting: boolean;
  onStart: () => void;
  onClose: () => void;
}) {
  const label = {
    idle: call.outcome ?? "Ready: the current draft is called, unsaved edits included.",
    connecting: "Connecting…",
    connected: "Connected",
    disconnecting: "Hanging up…",
  }[call.status];

  return (
    <aside className="flex w-80 shrink-0 flex-col gap-3 overflow-y-auto border-l border-slate-200 bg-white p-3">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold">Test call</h2>
        <Button tone="ghost" disabled={call.status !== "idle" || starting} onClick={onClose}>
          Close
        </Button>
      </div>

      <div className="flex items-center gap-2">
        {call.status === "idle" ? (
          <button
            type="button"
            className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
            disabled={starting}
            onClick={onStart}
          >
            {starting ? "Checking…" : call.outcome ? "Call again" : "Start call"}
          </button>
        ) : (
          <button
            type="button"
            className="rounded bg-red-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
            disabled={call.status === "disconnecting"}
            onClick={() => void call.hangUp()}
          >
            Hang up
          </button>
        )}
        {call.status === "connected" && (
          <button
            type="button"
            className="ml-auto flex items-center gap-2 rounded border border-slate-300 px-2 py-1 text-sm"
            onClick={call.toggleMic}
          >
            <span
              className={`h-2 w-2 rounded-full ${
                !call.micEnabled
                  ? "bg-slate-400"
                  : call.userSpeaking
                    ? "bg-emerald-500"
                    : "bg-emerald-300"
              }`}
            />
            {call.micEnabled ? "Mic on" : "Muted"}
          </button>
        )}
      </div>
      <p className="text-xs text-slate-500">{label}</p>
      {call.status !== "idle" && (
        <p className="rounded bg-amber-50 px-2 py-1 text-xs text-amber-800">
          Editing is locked until the call ends; you can still select and inspect.
        </p>
      )}

      {refusal && (
        <div className="flex flex-col gap-1">
          <p className="text-sm text-red-700">{refusal.error}</p>
          {refusal.issues && <IssueList issues={refusal.issues} />}
        </div>
      )}
      {call.error && (
        <p className="rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">
          {call.error}
        </p>
      )}

      <PathTaken path={call.path} />
      <CollectedState state={call.collected} />
      <Transcript call={call} />
    </aside>
  );
}

function PathTaken({ path }: { path: NodeEntered[] }) {
  if (path.length === 0) return null;
  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Path</h3>
      <ol className="flex flex-wrap items-center gap-1 text-xs text-slate-600">
        {path.map((step, i) => (
          <li key={i} className="flex items-center gap-1">
            {step.via && <span className="font-mono text-slate-400">—{step.via}→</span>}
            <span className="font-medium text-slate-800">{step.node}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function CollectedState({ state }: { state: Record<string, unknown> }) {
  const entries = Object.entries(state);
  if (entries.length === 0) return null;
  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Collected
      </h3>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
        {entries.map(([key, value]) => (
          <div key={key} className="contents">
            <dt className="font-mono text-xs text-slate-500">{key}</dt>
            <dd className="break-words">{typeof value === "string" ? value : JSON.stringify(value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Transcript({ call }: { call: Call }) {
  if (call.transcript.length === 0 && !call.interim) return null;
  return (
    <div className="flex min-h-0 flex-col">
      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Transcript
      </h3>
      <ul className="flex flex-col gap-1 text-sm">
        {call.transcript.map((entry, i) =>
          entry.role === "function" ? (
            <li key={i} className="font-mono text-xs break-words text-slate-500">
              → {entry.text}
            </li>
          ) : (
            <li key={i}>
              <span className="mr-2 font-medium text-slate-500">
                {entry.role === "user" ? "Caller" : "Agent"}
              </span>
              {entry.text}
            </li>
          ),
        )}
        {call.interim && (
          <li className="italic text-slate-400">
            <span className="mr-2 font-medium">Caller</span>
            {call.interim}
          </li>
        )}
      </ul>
    </div>
  );
}
