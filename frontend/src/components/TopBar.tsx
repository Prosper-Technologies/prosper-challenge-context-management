// Top bar: the agents/catalog switch, agent identity, save (with an
// unsaved-changes dot), undo/redo, the validation badge, import/export, the
// canvas/JSON toggle and the test call button. The editor's controls show only on
// the agents screen; the test call button shows on both, since the call outlives
// the switch. The keyboard shortcuts are in App (useShortcuts).

import { useRef, useState, useSyncExternalStore } from "react";
import { hasPendingBuffers, subscribeBuffers } from "../editor/buffers";
import { exportAgent, importAgent, saveAgent } from "../editor/session";
import { shortcutLabel } from "../editor/shortcuts";
import { editorStore, isDirty, repairProblems, useEditor, useHistory } from "../editor/store";
import { issuesOf } from "../editor/validation";
import { Button, IssueList } from "./fields";

export type Screen = "agents" | "catalog";

export default function TopBar({
  screen,
  onScreen,
  onTestCall,
}: {
  screen: Screen;
  onScreen: (screen: Screen) => void;
  onTestCall: () => void;
}) {
  const doc = useEditor((s) => s.doc);
  const agentId = useEditor((s) => s.agentId);
  const locked = useEditor((s) => s.locked);
  const switcher = (
    <div className="flex overflow-hidden rounded border border-slate-300 text-sm">
      {(["agents", "catalog"] as const).map((s) => (
        <button
          key={s}
          type="button"
          className={`px-3 py-1 ${screen === s ? "bg-slate-900 text-white" : "bg-white hover:bg-slate-50"}`}
          onClick={() => onScreen(s)}
        >
          {s === "agents" ? "Agents" : "Catalog"}
        </button>
      ))}
    </div>
  );

  return (
    <header className="flex items-center gap-3 border-b border-slate-200 bg-white px-4 py-2">
      {switcher}
      {screen === "agents" ? (
        <EditorControls />
      ) : (
        <div className="mr-auto min-w-0">
          <h1 className="truncate font-semibold">Clinic catalog</h1>
          <div className="font-mono text-[11px] text-slate-400">backend/data/catalog.json (read-only)</div>
        </div>
      )}
      {agentId && (screen === "agents" || locked) && (
        <button
          type="button"
          className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
          disabled={!doc}
          onClick={onTestCall}
          title={screen === "agents" ? undefined : "Show the call panel"}
        >
          {locked ? "In call…" : "Test call"}
        </button>
      )}
    </header>
  );
}

function EditorControls() {
  const doc = useEditor((s) => s.doc);
  const agentId = useEditor((s) => s.agentId);
  const saved = useEditor((s) => s.saved);
  // Uncommitted field text counts: Save commits it first.
  const pending = useSyncExternalStore(subscribeBuffers, hasPendingBuffers);
  const dirty = useEditor(isDirty) || pending;
  const locked = useEditor((s) => s.locked);
  const view = useEditor((s) => s.view);
  const { undo, redo, setView } = editorStore.getState();
  const { canUndo, canRedo } = useHistory();
  const forcedJson = repairProblems(doc).length > 0;

  return (
    <>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <h1 className="truncate font-semibold">{doc?.name || agentId || "Agent Builder"}</h1>
          {dirty && (
            <span title="Unsaved changes" className="h-2 w-2 shrink-0 rounded-full bg-amber-500" />
          )}
        </div>
        {agentId && (
          <div className="font-mono text-[11px] text-slate-400">
            {agentId}.json{saved ? "" : " (not saved yet)"}
          </div>
        )}
      </div>

      {agentId && (
        <>
          <Button
            tone="primary"
            disabled={!dirty}
            onClick={() => void saveAgent()}
            title={`Save (${shortcutLabel("save")})`}
          >
            Save
          </Button>
          <div className="flex">
            <Button
              tone="ghost"
              disabled={!canUndo || locked}
              onClick={undo}
              title={`Undo (${shortcutLabel("undo")})`}
            >
              ↶ Undo
            </Button>
            <Button
              tone="ghost"
              disabled={!canRedo || locked}
              onClick={redo}
              title={`Redo (${shortcutLabel("redo")})`}
            >
              ↷ Redo
            </Button>
          </div>
          <ValidationBadge />
        </>
      )}

      <div className="ml-auto flex">
        <ImportButton />
        {agentId && (
          <Button tone="ghost" onClick={() => void download()} title="Download the agent as JSON">
            Export
          </Button>
        )}
      </div>

      {agentId && (
        <div className="flex overflow-hidden rounded border border-slate-300 text-sm">
          {(["canvas", "json"] as const).map((v) => (
            <button
              key={v}
              type="button"
              disabled={v === "canvas" && forcedJson}
              className={`px-3 py-1 disabled:opacity-40 ${
                (forcedJson ? "json" : view) === v ? "bg-slate-900 text-white" : "bg-white"
              }`}
              onClick={() => setView(v)}
            >
              {v === "canvas" ? "Canvas" : "JSON"}
            </button>
          ))}
        </div>
      )}
    </>
  );
}

async function download() {
  const file = await exportAgent();
  if (!file) return;
  const url = URL.createObjectURL(new Blob([file.text], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = file.fileName;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url));
}

/** Pick a JSON file and open it as a new, unsaved agent. */
export function ImportButton({ tone = "ghost" }: { tone?: "ghost" | "default" }) {
  const input = useRef<HTMLInputElement>(null);
  const locked = useEditor((s) => s.locked);
  return (
    <>
      <Button
        tone={tone}
        disabled={locked}
        onClick={() => input.current?.click()}
        title="Open a JSON file as a new agent"
      >
        Import
      </Button>
      <input
        ref={input}
        type="file"
        accept=".json,application/json"
        hidden
        onChange={async (e) => {
          const file = e.target.files?.[0];
          e.target.value = ""; // so picking the same file again still fires
          if (file) await importAgent(file.name, await file.text());
        }}
      />
    </>
  );
}

function ValidationBadge() {
  const validation = useEditor((s) => s.validation);
  const [open, setOpen] = useState(false);
  const issues = issuesOf(validation);
  const errors = issues.filter((i) => i.level === "error").length;
  const warnings = issues.length - errors;

  let text: string;
  let tone: string;
  if (validation.status === "failed") {
    text = "Can't validate";
    tone = "bg-slate-100 text-slate-600";
  } else if (validation.status === "idle") {
    return null;
  } else if (errors) {
    text = `${errors} error${errors > 1 ? "s" : ""}${warnings ? `, ${warnings} warning${warnings > 1 ? "s" : ""}` : ""}`;
    tone = "bg-red-100 text-red-800";
  } else if (warnings) {
    text = `${warnings} warning${warnings > 1 ? "s" : ""}`;
    tone = "bg-amber-100 text-amber-800";
  } else {
    text = "Ready to call";
    tone = "bg-emerald-100 text-emerald-800";
  }
  const pending = validation.status === "pending";

  return (
    <div className="relative">
      <button
        type="button"
        className={`rounded-full px-3 py-1 text-xs font-medium ${tone} ${pending ? "opacity-60" : ""}`}
        title={validation.status === "failed" ? validation.error : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        {text}
      </button>
      {open && issues.length > 0 && (
        <div className="absolute top-full left-0 z-20 mt-1 max-h-96 w-96 overflow-y-auto rounded border border-slate-200 bg-white p-2 shadow-lg">
          <IssueList issues={issues} />
        </div>
      )}
    </div>
  );
}
