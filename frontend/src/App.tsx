// M1 integration page: edit the example agent as raw JSON and test-call the
// unsaved draft. The graph editor (M3) replaces the textarea; the call panel's
// behaviour (snapshot, lock, transcript, node tracking, cleanup) carries over.

import { useEffect, useMemo, useState } from "react";
import { type AgentSnapshot, type NodeEntered, useTestCall } from "./testCall";

type Parsed = { agent: AgentSnapshot; error: null } | { agent: null; error: string };

function parseDraft(text: string): Parsed {
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value !== "object" || value === null || Array.isArray(value))
      return { agent: null, error: "The agent must be a JSON object." };
    return { agent: value as AgentSnapshot, error: null };
  } catch (e) {
    return { agent: null, error: e instanceof Error ? e.message : String(e) };
  }
}

interface NodeSummary {
  name: string;
  end: boolean;
}

function nodesOf(agent: AgentSnapshot | null): NodeSummary[] {
  const nodes = agent?.nodes;
  if (!Array.isArray(nodes)) return [];
  return nodes.flatMap((n) =>
    n && typeof n === "object" && !Array.isArray(n) && typeof n.name === "string"
      ? [{ name: n.name, end: n.end === true }]
      : [],
  );
}

export default function App() {
  const call = useTestCall();
  const [draft, setDraft] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  // The exact document the running call uses; the panel describes this, not the draft.
  const [snapshot, setSnapshot] = useState<AgentSnapshot | null>(null);

  const loadExample = async () => {
    setLoadError(null);
    try {
      const res = await fetch("/api/agents/example");
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      setDraft(JSON.stringify(await res.json(), null, 2));
    } catch (e) {
      setLoadError(`Could not load the example agent (is the backend running?): ${e}`);
    }
  };

  useEffect(() => {
    void loadExample();
  }, []);

  const parsed = useMemo(() => parseDraft(draft), [draft]);
  const locked = call.status !== "idle";

  const startCall = () => {
    if (!parsed.agent || locked) return;
    const agent = structuredClone(parsed.agent);
    setSnapshot(agent);
    void call.start(agent);
  };

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white px-6 py-3">
        <h1 className="text-lg font-semibold">Agent Builder</h1>
        <p className="text-sm text-slate-500">
          Edit the agent draft and test it with a call. Nothing is saved.
        </p>
      </header>

      <main className="grid gap-6 p-6 lg:grid-cols-2">
        <section className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <h2 className="font-medium">Agent draft (JSON)</h2>
            <div className="flex items-center gap-3">
              {locked && (
                <span className="rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800">
                  Locked during call
                </span>
              )}
              <button
                className="text-sm text-slate-600 underline disabled:opacity-40"
                disabled={locked}
                onClick={() => void loadExample()}
              >
                Reload example
              </button>
            </div>
          </div>
          <textarea
            className="h-[70vh] w-full rounded border border-slate-300 bg-white p-3 font-mono text-xs read-only:bg-slate-100"
            spellCheck={false}
            readOnly={locked}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          {loadError && <p className="text-sm text-red-700">{loadError}</p>}
          {draft && parsed.error && (
            <p className="text-sm text-red-700">Invalid JSON: {parsed.error}</p>
          )}
        </section>

        <section className="flex flex-col gap-4">
          <CallControls call={call} canStart={!!parsed.agent} onStart={startCall} />
          {call.error && (
            <p className="rounded border border-red-200 bg-red-50 p-2 text-sm text-red-800">
              {call.error}
            </p>
          )}
          {snapshot && <NodeTrack nodes={nodesOf(snapshot)} path={call.path} live={locked} />}
          <CollectedState path={call.path} />
          <Transcript call={call} />
        </section>
      </main>
      <audio ref={call.audioRef} autoPlay hidden />
    </div>
  );
}

type Call = ReturnType<typeof useTestCall>;

function CallControls({
  call,
  canStart,
  onStart,
}: {
  call: Call;
  canStart: boolean;
  onStart: () => void;
}) {
  const label = {
    idle: call.outcome ?? "Ready",
    connecting: "Connecting…",
    connected: "Connected",
    disconnecting: "Hanging up…",
  }[call.status];

  return (
    <div className="flex items-center gap-3 rounded border border-slate-200 bg-white p-3">
      {call.status === "idle" ? (
        <button
          className="rounded bg-emerald-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          disabled={!canStart}
          onClick={onStart}
        >
          Test call
        </button>
      ) : (
        <button
          className="rounded bg-red-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          disabled={call.status === "disconnecting"}
          onClick={() => void call.hangUp()}
        >
          Hang up
        </button>
      )}
      <span className="text-sm text-slate-600">{label}</span>
      {call.status === "connected" && (
        <button
          className="ml-auto flex items-center gap-2 rounded border border-slate-300 px-3 py-1 text-sm"
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
          {call.micEnabled ? "Mic on" : "Mic muted"}
        </button>
      )}
    </div>
  );
}

function NodeTrack({
  nodes,
  path,
  live,
}: {
  nodes: NodeSummary[];
  path: NodeEntered[];
  live: boolean;
}) {
  const active = live ? path.at(-1)?.node : undefined;
  const visited = new Set(path.map((p) => p.node));

  return (
    <div className="rounded border border-slate-200 bg-white p-3">
      <h3 className="mb-2 text-sm font-medium">Nodes</h3>
      <div className="flex flex-wrap gap-2">
        {nodes.map((n) => (
          <span
            key={n.name}
            className={`rounded-full border px-3 py-1 text-xs ${
              n.name === active
                ? "border-emerald-600 bg-emerald-600 text-white"
                : visited.has(n.name)
                  ? "border-emerald-300 bg-emerald-50"
                  : "border-slate-300"
            } ${n.end ? "border-dashed" : ""}`}
          >
            {n.name}
          </span>
        ))}
      </div>
      {path.length > 0 && (
        <ol className="mt-3 flex flex-wrap items-center gap-1 text-xs text-slate-600">
          {path.map((step, i) => (
            <li key={i} className="flex items-center gap-1">
              {step.via && <span className="text-slate-400">—{step.via}→</span>}
              <span className="font-medium text-slate-800">{step.node}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function CollectedState({ path }: { path: NodeEntered[] }) {
  const state = path.at(-1)?.state ?? {};
  const entries = Object.entries(state);
  if (entries.length === 0) return null;

  return (
    <div className="rounded border border-slate-200 bg-white p-3">
      <h3 className="mb-2 text-sm font-medium">Collected</h3>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        {entries.map(([key, value]) => (
          <div key={key} className="contents">
            <dt className="font-mono text-xs text-slate-500">{key}</dt>
            <dd>{typeof value === "string" ? value : JSON.stringify(value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Transcript({ call }: { call: Call }) {
  if (call.transcript.length === 0 && !call.interim) return null;

  return (
    <div className="rounded border border-slate-200 bg-white p-3">
      <h3 className="mb-2 text-sm font-medium">Transcript</h3>
      <ul className="flex max-h-[40vh] flex-col gap-1 overflow-y-auto text-sm">
        {call.transcript.map((entry, i) =>
          entry.role === "function" ? (
            <li key={i} className="font-mono text-xs text-slate-500">
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
