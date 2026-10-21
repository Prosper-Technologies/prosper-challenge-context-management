import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentConfig, IssuesResponse } from "../../generated/contract";
import * as d from "../document";
import { type Validation, createValidator } from "../validation";
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
