// The builder: agent list | canvas (or JSON view) | inspector | test call, and
// the catalog screen beside it.
//
// The document lives in the editor store (editor/store.ts); this shell wires up
// loading (and the backend-down and no-agents states), live validation, keyboard
// shortcuts and the test call, whose lock and node trace flow back into the store.
//
// The agents/catalog switch is local state here. The call, its audio and its
// panel sit above the switch, so a call carries on across it. The editor stays
// mounted (hidden and inert) while the catalog shows, so field buffers that
// haven't committed, or can't, are still there on the way back; its shortcuts and
// the canvas's Delete key only act on the agents screen. The catalog screen
// mounts on first visit and then stays, keeping its playground session.

import { ReactFlowProvider } from "@xyflow/react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import * as api from "./api";
import AgentList from "./components/AgentList";
import CallPanel, { useCallLock } from "./components/CallPanel";
import Canvas from "./components/Canvas";
import CatalogView from "./components/CatalogView";
import Inspector from "./components/Inspector";
import JsonView from "./components/JsonView";
import { Button } from "./components/fields";
import TopBar, { ImportButton, type Screen } from "./components/TopBar";
import {
  type CallRefusal,
  newAgent,
  openAgent,
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

/** Editor shortcuts, only while the editor is showing (`active`). */
function useShortcuts(active: boolean) {
  const enabled = useRef(active);
  enabled.current = active;
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!enabled.current) return;
      const shortcut = shortcutFor(e, isTextField(e.target));
      if (!shortcut) return;
      e.preventDefault(); // e.g. the browser's own "Save page"
      const { undo, redo } = editorStore.getState();
      if (shortcut === "save") void saveAgent();
      else if (shortcut === "undo") undo();
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
    let timer: ReturnType<typeof setTimeout> | undefined;
    void (async () => {
      let agents;
      try {
        agents = await api.listAgents();
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        timer = setTimeout(() => setAttempt((n) => n + 1), 3000);
        return;
      }
      if (cancelled) return;
      setError(null);
      editorStore.getState().setAgents(agents);
      const first = agents.find((a) => a.id === DEFAULT_AGENT) ?? agents[0];
      if (first && !editorStore.getState().agentId) await openAgent(first.id, { confirm: false });
    })();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [attempt]);

  return { error, retry: () => setAttempt((n) => n + 1) };
}

export default function App() {
  const call = useTestCall();
  const [panelOpen, setPanelOpen] = useState(false);
  const [refusal, setRefusal] = useState<CallRefusal | null>(null);
  const [starting, setStarting] = useState(false);
  const [screen, setScreen] = useState<Screen>("agents");
  const [catalogVisited, setCatalogVisited] = useState(false);
  const editing = screen === "agents";

  const agentId = useEditor((s) => s.agentId);
  const doc = useEditor((s) => s.doc);
  const view = useEditor((s) => s.view);
  const notice = useEditor((s) => s.notice);
  const showCanvas = view === "canvas" && repairProblems(doc).length === 0;

  useLiveValidation();
  useCallLock(call);
  useShortcuts(editing);
  const load = useInitialLoad();

  // Info notices clear themselves; errors stay until dismissed or replaced.
  useEffect(() => {
    if (notice?.kind !== "info") return;
    const timer = setTimeout(() => editorStore.getState().notify(null), 6000);
    return () => clearTimeout(timer);
  }, [notice]);

  const switchScreen = (next: Screen) => {
    if (next === "catalog") setCatalogVisited(true);
    setScreen(next);
  };

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
        <TopBar
          screen={screen}
          onScreen={switchScreen}
          onTestCall={() => (call.status === "idle" ? void start() : setPanelOpen(true))}
        />
        {notice && (
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
          <div className="relative min-w-0 flex-1">
            <div className={`absolute inset-0 flex ${editing ? "" : "invisible"}`} inert={!editing}>
              <AgentList />
              <main className="relative min-w-0 flex-1">
                {load.error && !agentId ? (
                  <BackendDown error={load.error} onRetry={load.retry} />
                ) : !agentId ? (
                  <EmptyState />
                ) : showCanvas ? (
                  <Canvas key={agentId} active={editing} />
                ) : (
                  <JsonView key={agentId} />
                )}
              </main>
              {doc && (
                <aside className="w-80 shrink-0 overflow-y-auto border-l border-slate-200 bg-white p-4">
                  <Inspector />
                </aside>
              )}
            </div>
            {catalogVisited && (
              <div className={`absolute inset-0 bg-slate-50 ${editing ? "invisible" : ""}`} inert={editing}>
                <CatalogView />
              </div>
            )}
          </div>
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
