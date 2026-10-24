// Document actions: pure functions from one AgentConfig to the next. The store
// applies each one as a single undoable transaction.
//
// Every action copies only what it changes and keeps fields it doesn't know
// about, so imported data the forms don't expose survives editing. An action
// that changes nothing returns the same document (and so records no history).
// A rejected edit (a conflicting rename, malformed JSON) throws `EditError`.

import type {
  Action,
  AgentConfig,
  Edge,
  EndConversationAction,
  Node,
  PropertySchema,
  TtsSayAction,
} from "../generated/contract";

export class EditError extends Error {}

export type AnyAction = TtsSayAction | EndConversationAction | Action;
export type ActionField = "pre_actions" | "post_actions";
export interface XY {
  x: number;
  y: number;
}

/** `undefined` removes the key. */
type Patch<T> = { [K in keyof T]?: T[K] | undefined };

function assign<T extends object>(obj: T, patch: { [K in keyof T]?: unknown }): T {
  const next = { ...obj } as Record<string, unknown>;
  let changed = false;
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) {
      if (key in next) {
        delete next[key];
        changed = true;
      }
    } else if (next[key] !== value) {
      next[key] = value;
      changed = true;
    }
  }
  return changed ? (next as T) : obj;
}

function mapNode(doc: AgentConfig, i: number, fn: (node: Node) => Node): AgentConfig {
  const node = doc.nodes[i];
  if (!node) throw new EditError("That node no longer exists.");
  const next = fn(node);
  if (next === node) return doc;
  const nodes = doc.nodes.slice();
  nodes[i] = next;
  return { ...doc, nodes };
}

function mapEdge(doc: AgentConfig, i: number, j: number, fn: (edge: Edge) => Edge): AgentConfig {
  return mapNode(doc, i, (node) => {
    const edges = node.edges ?? [];
    const edge = edges[j];
    if (!edge) throw new EditError("That edge no longer exists.");
    const next = fn(edge);
    if (next === edge) return node;
    const copy = edges.slice();
    copy[j] = next;
    return { ...node, edges: copy };
  });
}

function uniqueName(base: string, taken: Set<string>, start = 1): string {
  for (let n = start; ; n++) {
    const name = `${base}_${n}`;
    if (!taken.has(name)) return name;
  }
}

// ---- agent ------------------------------------------------------------------------

export type AgentPatch = Patch<
  Pick<AgentConfig, "name" | "persona" | "voice_id" | "model" | "initial_node">
>;

export function updateAgent(doc: AgentConfig, patch: AgentPatch): AgentConfig {
  return assign(doc, patch);
}

/** Node names that collide, and function names that collide within a node. The
 * canvas keys nodes and edges by these, so it can't show the document until
 * they're unique. */
export function identifierProblems(doc: AgentConfig): string[] {
  const problems: string[] = [];
  const seen = new Set<string>();
  for (const node of doc.nodes) {
    if (seen.has(node.name)) problems.push(`Duplicate node name '${node.name}'.`);
    seen.add(node.name);
    const functions = new Set<string>();
    for (const edge of node.edges ?? []) {
      if (functions.has(edge.function))
        problems.push(`Duplicate function '${edge.function}' in node '${node.name}'.`);
      functions.add(edge.function);
    }
  }
  return problems;
}

// ---- nodes ------------------------------------------------------------------------

export type NodePatch = Patch<Pick<Node, "role_message" | "end">>;

export function updateNode(doc: AgentConfig, i: number, patch: NodePatch): AgentConfig {
  return mapNode(doc, i, (node) => assign(node, patch));
}

/** Set the catalog tools a node exposes (deduplicated, in the given order); an
 * empty list removes the field. Names aren't checked here: the validator knows
 * the registry. */
export function setNodeTools(doc: AgentConfig, i: number, tools: string[]): AgentConfig {
  const next = [...new Set(tools)];
  return mapNode(doc, i, (node) => {
    const current = node.tools ?? [];
    if (next.length === current.length && next.every((t, k) => t === current[k])) return node;
    return assign(node, { tools: next.length ? next : undefined });
  });
}

/** `tools` with `name` added (at the end) or removed, keeping the others' order. */
export function withTool(tools: string[] | undefined, name: string, enabled: boolean): string[] {
  const current = tools ?? [];
  if (enabled) return current.includes(name) ? current : [...current, name];
  return current.filter((t) => t !== name);
}

/** Rename a node and every reference to it (edge targets, the initial node). */
export function renameNode(doc: AgentConfig, i: number, name: string): AgentConfig {
  const old = doc.nodes[i]?.name;
  if (old === undefined) throw new EditError("That node no longer exists.");
  if (name === old) return doc;
  if (!name.trim()) throw new EditError("A node name can't be empty.");
  if (doc.nodes.some((n, k) => k !== i && n.name === name))
    throw new EditError(`A node named '${name}' already exists.`);

  const nodes = doc.nodes.map((node, k) => {
    const renamed = k === i ? { ...node, name } : node;
    if (!node.edges?.some((e) => e.target === old)) return renamed;
    return {
      ...renamed,
      edges: node.edges.map((e) => (e.target === old ? { ...e, target: name } : e)),
    };
  });
  return {
    ...doc,
    nodes,
    initial_node: doc.initial_node === old ? name : doc.initial_node,
  };
}

export function addNode(doc: AgentConfig, position: XY): { doc: AgentConfig; index: number } {
  const name = uniqueName("node", new Set(doc.nodes.map((n) => n.name)), doc.nodes.length + 1);
  const node: Node = {
    name,
    task_messages: [{ role: "developer", content: "" }],
    ui: { x: Math.round(position.x), y: Math.round(position.y) },
  };
  const next: AgentConfig = { ...doc, nodes: [...doc.nodes, node] };
  if (doc.nodes.length === 0) next.initial_node = name;
  return { doc: next, index: doc.nodes.length };
}

/** Delete nodes and edges; edges into a deleted node go with it. */
export function deleteElements(
  doc: AgentConfig,
  nodes: number[],
  edges: [node: number, edge: number][],
): AgentConfig {
  if (nodes.length === 0 && edges.length === 0) return doc;
  const deletedNodes = new Set(nodes);
  const deletedNames = new Set(nodes.map((i) => doc.nodes[i]?.name));
  const deletedEdges = new Set(edges.map(([i, j]) => `${i}:${j}`));

  const remaining = doc.nodes.flatMap((node, i) => {
    if (deletedNodes.has(i)) return [];
    if (!node.edges) return [node];
    const kept = node.edges.filter(
      (e, j) => !deletedEdges.has(`${i}:${j}`) && !deletedNames.has(e.target),
    );
    return [kept.length === node.edges.length ? node : { ...node, edges: kept }];
  });
  return { ...doc, nodes: remaining };
}

/** Set canvas positions (rounded), keeping any other `ui` fields. */
export function moveNodes(doc: AgentConfig, positions: Map<number, XY>): AgentConfig {
  let next = doc;
  for (const [i, { x, y }] of positions) {
    next = mapNode(next, i, (node) => {
      const ui = { ...node.ui, x: Math.round(x), y: Math.round(y) };
      return node.ui?.x === ui.x && node.ui?.y === ui.y ? node : { ...node, ui };
    });
  }
  return next;
}

// ---- task messages ----------------------------------------------------------------

export function setMessageContent(
  doc: AgentConfig,
  i: number,
  k: number,
  content: string,
): AgentConfig {
  return mapNode(doc, i, (node) => {
    const messages = node.task_messages ?? [];
    const message = messages[k];
    if (!message) throw new EditError("That message no longer exists.");
    if (message.content === content) return node;
    const copy = messages.slice();
    copy[k] = { ...message, content };
    return { ...node, task_messages: copy };
  });
}

export function addMessage(doc: AgentConfig, i: number): AgentConfig {
  return mapNode(doc, i, (node) => ({
    ...node,
    task_messages: [...(node.task_messages ?? []), { role: "developer", content: "" }],
  }));
}

export function removeMessage(doc: AgentConfig, i: number, k: number): AgentConfig {
  return mapNode(doc, i, (node) => ({
    ...node,
    task_messages: (node.task_messages ?? []).filter((_, m) => m !== k),
  }));
}

// ---- actions ------------------------------------------------------------------------

/** The node's "on entry, say…" text: its first `tts_say` pre-action. */
export function onEntrySay(node: Node): { text: string; editable: boolean } {
  const action = node.pre_actions?.find((a) => a.type === "tts_say");
  if (!action) return { text: "", editable: true };
  return typeof action.text === "string"
    ? { text: action.text, editable: true }
    : { text: "", editable: false }; // malformed: left to the raw JSON editor
}

/** Edit the first `tts_say` pre-action in place (adding one first, or removing it
 * when cleared). Other actions and their order are untouched. */
export function setOnEntrySay(doc: AgentConfig, i: number, text: string): AgentConfig {
  return mapNode(doc, i, (node) => {
    const actions = node.pre_actions ?? [];
    const k = actions.findIndex((a) => a.type === "tts_say");
    if (k < 0) {
      if (!text) return node;
      return { ...node, pre_actions: [{ type: "tts_say", text }, ...actions] };
    }
    if (actions[k].text === text) return node;
    const copy = actions.slice();
    if (text) copy[k] = { ...actions[k], text } as AnyAction;
    else copy.splice(k, 1);
    return { ...node, pre_actions: copy };
  });
}

export function setActions(
  doc: AgentConfig,
  i: number,
  field: ActionField,
  actions: AnyAction[],
): AgentConfig {
  return mapNode(doc, i, (node) => ({ ...node, [field]: actions }));
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch (e) {
    throw new EditError(`Invalid JSON: ${e instanceof Error ? e.message : e}`);
  }
}

/** Parse a raw actions editor. Only the shape is checked here (an array of objects
 * with a string `type`); whether each action can run is the validator's job. */
export function parseActions(text: string): AnyAction[] {
  const value = parseJson(text);
  if (!Array.isArray(value)) throw new EditError("Actions must be a JSON array.");
  value.forEach((action, k) => {
    if (!isObject(action) || typeof action.type !== "string")
      throw new EditError(`Action ${k + 1} must be an object with a string "type".`);
  });
  return value as AnyAction[];
}

// ---- edges ------------------------------------------------------------------------

export type EdgePatch = Patch<Pick<Edge, "description" | "target">>;

export function updateEdge(doc: AgentConfig, i: number, j: number, patch: EdgePatch): AgentConfig {
  return mapEdge(doc, i, j, (edge) => assign(edge, patch));
}

/** Set or (with null/"") remove the named guard an edge checks before transitioning. */
export function setEdgeGuard(
  doc: AgentConfig,
  i: number,
  j: number,
  guard: string | null,
): AgentConfig {
  return mapEdge(doc, i, j, (edge) => assign(edge, { guard: guard || undefined }));
}

export function renameFunction(doc: AgentConfig, i: number, j: number, name: string): AgentConfig {
  const edges = doc.nodes[i]?.edges ?? [];
  if (edges[j]?.function === name) return doc;
  if (!name) throw new EditError("A function name can't be empty.");
  if (edges.some((e, k) => k !== j && e.function === name))
    throw new EditError(`This node already has a function named '${name}'.`);
  return mapEdge(doc, i, j, (edge) => ({ ...edge, function: name }));
}

/** `go_to_<target>`, made a valid and unique function name within the node. */
export function edgeFunctionName(node: Node, target: string): string {
  const base = `go_to_${target}`.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 64);
  const taken = new Set((node.edges ?? []).map((e) => e.function));
  if (!taken.has(base)) return base;
  for (let n = 2; ; n++) {
    const suffix = `_${n}`;
    const name = base.slice(0, 64 - suffix.length) + suffix;
    if (!taken.has(name)) return name;
  }
}

export function connect(
  doc: AgentConfig,
  source: number,
  target: string,
): { doc: AgentConfig; edge: number } {
  const node = doc.nodes[source];
  if (!node) throw new EditError("That node no longer exists.");
  if (node.name === target) throw new EditError("An edge can't target its own node.");
  const edge: Edge = {
    function: edgeFunctionName(node, target),
    description: `Call this when the conversation should move on to ${target}.`,
    target,
  };
  const edges = [...(node.edges ?? []), edge];
  return { doc: mapNode(doc, source, (n) => ({ ...n, edges })), edge: edges.length - 1 };
}

// ---- edge properties (the variables an edge collects) -------------------------------

export const FORM_TYPES = ["string", "number", "integer", "boolean"] as const;
// Keywords that give a property structure the table can't show.
const STRUCTURAL_KEYWORDS = [
  "items",
  "prefixItems",
  "properties",
  "additionalProperties",
  "anyOf",
  "oneOf",
  "allOf",
  "not",
  "$ref",
];

/** Why a property needs the raw JSON editor instead of the table, or null. */
export function rawOnlyReason(schema: PropertySchema): string | null {
  const { type } = schema;
  if (type != null && !(FORM_TYPES as readonly unknown[]).includes(type))
    return Array.isArray(type) ? "a union type" : `type '${type}'`;
  const keyword = STRUCTURAL_KEYWORDS.find((k) => k in schema);
  if (keyword) return `'${keyword}'`;
  if (schema.enum != null) {
    const roundTrips = schema.enum.every((v) =>
      typeof v === "string"
        ? v !== "" && !v.includes(",") && v.trim() === v
        : typeof v === "number" || typeof v === "boolean",
    );
    if (!roundTrips) return "enum values the table can't edit";
  }
  return null;
}

/** Keywords the table doesn't show; they survive form edits. */
export function otherKeywords(schema: PropertySchema): string[] {
  return Object.keys(schema).filter((k) => !["type", "description", "enum"].includes(k));
}

export function formatEnum(values: PropertySchema["enum"]): string {
  return (values ?? []).map(String).join(", ");
}

/** Parse comma-separated enum values for the property's type; empty -> no enum. */
export function parseEnum(text: string, type: PropertySchema["type"]): unknown[] | undefined {
  const parts = text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (parts.length === 0) return undefined;
  if (type === "number" || type === "integer") {
    return parts.map((p) => {
      const n = Number(p);
      if (!Number.isFinite(n) || (type === "integer" && !Number.isInteger(n)))
        throw new EditError(`'${p}' is not ${type === "integer" ? "an integer" : "a number"}.`);
      return n;
    });
  }
  if (type === "boolean") {
    return parts.map((p) => {
      if (p !== "true" && p !== "false") throw new EditError(`'${p}' is not true or false.`);
      return p === "true";
    });
  }
  return parts;
}

/** Parse a raw property schema: an object whose table keywords have valid shapes. */
export function parsePropertySchema(text: string): PropertySchema {
  const value = parseJson(text);
  if (!isObject(value)) throw new EditError("A property schema must be a JSON object.");
  const { type, description, enum: values } = value;
  const typeOk =
    type == null ||
    typeof type === "string" ||
    (Array.isArray(type) && type.every((t) => typeof t === "string"));
  if (!typeOk) throw new EditError('"type" must be a string or an array of strings.');
  if (description != null && typeof description !== "string")
    throw new EditError('"description" must be a string.');
  if (values != null && !Array.isArray(values)) throw new EditError('"enum" must be an array.');
  return value as PropertySchema;
}

function mapProperties(
  doc: AgentConfig,
  i: number,
  j: number,
  fn: (properties: Record<string, PropertySchema>, edge: Edge) => Partial<Edge> | null,
): AgentConfig {
  return mapEdge(doc, i, j, (edge) => {
    const patch = fn(edge.properties ?? {}, edge);
    return patch ? { ...edge, ...patch } : edge;
  });
}

export function addProperty(doc: AgentConfig, i: number, j: number): AgentConfig {
  return mapProperties(doc, i, j, (properties) => {
    const name = uniqueName("field", new Set(Object.keys(properties)));
    return { properties: { ...properties, [name]: { type: "string" } } };
  });
}

export function removeProperty(doc: AgentConfig, i: number, j: number, name: string): AgentConfig {
  return mapProperties(doc, i, j, (properties, edge) => {
    if (!(name in properties)) return null;
    const { [name]: _removed, ...rest } = properties;
    const patch: Partial<Edge> = { properties: rest };
    if (edge.required?.includes(name)) patch.required = edge.required.filter((r) => r !== name);
    return patch;
  });
}

/** Rename a property in place (keeping key order) and in `required`. */
export function renameProperty(
  doc: AgentConfig,
  i: number,
  j: number,
  old: string,
  name: string,
): AgentConfig {
  return mapProperties(doc, i, j, (properties, edge) => {
    if (name === old) return null;
    if (!name.trim()) throw new EditError("A property name can't be empty.");
    if (name in properties) throw new EditError(`A property named '${name}' already exists.`);
    const renamed = Object.fromEntries(
      Object.entries(properties).map(([k, v]) => [k === old ? name : k, v]),
    );
    const patch: Partial<Edge> = { properties: renamed };
    if (edge.required?.includes(old))
      patch.required = edge.required.map((r) => (r === old ? name : r));
    return patch;
  });
}

export function updateProperty(
  doc: AgentConfig,
  i: number,
  j: number,
  name: string,
  patch: Patch<Pick<PropertySchema, "type" | "description" | "enum">>,
): AgentConfig {
  return mapProperties(doc, i, j, (properties) => {
    const schema = properties[name];
    if (!schema) throw new EditError(`Property '${name}' no longer exists.`);
    const next = assign(schema, patch);
    return next === schema ? null : { properties: { ...properties, [name]: next } };
  });
}

export function setPropertySchema(
  doc: AgentConfig,
  i: number,
  j: number,
  name: string,
  schema: PropertySchema,
): AgentConfig {
  return mapProperties(doc, i, j, (properties) => ({
    properties: { ...properties, [name]: schema },
  }));
}

export function setRequired(
  doc: AgentConfig,
  i: number,
  j: number,
  name: string,
  required: boolean,
): AgentConfig {
  return mapProperties(doc, i, j, (_, edge) => {
    const current = edge.required ?? [];
    if (current.includes(name) === required) return null;
    return { required: required ? [...current, name] : current.filter((r) => r !== name) };
  });
}
