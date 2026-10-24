import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentConfig, IssuesResponse } from "../../generated/contract";
import * as d from "../document";
import { type Validation, createValidator, issueSelection } from "../validation";
import { agent } from "./fixtures";

const issue = (message: string) => ({
  level: "error" as const,
  path: ["initial_node"],
  node: null,
  edge: null,
  message,
});

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

function setup() {
  const resolvers: ((r: IssuesResponse) => void)[] = [];
  const request = vi.fn(
    (_doc: AgentConfig) => new Promise<IssuesResponse>((resolve) => resolvers.push(resolve)),
  );
  const results: Validation[] = [];
  const validator = createValidator({ request, onResult: (r) => results.push(r), delay: 100 });
  return { request, resolvers, results, validator };
}

describe("live validation", () => {
  it("debounces and skips position-only changes", async () => {
    const { request, validator } = setup();
    const doc = agent();
    validator.schedule(doc);
    validator.schedule(d.updateAgent(doc, { name: "A" }));
    await vi.advanceTimersByTimeAsync(100);
    expect(request).toHaveBeenCalledTimes(1);

    const named = d.updateAgent(doc, { name: "A" });
    validator.schedule(d.moveNodes(named, new Map([[0, { x: 99, y: 99 }]])));
    await vi.advanceTimersByTimeAsync(100);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("ignores a response for an older document", async () => {
    const { request, resolvers, results, validator } = setup();
    validator.schedule(agent());
    await vi.advanceTimersByTimeAsync(100);
    validator.schedule(d.updateAgent(agent(), { name: "Newer" }));
    await vi.advanceTimersByTimeAsync(100);
    expect(request).toHaveBeenCalledTimes(2);

    resolvers[1]({ issues: [issue("new")], malformed: false });
    resolvers[0]({ issues: [issue("old")], malformed: false });
    await vi.runAllTimersAsync();

    const done = results.filter((r) => r.status === "done");
    expect(done).toEqual([{ status: "done", issues: [issue("new")], malformed: false }]);
  });
});

describe("issueSelection", () => {
  const at = (...path: (string | number)[]) => ({ ...issue("x"), path });

  it("routes an issue to what its path points at", () => {
    expect(issueSelection(at("initial_node"))).toEqual({ kind: "agent" });
    expect(issueSelection(at("persona"))).toEqual({ kind: "agent" });
    expect(issueSelection(at("nodes", 2, "name"))).toEqual({ kind: "node", node: 2 });
    expect(issueSelection(at("nodes", 2, "edges", 1, "target"))).toEqual({ kind: "edge", node: 2, edge: 1 });
  });

  it("routes tool issues to the node and guard issues to the edge", () => {
    expect(issueSelection(at("nodes", 1, "tools", 3))).toEqual({ kind: "node", node: 1 });
    expect(issueSelection(at("nodes", 1, "tools"))).toEqual({ kind: "node", node: 1 });
    expect(issueSelection(at("nodes", 4, "edges", 0, "guard"))).toEqual({ kind: "edge", node: 4, edge: 0 });
  });

  it("falls back to the agent or the node when the path is partial", () => {
    expect(issueSelection(at("nodes"))).toEqual({ kind: "agent" });
    expect(issueSelection(at("nodes", 0, "edges"))).toEqual({ kind: "node", node: 0 });
  });
});
