import { beforeEach, describe, expect, it, vi } from "vitest";
import type { IssuesResponse } from "../../generated/contract";
import { registerBuffer } from "../buffers";
import * as d from "../document";
import { exportAgent, importAgent } from "../session";
import { editorStore, isDirty } from "../store";
import { agent } from "./fixtures";

const state = () => editorStore.getState();
const ok = async (): Promise<IssuesResponse> => ({ issues: [], malformed: false });
const malformed = async (): Promise<IssuesResponse> => ({
  issues: [{ level: "error", path: ["nodes"], node: null, edge: null, message: "Field required" }],
  malformed: true,
});

beforeEach(() => {
  state().setLocked(false);
  state().setAgents([{ id: "example", name: "Example" }]);
  state().open("test", { doc: agent() }, true);
});

describe("import", () => {
  it("opens the file as a new unsaved agent, content intact", async () => {
    await importAgent("intake.json", JSON.stringify(agent()), ok);

    expect(state().agentId).toBe("intake");
    expect(state().saved).toBe(false);
    expect(state().doc).toEqual(agent()); // unknown fields and positions included
    expect(state().view).toBe("canvas");
    expect(isDirty(state())).toBe(true);
  });

  it("doesn't take a saved agent's id", async () => {
    await importAgent("Example.json", JSON.stringify(agent()), ok);
    expect(state().agentId).toBe("example-2");
  });

  it("only adds positions to nodes that have none", async () => {
    const doc = agent();
    delete doc.nodes[2].ui;
    await importAgent("a.json", JSON.stringify(doc), ok);

    const nodes = state().doc!.nodes;
    expect(nodes[2].ui).toBeDefined();
    expect(nodes[1].ui).toEqual(agent().nodes[1].ui);
  });

  it("opens a malformed agent or invalid JSON in the repair view, text intact", async () => {
    await importAgent("broken.json", '{"name": "No nodes"}', malformed);
    expect(state().doc).toBeNull();
    expect(state().raw).toBe('{"name": "No nodes"}');
    expect(state().view).toBe("json");
    expect(state().notice?.kind).toBe("error");

    const validate = vi.fn(ok);
    await importAgent("typo.json", '{"name": "x",}', validate);
    expect(validate).not.toHaveBeenCalled();
    expect(state().raw).toBe('{"name": "x",}');
  });

  it("is refused during a test call", async () => {
    state().setLocked(true);
    await importAgent("intake.json", JSON.stringify(agent()), ok);
    expect(state().agentId).toBe("test");
  });
});

describe("export", () => {
  it("round-trips through import unchanged", async () => {
    const file = await exportAgent();
    expect(file?.fileName).toBe("test.json");
    expect(JSON.parse(file!.text)).toEqual(agent());

    await importAgent(file!.fileName, file!.text, ok);
    expect(state().doc).toEqual(agent());
  });

  it("includes pending field edits, and stops on a rejected one", async () => {
    const unregister = registerBuffer(() => {
      state().edit((doc) => d.updateAgent(doc, { name: "Typed" }));
      unregister();
      return null;
    });
    expect(JSON.parse((await exportAgent())!.text).name).toBe("Typed");

    const stop = registerBuffer(() => "Bad value");
    expect(await exportAgent()).toBeNull();
    stop();
    expect(state().notice?.text).toMatch(/Bad value/);
  });

  it("exports a malformed document's text as is", async () => {
    state().open("broken", { raw: "{ not json" }, false);
    expect((await exportAgent())?.text).toBe("{ not json");
  });
});
