// The whole document as raw JSON. It's also the repair view: a document whose
// node or function names collide (or that isn't a well-formed agent at all)
// opens here, since the canvas keys elements by those names. Apply only accepts
// a well-formed agent (checked by the backend); graph errors are fine to apply.

import { useEffect, useRef, useState } from "react";
import type { Issue } from "../generated/contract";
import { registerBuffer } from "../editor/buffers";
import { applyJson } from "../editor/session";
import { editorStore, repairProblems, useEditor } from "../editor/store";
import { Button, IssueList } from "./fields";

export default function JsonView() {
  const doc = useEditor((s) => s.doc);
  const raw = useEditor((s) => s.raw);
  const locked = useEditor((s) => s.locked);
  const problems = repairProblems(doc);
  const value = doc ? JSON.stringify(doc, null, 2) : (raw ?? "");

  const [draft, setDraft] = useState<string | null>(null);
  const [result, setResult] = useState<{ error: string; issues?: Issue[] } | null>(null);
  const [applying, setApplying] = useState(false);
  const draftRef = useRef(draft);
  draftRef.current = draft;

  const apply = useRef(async () => {
    const text = draftRef.current;
    if (text === null) return null;
    const forced = repairProblems(editorStore.getState().doc).length > 0;
    setApplying(true);
    const failed = await applyJson(text);
    setApplying(false);
    setResult(failed);
    if (failed) return failed.error;
    setDraft(null);
    if (forced && repairProblems(editorStore.getState().doc).length === 0)
      editorStore.getState().setView("canvas");
    return null;
  }).current;

  const dirty = draft !== null;
  useEffect(() => (dirty ? registerBuffer(apply) : undefined), [dirty, apply]);

  return (
    <div className="flex h-full flex-col gap-2 p-4">
      {problems.length > 0 && (
        <div className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          <p className="font-medium">Repair needed before the canvas can show this agent:</p>
          <ul className="ml-4 list-disc">
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
          <p className="mt-1 text-xs">Edit the JSON below and apply. You can still save it as is.</p>
        </div>
      )}
      <textarea
        className="min-h-0 flex-1 rounded border border-slate-300 bg-white p-3 font-mono text-xs read-only:bg-slate-100"
        spellCheck={false}
        readOnly={locked}
        value={draft ?? value}
        onChange={(e) => {
          setDraft(e.target.value);
          setResult(null);
        }}
      />
      {result && (
        <div className="flex flex-col gap-1">
          <p className="text-sm text-red-700">{result.error}</p>
          {result.issues && <IssueList issues={result.issues} onSelect={() => undefined} />}
        </div>
      )}
      <div className="flex gap-2">
        <Button tone="primary" disabled={!dirty || locked || applying} onClick={() => void apply()}>
          {applying ? "Applying…" : "Apply"}
        </Button>
        <Button
          disabled={!dirty || applying}
          onClick={() => {
            setDraft(null);
            setResult(null);
          }}
        >
          Revert
        </Button>
        {dirty && <span className="self-center text-xs text-slate-500">Unapplied edits</span>}
      </div>
    </div>
  );
}
