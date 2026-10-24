import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentConfig, IssuesResponse } from "../../generated/contract";
import { registerBuffer } from "../buffers";
import * as d from "../document";
import { startTestCall } from "../session";
import { editorStore } from "../store";
import { agent } from "./fixtures";

const state = () => editorStore.getState();
const ok: IssuesResponse = { issues: [], malformed: false };

beforeEach(() => {
  state().setLocked(false);
  state().open("test", { doc: agent() }, true);
});

describe("startTestCall", () => {
  it("locks before validating the exact snapshot it then calls", async () => {
    let lockedDuringValidation = false;
    const validate = vi.fn(async (_doc: AgentConfig) => {
      lockedDuringValidation = state().locked;
      return ok;
    });
    const start = vi.fn(async (_agent: AgentConfig) => undefined);

    expect(await startTestCall(start, validate)).toBeNull();
    expect(lockedDuringValidation).toBe(true);
    expect(state().locked).toBe(true); // held until the call is torn down
    const snapshot = start.mock.calls[0][0];
    expect(snapshot).toEqual(state().doc);
    expect(snapshot).not.toBe(state().doc);
    expect(validate.mock.calls[0][0]).toBe(snapshot);
  });

  it("commits pending field buffers into the snapshot first", async () => {
    const unregister = registerBuffer(() => {
      state().edit((doc) => d.updateAgent(doc, { name: "Typed but not blurred" }));
      unregister();
      return null;
    });
    const start = vi.fn(async (_agent: AgentConfig) => undefined);
    await startTestCall(start, async () => ok);
    expect(start.mock.calls[0][0].name).toBe("Typed but not blurred");
  });

  it("refuses, unlocked, when a buffer can't be committed", async () => {
    const unregister = registerBuffer(() => "A node named 'done' already exists.");
    const start = vi.fn(async (_agent: AgentConfig) => undefined);
    const refusal = await startTestCall(start, async () => ok);
    unregister();

    expect(refusal?.error).toMatch(/already exists/);
    expect(start).not.toHaveBeenCalled();
    expect(state().locked).toBe(false);
  });

  it("releases the lock when the snapshot has errors or validation fails", async () => {
    const start = vi.fn(async (_agent: AgentConfig) => undefined);
    const invalid: IssuesResponse = {
      issues: [
        { level: "warning", path: ["nodes", 0], node: "greeting", edge: null, message: "w" },
        { level: "error", path: ["initial_node"], node: null, edge: null, message: "e" },
      ],
      malformed: false,
    };

    const refusal = await startTestCall(start, async () => invalid);
    expect(refusal?.issues?.map((i) => i.message)).toEqual(["e"]);
    expect(state().locked).toBe(false);

    const failed = await startTestCall(start, async () => {
      throw new Error("backend down");
    });
    expect(failed?.error).toBe("backend down");
    expect(state().locked).toBe(false);
    expect(start).not.toHaveBeenCalled();
  });
});
