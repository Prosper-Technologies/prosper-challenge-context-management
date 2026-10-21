// Top bar: agent identity, save (with an unsaved-changes dot), undo/redo, the
// validation badge, the canvas/JSON toggle and the test call button.

import { useState, useSyncExternalStore } from "react";
import { hasPendingBuffers, subscribeBuffers } from "../editor/buffers";
import { saveAgent } from "../editor/session";
import { editorStore, isDirty, repairProblems, useEditor, useHistory } from "../editor/store";
import { issuesOf } from "../editor/validation";
import { Button, IssueList } from "./fields";

export default function TopBar({ onTestCall }: { onTestCall: () => void }) {
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
    <header className="flex items-center gap-3 border-b border-slate-200 bg-white px-4 py-2">
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
          <Button tone="primary" disabled={!dirty} onClick={() => void saveAgent()}>
            Save
          </Button>
          <div className="flex">
            <Button tone="ghost" disabled={!canUndo || locked} onClick={undo} title="Undo">
              ↶ Undo
            </Button>
            <Button tone="ghost" disabled={!canRedo || locked} onClick={redo} title="Redo">
              ↷ Redo
            </Button>
          </div>
          <ValidationBadge />
          <div className="ml-auto flex overflow-hidden rounded border border-slate-300 text-sm">
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
          <button
            type="button"
            className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
            disabled={!doc}
            onClick={onTestCall}
          >
            {locked ? "In call…" : "Test call"}
          </button>
        </>
      )}
    </header>
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
