// The builder API (backend/api.py), through the Vite proxy.

import type { AgentConfig, AgentSummary, IssuesResponse } from "./generated/contract";

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
