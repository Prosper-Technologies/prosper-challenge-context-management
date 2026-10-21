// The builder: agent list | canvas (or JSON view) | inspector | test call.
//
// The document lives in the editor store (editor/store.ts); this shell wires up
// loading (and the backend-down and no-agents states), live validation, keyboard
// shortcuts and the test call, whose lock and node trace flow back into the store.

import { ReactFlowProvider } from "@xyflow/react";
import { type ReactNode, useEffect, useState } from "react";
import * as api from "./api";
import AgentList from "./components/AgentList";
import CallPanel, { useCallLock } from "./components/CallPanel";
import Canvas from "./components/Canvas";
import Inspector from "./components/Inspector";
import JsonView from "./components/JsonView";
import { Button } from "./components/fields";
import TopBar, { ImportButton } from "./components/TopBar";
import {
  type CallRefusal,
  newAgent,
  openAgent,
  refreshAgents,
  saveAgent,
  startTestCall,
} from "./editor/session";
import { isTextField, shortcutFor } from "./editor/shortcuts";
import { editorStore, repairProblems, useEditor } from "./editor/store";
import { createValidator } from "./editor/validation";
import { useTestCall } from "./testCall";

const DEFAULT_AGENT = "example";

function useLiveValidation() {
  useEffect(() => {
    const validator = createValidator({
      request: api.validateAgent,
      onResult: (v) => editorStore.getState().setValidation(v),
    });
    validator.schedule(editorStore.getState().doc);
    const unsubscribe = editorStore.subscribe((state, prev) => {
      if (state.doc !== prev.doc) validator.schedule(state.doc);
    });
    return () => {
      unsubscribe();
      validator.dispose();
    };
  }, []);
}

function useShortcuts() {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const shortcut = shortcutFor(e, isTextField(e.target));
      if (!shortcut) return;
      e.preventDefault(); // e.g. the browser's own "Save page"
      const { agentId, undo, redo } = editorStore.getState();
      if (shortcut === "save") {
        if (agentId) void saveAgent();
      } else if (shortcut === "undo") undo();
      else redo();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}

/** Load the agent list and open the default agent. Until the backend answers
 * (`make dev` starts Vite before Python is up), retry every few seconds. */
function useInitialLoad(): { error: string | null; retry: () => void } {
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const failed = await refreshAgents();
      if (cancelled) return;
      setError(failed);
      if (failed) return;
      editorStore.getState().notify(null);
      const { agents, agentId } = editorStore.getState();
      const first = agents.find((a) => a.id === DEFAULT_AGENT) ?? agents[0];
      if (first && !agentId) await openAgent(first.id, { confirm: false });
    })();
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  useEffect(() => {
    if (!error) return;
    const timer = setTimeout(() => setAttempt((n) => n + 1), 3000);
    return () => clearTimeout(timer);
  }, [error, attempt]);

  return { error, retry: () => setAttempt((n) => n + 1) };
}

export default function App() {
  const call = useTestCall();
  const [panelOpen, setPanelOpen] = useState(false);
  const [refusal, setRefusal] = useState<CallRefusal | null>(null);
  const [starting, setStarting] = useState(false);

  const agentId = useEditor((s) => s.agentId);
  const doc = useEditor((s) => s.doc);
  const view = useEditor((s) => s.view);
  const notice = useEditor((s) => s.notice);
  const showCanvas = view === "canvas" && repairProblems(doc).length === 0;

  useLiveValidation();
  useCallLock(call);
  useShortcuts();
  const load = useInitialLoad();

  // Info notices clear themselves; errors stay until dismissed or replaced.
  useEffect(() => {
    if (notice?.kind !== "info") return;
    const timer = setTimeout(() => editorStore.getState().notify(null), 6000);
    return () => clearTimeout(timer);
  }, [notice]);

  const start = async () => {
    setPanelOpen(true);
    setRefusal(null);
    setStarting(true);
    setRefusal(await startTestCall(call.start));
    setStarting(false);
  };

  return (
    <ReactFlowProvider>
      <div className="flex h-screen flex-col bg-slate-50 text-slate-900">
        <TopBar onTestCall={() => (call.status === "idle" ? void start() : setPanelOpen(true))} />
        {notice && !load.error && (
          <div
            role={notice.kind === "error" ? "alert" : "status"}
            className={`flex items-start gap-2 px-4 py-1.5 text-sm ${
              notice.kind === "error" ? "bg-red-50 text-red-800" : "bg-emerald-50 text-emerald-800"
            }`}
          >
            <span className="flex-1">{notice.text}</span>
            <button
              type="button"
              title="Dismiss"
              className="opacity-60 hover:opacity-100"
              onClick={() => editorStore.getState().notify(null)}
            >
              ✕
            </button>
          </div>
        )}
        <div className="flex min-h-0 flex-1">
          <AgentList />
          <main className="relative min-w-0 flex-1">
            {load.error && !agentId ? (
              <BackendDown error={load.error} onRetry={load.retry} />
            ) : !agentId ? (
              <EmptyState />
            ) : showCanvas ? (
              <Canvas key={agentId} />
            ) : (
              <JsonView key={agentId} />
            )}
          </main>
          {doc && (
            <aside className="w-80 shrink-0 overflow-y-auto border-l border-slate-200 bg-white p-4">
              <Inspector />
            </aside>
          )}
          {panelOpen && (
            <CallPanel
              call={call}
              refusal={refusal}
              starting={starting}
              onStart={() => void start()}
              onClose={() => setPanelOpen(false)}
            />
          )}
        </div>
        <audio ref={call.audioRef} autoPlay hidden />
      </div>
    </ReactFlowProvider>
  );
}

function Centered({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="flex max-w-md flex-col items-center gap-3 text-center text-sm text-slate-600">
        {children}
      </div>
    </div>
  );
}

function EmptyState() {
  const hasAgents = useEditor((s) => s.agents.length > 0);
  return (
    <Centered>
      <p className="font-medium text-slate-800">
        {hasAgents ? "No agent open" : "No saved agents yet"}
      </p>
      <p>
        {hasAgents
          ? "Pick an agent on the left, start a new one, or import a JSON file."
          : "Start a new agent, or import one from a JSON file (for example an exported agent)."}
      </p>
      <div className="flex gap-2">
        <Button tone="primary" onClick={() => newAgent("New agent")}>
          + New agent
        </Button>
        <ImportButton tone="default" />
      </div>
    </Centered>
  );
}

function BackendDown({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <Centered>
      <p className="font-medium text-slate-800">Can't reach the backend</p>
      <p>
        Start it with <code className="rounded bg-slate-100 px-1">make dev</code> (or{" "}
        <code className="rounded bg-slate-100 px-1">make run</code> alongside{" "}
        <code className="rounded bg-slate-100 px-1">make web</code>). This page retries on its
        own.
      </p>
      <p className="font-mono text-xs break-all text-slate-400">{error}</p>
      <Button onClick={onRetry}>Retry now</Button>
    </Centered>
  );
}
