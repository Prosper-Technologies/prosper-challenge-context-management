// Agent-level flows that talk to the backend: list, open, create, duplicate,
// delete, save, import/export, and applying the raw JSON view. Agent switching
// (which includes importing) is refused while a test call holds the lock.

import * as api from "../api";
import type { AgentConfig, Issue, IssuesResponse } from "../generated/contract";
import { commitBuffers } from "./buffers";
import { editorStore, isDirty, repairProblems } from "./store";

const state = () => editorStore.getState();

function messageOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function fail(e: unknown) {
  state().notify({ kind: "error", text: messageOf(e) });
}

function confirmDiscard(): boolean {
  return !isDirty(state()) || window.confirm("Discard unsaved changes to this agent?");
}

function canSwitch(): boolean {
  if (state().locked) {
    state().notify({ kind: "error", text: "Hang up before switching agents." });
    return false;
  }
  return confirmDiscard();
}

export async function refreshAgents() {
  try {
    state().setAgents(await api.listAgents());
  } catch (e) {
    fail(e);
  }
}

export async function openAgent(id: string, { confirm = true } = {}) {
  if (confirm && !canSwitch()) return;
  try {
    const data = await api.getAgent(id);
    const { malformed } = await api.validateAgent(data);
    state().open(
      id,
      malformed ? { raw: JSON.stringify(data, null, 2) } : { doc: data as AgentConfig },
      true,
    );
  } catch (e) {
    fail(e);
  }
}

/** Lowercase letters, digits, '_' and '-', unique among saved agents. */
export function agentIdFor(name: string): string {
  const base =
    name
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 56) || "agent";
  const taken = new Set(state().agents.map((a) => a.id));
  if (!taken.has(base)) return base;
  for (let n = 2; ; n++) if (!taken.has(`${base}-${n}`)) return `${base}-${n}`;
}

export function newAgent(name: string) {
  if (!canSwitch()) return;
  const doc: AgentConfig = {
    name,
    persona: "",
    initial_node: "greeting",
    nodes: [
      {
        name: "greeting",
        task_messages: [{ role: "developer", content: "Greet the caller." }],
        ui: { x: 0, y: 0 },
      },
    ],
  };
  state().open(agentIdFor(name), { doc }, false);
}

export function duplicateAgent() {
  const { doc } = state();
  if (!doc || state().locked) return;
  const name = `${doc.name} (copy)`;
  // The duplicate carries the current edits; the original keeps its own.
  const copy = structuredClone({ ...doc, name });
  if (!confirmDiscard()) return;
  state().open(agentIdFor(name), { doc: copy }, false);
}

export async function deleteAgent(id: string) {
  const { agentId, saved, locked } = state();
  if (locked) return;
  if (!window.confirm(`Delete agent '${id}'? This can't be undone.`)) return;
  try {
    if (id !== agentId || saved) await api.deleteAgent(id);
    if (id === agentId) state().close();
    await refreshAgents();
  } catch (e) {
    fail(e);
  }
}

/** Commit pending field buffers; why not, if any was rejected. */
async function commitPending(action: string): Promise<string | null> {
  const errors = await commitBuffers();
  return errors.length ? `Can't ${action} until the field shown in red is fixed: ${errors[0]}` : null;
}

export async function saveAgent() {
  const pending = await commitPending("save");
  if (pending) return state().notify({ kind: "error", text: pending });
  const { agentId, doc } = state();
  if (!agentId || !doc) return;
  try {
    const result = await api.putAgent(agentId, doc);
    if (result.malformed) {
      state().notify({ kind: "error", text: `Not saved: ${result.issues[0]?.message}` });
      return;
    }
    state().markSaved(doc);
    const errors = result.issues.filter((i) => i.level === "error").length;
    state().notify({
      kind: "info",
      text: errors ? `Saved with ${errors} error(s); fix them before a test call.` : "Saved.",
    });
    await refreshAgents();
  } catch (e) {
    fail(e);
  }
}

type Parsed = { doc: AgentConfig } | { error: string; issues?: Issue[] };

/** Parse JSON text and check with the backend that it's a well-formed agent.
 * Throws if the backend can't be reached. */
async function parseAgent(
  text: string,
  validate: (doc: unknown) => Promise<IssuesResponse> = api.validateAgent,
): Promise<Parsed> {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch (e) {
    return { error: `Invalid JSON: ${messageOf(e)}` };
  }
  const result = await validate(data);
  return result.malformed
    ? { error: "Not a well-formed agent.", issues: result.issues }
    : { doc: data as AgentConfig };
}

/** Open a JSON file as a new, unsaved agent, named after the file.
 *
 * The document is taken as is, so fields the builder doesn't know about survive;
 * only nodes without a canvas position get one. A file that isn't a well-formed
 * agent (or isn't JSON at all) opens in the JSON repair view with its text. */
export async function importAgent(
  fileName: string,
  text: string,
  validate: (doc: unknown) => Promise<IssuesResponse> = api.validateAgent,
) {
  if (!canSwitch()) return;
  let parsed: Parsed;
  try {
    parsed = await parseAgent(text, validate);
  } catch (e) {
    return fail(e);
  }
  const id = agentIdFor(fileName.replace(/\.json$/i, ""));
  state().open(id, "doc" in parsed ? { doc: parsed.doc } : { raw: text }, false);
  state().notify(
    "doc" in parsed
      ? { kind: "info", text: `Imported ${fileName} as '${id}'. It isn't saved yet.` }
      : { kind: "error", text: `${fileName} isn't a well-formed agent. Repair it below, then apply.` },
  );
}

/** The open agent as a JSON file (`<id>.json`), pending field edits included. */
export async function exportAgent(): Promise<{ fileName: string; text: string } | null> {
  const pending = await commitPending("export");
  if (pending) {
    state().notify({ kind: "error", text: pending });
    return null;
  }
  const { agentId, doc, raw } = state();
  if (!agentId || (!doc && raw === null)) return null;
  return { fileName: `${agentId}.json`, text: doc ? `${JSON.stringify(doc, null, 2)}\n` : raw! };
}

/** Replace the document with raw JSON, if it's a well-formed agent. */
export async function applyJson(text: string): Promise<{ error: string; issues?: Issue[] } | null> {
  let parsed: Parsed;
  try {
    parsed = await parseAgent(text);
  } catch (e) {
    return { error: messageOf(e) };
  }
  if (!("doc" in parsed)) return parsed;
  const refused = state().replace(parsed.doc);
  return refused ? { error: refused } : null;
}

export interface CallRefusal {
  error: string;
  issues?: Issue[];
}

/** Start a test call of the current draft, without saving it.
 *
 * Commits pending fields, then locks the document and validates that exact
 * snapshot; the call starts only if it has no errors. On any refusal the lock is
 * released here. Once the call starts, the caller releases the lock when the
 * call is torn down (see `useCallLock`). */
export async function startTestCall(
  start: (agent: AgentConfig) => Promise<void>,
  validate: (doc: AgentConfig) => Promise<IssuesResponse> = api.validateAgent,
): Promise<CallRefusal | null> {
  if (state().locked) return { error: "A call is already in progress." };
  const pending = await commitPending("start a call");
  if (pending) return { error: pending };
  const { doc } = state();
  if (!doc) return { error: "No agent is open." };
  if (repairProblems(doc).length) return { error: "Repair the agent's JSON before a test call." };

  state().setLocked(true);
  const snapshot = structuredClone(doc);
  try {
    const result = await validate(snapshot);
    const errors = result.issues.filter((i) => i.level === "error");
    if (result.malformed || errors.length) {
      state().setLocked(false);
      return { error: `Fix ${errors.length} error(s) before a test call.`, issues: errors };
    }
  } catch (e) {
    state().setLocked(false);
    return { error: messageOf(e) };
  }
  void start(snapshot);
  return null;
}
