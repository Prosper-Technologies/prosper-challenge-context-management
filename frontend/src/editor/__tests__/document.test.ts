import { describe, expect, it } from "vitest";
import * as d from "../document";
import { agent } from "./fixtures";

describe("renameNode", () => {
  it("updates edge targets and the initial node in one step", () => {
    const doc = agent();
    const renamed = d.renameNode(d.renameNode(doc, 1, "details"), 0, "hello");

    expect(renamed.nodes.map((n) => n.name)).toEqual(["hello", "details", "done"]);
    expect(renamed.nodes[0].edges![0].target).toBe("details");
    expect(renamed.initial_node).toBe("hello");
    expect(doc.nodes[1].name).toBe("collect"); // the source is not mutated
  });

  it("rejects empty and duplicate names", () => {
    expect(() => d.renameNode(agent(), 0, "  ")).toThrow(d.EditError);
    expect(() => d.renameNode(agent(), 0, "done")).toThrow("already exists");
  });

  it("returns the same document when nothing changes", () => {
    const doc = agent();
    expect(d.renameNode(doc, 0, "greeting")).toBe(doc);
    expect(d.updateAgent(doc, { name: "Test" })).toBe(doc);
  });
});

describe("connect", () => {
  it("generates a unique, valid go_to_<target> name", () => {
    let doc = d.renameNode(agent(), 2, "Wrap up!");
    const first = d.connect(doc, 1, "Wrap up!");
    expect(first.doc.nodes[1].edges![first.edge].function).toBe("go_to_Wrap_up_");
    doc = first.doc;
    const second = d.connect(doc, 1, "Wrap up!");
    expect(second.doc.nodes[1].edges![second.edge].function).toBe("go_to_Wrap_up__2");
  });

  it("refuses self-loops", () => {
    expect(() => d.connect(agent(), 0, "greeting")).toThrow("own node");
  });

  it("checks function renames for collisions within the node", () => {
    const { doc } = d.connect(agent(), 1, "greeting");
    expect(() => d.renameFunction(doc, 1, 1, "done")).toThrow("already has");
    expect(d.renameFunction(doc, 1, 1, "restart").nodes[1].edges![1].function).toBe("restart");
  });
});

describe("deleteElements", () => {
  it("removes a node together with the edges into it", () => {
    const doc = d.deleteElements(agent(), [1], []);
    expect(doc.nodes.map((n) => n.name)).toEqual(["greeting", "done"]);
    expect(doc.nodes[0].edges).toEqual([]);
  });

  it("removes individual edges", () => {
    const doc = d.deleteElements(agent(), [], [[1, 0]]);
    expect(doc.nodes[1].edges).toEqual([]);
    expect(doc.nodes[0].edges).toHaveLength(1);
  });
});

describe("addNode and moveNodes", () => {
  it("adds a uniquely named node with a developer task message", () => {
    const { doc, index } = d.addNode(agent(), { x: 10.4, y: 20.6 });
    expect(doc.nodes[index]).toEqual({
      name: "node_4",
      task_messages: [{ role: "developer", content: "" }],
      ui: { x: 10, y: 21 },
    });
  });

  it("moves nodes, keeping other ui fields", () => {
    const doc = d.moveNodes(agent(), new Map([[1, { x: 5, y: 6 }]]));
    expect(doc.nodes[1].ui).toEqual({ x: 5, y: 6, x_pinned: true });
    const same = agent();
    expect(d.moveNodes(same, new Map([[0, { x: 0, y: 0 }]]))).toBe(same);
  });
});

describe("form edits preserve what they don't show", () => {
  it("keeps message roles and extra fields", () => {
    const doc = d.setMessageContent(agent(), 0, 0, "Say hello.");
    expect(doc.nodes[0].task_messages![0]).toEqual({
      role: "system",
      content: "Say hello.",
      x_note: "kept",
    });
  });

  it("edits the on-entry tts_say in place, keeping other actions and order", () => {
    const doc = d.setOnEntrySay(agent(), 0, "Hi there!");
    expect(doc.nodes[0].pre_actions).toEqual([
      { type: "custom_log", level: "info" },
      { type: "tts_say", text: "Hi there!", append_text_to_context: true },
    ]);
    expect(d.setOnEntrySay(doc, 0, "").nodes[0].pre_actions).toEqual([
      { type: "custom_log", level: "info" },
    ]);
    const added = d.setOnEntrySay(agent(), 1, "Welcome back.");
    expect(added.nodes[1].pre_actions).toEqual([{ type: "tts_say", text: "Welcome back." }]);
    expect(d.onEntrySay(agent().nodes[0])).toEqual({ text: "Hello!", editable: true });
  });

  it("keeps unrelated schema keywords when editing a property", () => {
    let doc = d.updateProperty(agent(), 0, 0, "intent", { description: "Why they called." });
    doc = d.updateProperty(doc, 0, 0, "age", { type: "number" });
    const props = doc.nodes[0].edges![0].properties!;
    expect(props.intent).toEqual({
      type: "string",
      enum: ["book", "cancel"],
      x_hint: "kept",
      description: "Why they called.",
    });
    expect(props.age).toEqual({ type: "number", minimum: 0 });
    expect(props.tags).toEqual({ type: "array", items: { type: "string" } });
  });

  it("renames a property in place and in required", () => {
    const doc = d.renameProperty(agent(), 0, 0, "intent", "goal");
    const edge = doc.nodes[0].edges![0];
    expect(Object.keys(edge.properties!)).toEqual(["goal", "age", "tags"]);
    expect(edge.required).toEqual(["goal", "age"]);
    expect(() => d.renameProperty(doc, 0, 0, "goal", "age")).toThrow("already exists");
  });

  it("removes a property from required too", () => {
    const edge = d.removeProperty(agent(), 0, 0, "age").nodes[0].edges![0];
    expect(Object.keys(edge.properties!)).toEqual(["intent", "tags"]);
    expect(edge.required).toEqual(["intent"]);
  });

  it("toggles required without reordering", () => {
    let doc = d.setRequired(agent(), 0, 0, "intent", false);
    doc = d.setRequired(doc, 0, 0, "intent", true);
    expect(doc.nodes[0].edges![0].required).toEqual(["age", "intent"]);
  });

  it("sends structures the table can't edit to raw JSON", () => {
    const props = agent().nodes[0].edges![0].properties!;
    expect(d.rawOnlyReason(props.intent)).toBeNull();
    expect(d.rawOnlyReason(props.age)).toBeNull(); // `minimum` survives form edits
    expect(d.rawOnlyReason(props.tags)).toBe("type 'array'");
    expect(d.rawOnlyReason({ type: ["string", "null"] })).toBe("a union type");
    expect(d.rawOnlyReason({ type: "string", anyOf: [] })).toBe("'anyOf'");
    expect(d.rawOnlyReason({ enum: ["a, b"] })).not.toBeNull();
    expect(d.otherKeywords(props.age)).toEqual(["minimum"]);
  });
});

describe("parsers", () => {
  it("parses enums for the property type", () => {
    expect(d.parseEnum("book, cancel ,", "string")).toEqual(["book", "cancel"]);
    expect(d.parseEnum("1, 2.5", "number")).toEqual([1, 2.5]);
    expect(d.parseEnum("", "string")).toBeUndefined();
    expect(() => d.parseEnum("1.5", "integer")).toThrow("not an integer");
  });

  it("checks raw actions' shape only", () => {
    expect(d.parseActions('[{"type": "anything", "x": 1}]')).toEqual([{ type: "anything", x: 1 }]);
    expect(() => d.parseActions("{}")).toThrow("array");
    expect(() => d.parseActions('[{"text": "hi"}]')).toThrow('string "type"');
    expect(() => d.parseActions("[")).toThrow("Invalid JSON");
  });

  it("checks raw property schemas' table keywords", () => {
    expect(d.parsePropertySchema('{"type": "array", "items": {}}')).toEqual({
      type: "array",
      items: {},
    });
    expect(() => d.parsePropertySchema('{"type": 3}')).toThrow('"type"');
    expect(() => d.parsePropertySchema("[]")).toThrow("object");
  });
});

it("finds identifiers the canvas can't key by", () => {
  const doc = agent();
  expect(d.identifierProblems(doc)).toEqual([]);
  doc.nodes[2].name = "collect";
  doc.nodes[1].edges!.push({ function: "done", description: "", target: "greeting" });
  expect(d.identifierProblems(doc)).toEqual([
    "Duplicate function 'done' in node 'collect'.",
    "Duplicate node name 'collect'.",
  ]);
});
