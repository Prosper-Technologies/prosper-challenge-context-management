// The builder: agent list | canvas (or JSON view) | inspector | test call.
//
// The document lives in the editor store (editor/store.ts); this shell wires up
// loading, live validation and the test call, whose lock and node trace flow
// back into the store.

import { ReactFlowProvider } from "@xyflow/react";
import { useEffect, useState } from "react";
import * as api from "./api";
import AgentList from "./components/AgentList";
import CallPanel, { useCallLock } from "./components/CallPanel";
import Canvas from "./components/Canvas";
import Inspector from "./components/Inspector";
import JsonView from "./components/JsonView";
import TopBar from "./components/TopBar";
import { type CallRefusal, openAgent, refreshAgents, startTestCall } from "./editor/session";
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

  useEffect(() => {
    void (async () => {
      await refreshAgents();
      const { agents } = editorStore.getState();
      const first = agents.find((a) => a.id === DEFAULT_AGENT) ?? agents[0];
      if (first) await openAgent(first.id, { confirm: false });
    })();
  }, []);

  // Notices clear themselves.
  useEffect(() => {
    if (!notice) return;
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
        {notice && (
          <div
            className={`px-4 py-1.5 text-sm ${
              notice.kind === "error" ? "bg-red-50 text-red-800" : "bg-emerald-50 text-emerald-800"
            }`}
          >
            {notice.text}
          </div>
        )}
        <div className="flex min-h-0 flex-1">
          <AgentList />
          <main className="relative min-w-0 flex-1">
            {!agentId ? (
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

function EmptyState() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-slate-500">
      Pick an agent on the left, or create one with “+ New”.
    </div>
  );
}
