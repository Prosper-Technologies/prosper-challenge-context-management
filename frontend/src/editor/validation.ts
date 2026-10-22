// Live validation: the backend's /validate, debounced, for the badge and the
// inspector. Position-only changes (dragging) don't revalidate, and a response
// for an older document is dropped.

import type { AgentConfig, Issue, IssuesResponse } from "../generated/contract";
import type { Selection } from "./store";

export type Validation =
  | { status: "idle" }
  | { status: "pending"; issues: Issue[] } // the previous result's issues, until replaced
  | { status: "done"; issues: Issue[]; malformed: boolean }
  | { status: "failed"; error: string };

/** The document minus canvas positions: what validation depends on. */
export function validationKey(doc: AgentConfig): string {
  return JSON.stringify(doc, (key, value) => (key === "ui" ? undefined : value));
}

export function createValidator(options: {
  request: (doc: AgentConfig) => Promise<IssuesResponse>;
  onResult: (result: Validation) => void;
  delay?: number;
}) {
  const { request, onResult, delay = 400 } = options;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let lastKey: string | null = null;
  let issues: Issue[] = [];
  let token = 0;

  return {
    schedule(doc: AgentConfig | null) {
      if (!doc) {
        clearTimeout(timer);
        token++;
        lastKey = null;
        onResult({ status: "idle" });
        return;
      }
      const key = validationKey(doc);
      if (key === lastKey) return;
      lastKey = key;
      clearTimeout(timer);
      const mine = ++token;
      onResult({ status: "pending", issues });
      timer = setTimeout(async () => {
        try {
          const result = await request(doc);
          if (mine !== token) return; // a newer document was scheduled meanwhile
          issues = result.issues;
          onResult({ status: "done", ...result });
        } catch (e) {
          if (mine !== token) return;
          lastKey = null; // retry on the next change
          onResult({ status: "failed", error: e instanceof Error ? e.message : String(e) });
        }
      }, delay);
    },
    dispose() {
      clearTimeout(timer);
      token++;
    },
  };
}

const NO_ISSUES: Issue[] = [];

/** The current issues (a stable reference, so it's safe in a store selector). */
export function issuesOf(validation: Validation): Issue[] {
  return validation.status === "pending" || validation.status === "done"
    ? validation.issues
    : NO_ISSUES;
}

/** What an issue points at, from its path. */
export function issueSelection(issue: Issue): Selection {
  const [field, i, sub, j] = issue.path;
  if (field !== "nodes" || typeof i !== "number") return { kind: "agent" };
  if (sub === "edges" && typeof j === "number") return { kind: "edge", node: i, edge: j };
  return { kind: "node", node: i };
}

/** Issues about node `i` itself (not its edges). */
export function nodeIssues(issues: Issue[], i: number): Issue[] {
  return issues.filter((x) => x.path[0] === "nodes" && x.path[1] === i && x.path[2] !== "edges");
}

export function edgeIssues(issues: Issue[], i: number, j: number): Issue[] {
  return issues.filter(
    (x) => x.path[0] === "nodes" && x.path[1] === i && x.path[2] === "edges" && x.path[3] === j,
  );
}

export function agentIssues(issues: Issue[]): Issue[] {
  return issues.filter((x) => x.path[0] !== "nodes" || typeof x.path[1] !== "number");
}

export function worstLevel(issues: Pick<Issue, "level">[]): "error" | "warning" | null {
  if (issues.some((x) => x.level === "error")) return "error";
  return issues.length > 0 ? "warning" : null;
}
