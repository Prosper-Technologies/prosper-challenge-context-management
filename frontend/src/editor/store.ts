// Editor state. `doc` is the one persisted document, and the only thing undo
// history tracks (zundo, partialized to `doc`). Everything else here (selection,
// view, validation, the call lock and trace) is transient: changing it neither
// dirties the document nor records history.
//
// Every document change goes through `edit`, one action per transaction, so an
// undo step is a whole drag, field commit or rename. While a test call holds the
// lock, `edit`, undo and redo are refused.

import { type TemporalState, temporal } from "zundo";
import { type StoreApi, createStore, useStore } from "zustand";
import type { AgentConfig, AgentSummary } from "../generated/contract";
import type { NodeEntered } from "../testCall";
import { EditError, identifierProblems } from "./document";
import { edgeId } from "./flow";
import { layoutMissing } from "./layout";
import type { Validation } from "./validation";

export type Selection =
  | { kind: "agent" }
  | { kind: "node"; node: number }
  | { kind: "edge"; node: number; edge: number };

export interface Trace {
  active: string | null; // the call's current node; null once the call ends
  nodes: Set<string>; // visited nodes
  edges: Set<string>; // traversed edge ids
}

export interface Notice {
  kind: "error" | "info";
  text: string;
}

export type EditResult = AgentConfig | [AgentConfig, Selection];

interface EditorState {
  agents: AgentSummary[];
  agentId: string | null;
  saved: boolean; // the agent exists on disk
  doc: AgentConfig | null; // null: nothing open, or `raw` is too malformed to edit
  baseline: AgentConfig | null; // what "unsaved changes" is measured against
  raw: string | null; // a loaded document that isn't a well-formed agent
  view: "canvas" | "json";
  selection: Selection;
  locked: boolean;
  validation: Validation;
  trace: Trace;
  notice: Notice | null;

  setAgents(agents: AgentSummary[]): void;
  /** Open a document (or malformed raw text) as the current agent; clears history. */
  open(agentId: string, source: { doc: AgentConfig } | { raw: string }, saved: boolean): void;
  close(): void;
  /** Apply one document action as a transaction; returns why it was refused, or null. */
  edit(fn: (doc: AgentConfig) => EditResult): string | null;
  /** Replace the whole document (the JSON view), giving new nodes a position. */
  replace(doc: AgentConfig): string | null;
  markSaved(doc: AgentConfig): void;
  select(selection: Selection): void;
  setView(view: "canvas" | "json"): void;
  setLocked(locked: boolean): void;
  setValidation(validation: Validation): void;
  setTrace(path: NodeEntered[], live: boolean): void;
  notify(notice: Notice | null): void;
  undo(): void;
  redo(): void;
}

type History = TemporalState<{ doc: AgentConfig | null }>;

// zundo attaches `temporal` to the store before the state creator runs, but its
// creator typing leaves it `unknown`.
const history = (store: unknown) =>
  (store as { temporal: StoreApi<History> }).temporal.getState();

const emptyTrace: Trace = { active: null, nodes: new Set(), edges: new Set() };

/** The selection, or the agent settings if it no longer points at anything. */
export function resolveSelection(doc: AgentConfig | null, selection: Selection): Selection {
  if (selection.kind === "agent" || !doc) return { kind: "agent" };
  const node = doc.nodes[selection.node];
  if (!node) return { kind: "agent" };
  if (selection.kind === "edge" && !node.edges?.[selection.edge]) return { kind: "agent" };
  return selection;
}

export function createEditorStore() {
  return createStore<EditorState>()(
    temporal(
      (set, get, store) => ({
        agents: [],
        agentId: null,
        saved: false,
        doc: null,
        baseline: null,
        raw: null,
        view: "canvas",
        selection: { kind: "agent" },
        locked: false,
        validation: { status: "idle" },
        trace: emptyTrace,
        notice: null,

        setAgents: (agents) => set({ agents }),

        open(agentId, source, saved) {
          const doc = "doc" in source ? layoutMissing(source.doc) : null;
          set({
            agentId,
            saved,
            doc,
            // A freshly laid-out document isn't an unsaved change.
            baseline: saved ? doc : null,
            raw: "raw" in source ? source.raw : null,
            view: doc && identifierProblems(doc).length === 0 ? "canvas" : "json",
            selection: { kind: "agent" },
            trace: emptyTrace,
            notice: null,
          });
          history(store).clear();
        },

        close() {
          set({ agentId: null, saved: false, doc: null, baseline: null, raw: null });
          history(store).clear();
        },

        edit(fn) {
          const { doc, locked, selection } = get();
          if (locked) return "Editing is locked during a test call.";
          if (!doc) return "No agent is open.";
          let result: EditResult;
          try {
            result = fn(doc);
          } catch (e) {
            if (e instanceof EditError) return e.message;
            throw e;
          }
          const [next, nextSelection] = Array.isArray(result) ? result : [result, selection];
          if (next !== doc || nextSelection !== selection)
            set({ doc: next, selection: resolveSelection(next, nextSelection) });
          return null;
        },

        replace(doc) {
          const { locked, selection } = get();
          if (locked) return "Editing is locked during a test call.";
          const next = layoutMissing(doc);
          set({ doc: next, raw: null, selection: resolveSelection(next, selection) });
          return null;
        },

        markSaved(doc) {
          set({ baseline: doc, saved: true });
        },

        select: (selection) => set({ selection: resolveSelection(get().doc, selection) }),
        setView: (view) => set({ view }),
        setLocked: (locked) => set({ locked }),
        setValidation: (validation) => set({ validation }),
        notify: (notice) => set({ notice }),

        setTrace(path, live) {
          set({
            trace: {
              active: live ? (path.at(-1)?.node ?? null) : null,
              nodes: new Set(path.map((p) => p.node)),
              edges: new Set(
                path.flatMap((p) => (p.from && p.via ? [edgeId(p.from, p.via)] : [])),
              ),
            },
          });
        },

        undo() {
          if (get().locked) return;
          history(store).undo();
          set({ selection: resolveSelection(get().doc, get().selection) });
        },

        redo() {
          if (get().locked) return;
          history(store).redo();
          set({ selection: resolveSelection(get().doc, get().selection) });
        },
      }),
      {
        partialize: (state) => ({ doc: state.doc }),
        // Only document changes are history; selection, validation etc. are not.
        equality: (past, current) => past.doc === current.doc,
        limit: 200,
      },
    ),
  );
}

export type EditorStore = ReturnType<typeof createEditorStore>;

export const editorStore = createEditorStore();

export function useEditor<T>(selector: (state: EditorState) => T): T {
  return useStore(editorStore, selector);
}

export function useHistory() {
  const canUndo = useStore(editorStore.temporal, (s) => s.pastStates.length > 0);
  const canRedo = useStore(editorStore.temporal, (s) => s.futureStates.length > 0);
  return { canUndo, canRedo };
}

// ---- derived state -----------------------------------------------------------------

/** Deep equality for JSON values. */
export function jsonEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every(
    (k) => k in b && jsonEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]),
  );
}

export function isDirty(state: Pick<EditorState, "doc" | "baseline" | "saved">): boolean {
  if (!state.saved) return state.doc !== null;
  return state.doc !== state.baseline && !jsonEqual(state.doc, state.baseline);
}

const problemsCache = new WeakMap<AgentConfig, string[]>();

/** Why the document can't be shown on the canvas (empty if it can). */
export function repairProblems(doc: AgentConfig | null): string[] {
  if (!doc) return ["This document isn't a well-formed agent."];
  let problems = problemsCache.get(doc);
  if (!problems) problemsCache.set(doc, (problems = identifierProblems(doc)));
  return problems;
}
