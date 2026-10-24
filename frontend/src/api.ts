// The builder API (backend/api.py) and the catalog API (backend/catalog_api.py),
// through the Vite proxy.

import type {
  AgentConfig,
  AgentSummary,
  Catalog,
  IssuesResponse,
  LintResponse,
  PlaygroundSession,
  ToolRunResponse,
  ToolsResponse,
} from "./generated/contract";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

const UNREACHABLE = "The backend is unreachable (is `make dev` running?)";

async function request<T>(path: string, init?: RequestInit, accept: number[] = []): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ApiError(`${UNREACHABLE}: ${e}`, 0);
  }
  // The Vite dev proxy answers 502 when nothing is listening on :7860.
  if (res.status === 502) throw new ApiError(UNREACHABLE, 502);
  if (!res.ok && !accept.includes(res.status)) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // No JSON body; keep the status text.
    }
    throw new ApiError(`${res.status} ${detail}`, res.status);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const listAgents = () => request<AgentSummary[]>("/api/agents");

/** The saved document as written; it may be malformed if edited by hand. */
export const getAgent = (id: string) => request<unknown>(`/api/agents/${id}`);

/** Save; a malformed document comes back as `{malformed: true}` (HTTP 422), unsaved. */
export const putAgent = (id: string, doc: AgentConfig) =>
  request<IssuesResponse>(`/api/agents/${id}`, json("PUT", doc), [422]);

export const deleteAgent = (id: string) =>
  request<void>(`/api/agents/${id}`, { method: "DELETE" });

export const validateAgent = (doc: unknown) =>
  request<IssuesResponse>("/api/agents/validate", json("POST", doc));

// ---- catalog ------------------------------------------------------------------------

export const getCatalog = () => request<Catalog>("/api/catalog");

export const getCatalogLint = () => request<LintResponse>("/api/catalog/lint");

/** Tool schemas and edge guard metadata (the registry agents may name). */
export const listTools = () => request<ToolsResponse>("/api/catalog/tools");

/** A new sandbox session, with its own calendar. */
export const createPlaygroundSession = () =>
  request<PlaygroundSession>("/api/catalog/sessions", { method: "POST" });

/** `keepalive` lets it outlive the page (on `pagehide`). Expired ids are a 204 too. */
export const deletePlaygroundSession = (id: string, { keepalive = false } = {}) =>
  request<void>(`/api/catalog/sessions/${encodeURIComponent(id)}`, { method: "DELETE", keepalive });

/** Run a tool in a sandbox session. A tool's own failure is a 200 with `{error}` in
 * `result`; an unknown session is a 404 and an expired one a 410 (`ApiError.status`). */
export const runTool = (name: string, sessionId: string, args: Record<string, unknown>) =>
  request<ToolRunResponse>(
    `/api/catalog/tools/${encodeURIComponent(name)}`,
    json("POST", { session_id: sessionId, args }),
  );
