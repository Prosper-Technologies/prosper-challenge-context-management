import { beforeEach, describe, expect, it } from "vitest";
import * as d from "../document";
import { toFlow } from "../flow";
import { layoutMissing, tidyLayout } from "../layout";
import { createEditorStore, isDirty } from "../store";
import { agent } from "./fixtures";

let store: ReturnType<typeof createEditorStore>;
const state = () => store.getState();
const history = () => store.temporal.getState();

beforeEach(() => {
  store = createEditorStore();
  state().open("test", { doc: agent() }, true);
});

describe("undo history", () => {
  it("starts empty when an agent is opened", () => {
    state().edit((doc) => d.updateAgent(doc, { name: "Changed" }));
    state().open("other", { doc: agent() }, true);
    expect(history().pastStates).toHaveLength(0);
  });

  it("records a rename, with its reference updates, as one step", () => {
    state().edit((doc) => d.renameNode(doc, 1, "details"));
    expect(history().pastStates).toHaveLength(1);
    expect(state().doc!.nodes[0].edges![0].target).toBe("details");

    state().undo();
    expect(state().doc).toEqual(agent());
    state().redo();
    expect(state().doc!.nodes[1].name).toBe("details");
  });

  it("records tidy layout as one step, keeping other position fields", () => {
    state().edit((doc) => d.moveNodes(doc, new Map([[2, { x: 900, y: -40 }]])));
    history().clear();
    state().edit(tidyLayout);

    const nodes = state().doc!.nodes;
    expect(history().pastStates).toHaveLength(1);
    expect(nodes[0].ui!.y).toBeLessThan(nodes[1].ui!.y);
    expect(nodes[1].ui!.y).toBeLessThan(nodes[2].ui!.y);
    expect(nodes[1].ui!.x_pinned).toBe(true);
    // Already tidy: nothing changes, so no new step.
    state().edit(tidyLayout);
    expect(history().pastStates).toHaveLength(1);
  });

  it("records a drag, committed on drop, as one step", () => {
    state().edit((doc) =>
      d.moveNodes(
        doc,
        new Map([
          [0, { x: 50, y: 50 }],
          [1, { x: 60, y: 260 }],
        ]),
      ),
    );
    expect(history().pastStates).toHaveLength(1);
    state().undo();
    expect(state().doc!.nodes[0].ui).toEqual({ x: 0, y: 0 });
  });

  it("doesn't record transient changes or no-op edits", () => {
    state().select({ kind: "node", node: 1 });
    state().setValidation({ status: "done", issues: [], malformed: false });
    state().setTrace([{ type: "node_entered", from: null, node: "greeting", via: null, state: {} }], true);
    state().setView("json");
    state().edit((doc) => d.updateAgent(doc, { name: "Test" }));
    expect(history().pastStates).toHaveLength(0);
    expect(isDirty(state())).toBe(false);
  });

  it("falls back to agent settings when undo removes the selection", () => {
    state().edit((doc) => {
      const { doc: next, index } = d.addNode(doc, { x: 0, y: 0 });
      return [next, { kind: "node", node: index }];
    });
    expect(state().selection).toEqual({ kind: "node", node: 3 });
    state().undo();
    expect(state().selection).toEqual({ kind: "agent" });
  });
});

describe("the call lock", () => {
  it("refuses document edits, undo and redo, but not selection", () => {
    state().edit((doc) => d.updateAgent(doc, { name: "Before" }));
    state().setLocked(true);

    expect(state().edit((doc) => d.updateAgent(doc, { name: "During" }))).toMatch(/locked/);
    expect(state().replace(agent())).toMatch(/locked/);
    state().undo();
    expect(state().doc!.name).toBe("Before");
    state().select({ kind: "node", node: 2 });
    expect(state().selection).toEqual({ kind: "node", node: 2 });

    state().setLocked(false);
    state().undo();
    expect(state().doc!.name).toBe("Test");
  });
});

describe("dirty tracking", () => {
  it("compares against the opened document, by value", () => {
    state().edit((doc) => d.updateAgent(doc, { name: "Changed" }));
    expect(isDirty(state())).toBe(true);
    state().edit((doc) => d.updateAgent(doc, { name: "Test" }));
    expect(isDirty(state())).toBe(false);
  });

  it("treats a new agent as unsaved until saved", () => {
    state().open("fresh", { doc: agent() }, false);
    expect(isDirty(state())).toBe(true);
    state().markSaved(state().doc!);
    expect(isDirty(state())).toBe(false);
  });

  it("doesn't count the initial layout as a change", () => {
    const doc = agent();
    doc.nodes.forEach((n) => delete n.ui);
    state().open("unplaced", { doc }, true);
    expect(state().doc!.nodes.every((n) => n.ui)).toBe(true);
    expect(isDirty(state())).toBe(false);
  });

  it("opens documents with colliding names in the JSON view", () => {
    const doc = agent();
    doc.nodes[2].name = "collect";
    state().open("dupes", { doc }, true);
    expect(state().view).toBe("json");
  });
});

describe("toFlow and layout", () => {
  it("keeps ids and positions stable across unrelated edits", () => {
    const before = toFlow(agent());
    const edited = toFlow(d.setMessageContent(agent(), 0, 0, "New prompt"));
    expect(edited.nodes.map((n) => [n.id, n.position])).toEqual(
      before.nodes.map((n) => [n.id, n.position]),
    );
    expect(edited.edges.map((e) => e.id)).toEqual(["greeting::choose", "collect::done"]);
  });

  it("leaves dangling edges off the canvas", () => {
    const doc = d.updateEdge(agent(), 1, 0, { target: "nowhere" });
    expect(toFlow(doc).edges.map((e) => e.id)).toEqual(["greeting::choose"]);
  });

  it("only places nodes that have no position", () => {
    const doc = agent();
    expect(layoutMissing(doc)).toBe(doc); // prompt edits never rerun layout

    delete doc.nodes[2].ui;
    const placed = layoutMissing(doc);
    expect(placed.nodes[0].ui).toEqual({ x: 0, y: 0 });
    expect(placed.nodes[1].ui).toEqual({ x: 0, y: 200, x_pinned: true });
    expect(placed.nodes[2].ui!.y).toBeGreaterThan(200);
  });
});
