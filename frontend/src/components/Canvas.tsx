// The graph canvas: a controlled React Flow view of the document. Canvas events
// become document actions (connect, delete, a finished drag) or transient state
// (selection, in-progress drag positions, measured sizes). A drag commits once,
// on drop, so it is a single undo step and never triggers validation. "Tidy
// layout" re-lays out every node as one step. A node's badge counts the catalog
// tools it exposes.

import {
  Background,
  type Connection,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  type NodeChange,
  type NodeProps,
  Panel,
  Position,
  ReactFlow,
  useReactFlow,
} from "@xyflow/react";
import { memo, useCallback, useMemo, useState } from "react";
import * as d from "../editor/document";
import type { Node as NodeConfig } from "../generated/contract";
import { type AgentFlowEdge, type AgentFlowNode, edgeId, toFlow } from "../editor/flow";
import { NODE_WIDTH, tidyLayout } from "../editor/layout";
import { editorStore, useEditor } from "../editor/store";
import { issuesOf, worstLevel } from "../editor/validation";
import { Button } from "./fields";

const nodeTypes = { agent: memo(AgentNode) };

type Size = { width: number; height: number };

/** `active`: the agents screen is showing. React Flow listens for Delete on the
 * whole document, so a hidden canvas must not delete its selection. */
export default function Canvas({ active }: { active: boolean }) {
  const doc = useEditor((s) => s.doc)!;
  const selection = useEditor((s) => s.selection);
  const locked = useEditor((s) => s.locked);
  const tracedEdges = useEditor((s) => s.trace.edges);
  const activeNode = useEditor((s) => s.trace.active);
  const live = activeNode !== null;
  const { edit, select, notify } = editorStore.getState();
  const { fitView } = useReactFlow();

  const base = useMemo(() => toFlow(doc), [doc]);
  const [measured, setMeasured] = useState<Record<string, Size>>({});
  const [dragging, setDragging] = useState<Record<string, d.XY>>({});

  const selectedNode = selection.kind === "node" ? doc.nodes[selection.node]?.name : undefined;
  const source = selection.kind === "edge" ? doc.nodes[selection.node] : undefined;
  const selectedFn = selection.kind === "edge" ? source?.edges?.[selection.edge]?.function : undefined;
  const selectedEdge = source && selectedFn !== undefined ? edgeId(source.name, selectedFn) : undefined;

  const nodes = useMemo<AgentFlowNode[]>(
    () =>
      base.nodes.map((n) => ({
        ...n,
        position: dragging[n.id] ?? n.position,
        measured: measured[n.id],
        selected: n.id === selectedNode,
      })),
    [base.nodes, dragging, measured, selectedNode],
  );

  const edges = useMemo<AgentFlowEdge[]>(
    () =>
      base.edges.map((e) => {
        const traced = tracedEdges.has(e.id);
        const selected = e.id === selectedEdge;
        const color = selected ? "#0ea5e9" : traced ? "#059669" : "#94a3b8";
        return {
          ...e,
          selected,
          animated: traced && live,
          markerEnd: { type: MarkerType.ArrowClosed, color },
          style: selected || traced ? { stroke: color, strokeWidth: 2.5 } : undefined,
          labelStyle: { fontFamily: "ui-monospace, monospace", fontSize: 11 },
          labelBgPadding: [4, 2] as [number, number],
          labelBgBorderRadius: 4,
        };
      }),
    [base.edges, selectedEdge, tracedEdges, live],
  );

  const onNodesChange = useCallback((changes: NodeChange<AgentFlowNode>[]) => {
    const sizes: Record<string, Size> = {};
    const moves: Record<string, d.XY> = {};
    for (const change of changes) {
      if (change.type === "dimensions" && change.dimensions) sizes[change.id] = change.dimensions;
      if (change.type === "position" && change.dragging && change.position)
        moves[change.id] = change.position;
    }
    if (Object.keys(sizes).length) setMeasured((m) => ({ ...m, ...sizes }));
    if (Object.keys(moves).length) setDragging((m) => ({ ...m, ...moves }));
  }, []);

  const onNodeDragStop = useCallback(
    (_: unknown, __: AgentFlowNode, dragged: AgentFlowNode[]) => {
      edit((doc) => d.moveNodes(doc, new Map(dragged.map((n) => [n.data.index, n.position]))));
      setDragging({});
    },
    [edit],
  );

  const onConnect = useCallback(
    ({ source, target }: Connection) => {
      const refused = edit((doc) => {
        const i = doc.nodes.findIndex((n) => n.name === source);
        const { doc: next, edge } = d.connect(doc, i, target);
        return [next, { kind: "edge", node: i, edge }];
      });
      if (refused) notify({ kind: "error", text: refused });
    },
    [edit, notify],
  );

  const onDelete = useCallback(
    ({ nodes, edges }: { nodes: AgentFlowNode[]; edges: AgentFlowEdge[] }) => {
      edit((doc) => [
        d.deleteElements(
          doc,
          nodes.map((n) => n.data.index),
          edges.flatMap((e) => (e.data ? [[e.data.node, e.data.edge] as [number, number]] : [])),
        ),
        { kind: "agent" },
      ]);
    },
    [edit],
  );

  const nodeColor = useCallback(
    (n: AgentFlowNode) => minimapColor(doc.nodes[n.data.index], doc.initial_node, activeNode),
    [doc, activeNode],
  );

  const tidy = () => {
    edit(tidyLayout);
    // After React Flow has the new positions.
    requestAnimationFrame(() => void fitView({ padding: 0.2, maxZoom: 1, duration: 300 }));
  };

  return (
    <ReactFlow<AgentFlowNode, AgentFlowEdge>
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodesChange={onNodesChange}
      onNodeDragStop={onNodeDragStop}
      onConnect={onConnect}
      onDelete={onDelete}
      isValidConnection={(c) => c.source !== c.target}
      onNodeClick={(_, n) => select({ kind: "node", node: n.data.index })}
      onEdgeClick={(_, e) => e.data && select({ kind: "edge", node: e.data.node, edge: e.data.edge })}
      onPaneClick={() => select({ kind: "agent" })}
      nodesDraggable={!locked}
      nodesConnectable={!locked}
      deleteKeyCode={locked || !active ? null : ["Backspace", "Delete"]}
      multiSelectionKeyCode={null}
      selectionKeyCode={null}
      fitView
      fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
    >
      <Background />
      <Controls showInteractive={false} />
      <MiniMap<AgentFlowNode>
        pannable
        zoomable
        nodeColor={nodeColor}
        className="rounded border border-slate-200"
      />
      <Panel position="top-right">
        <Button
          disabled={locked || doc.nodes.length < 2}
          title="Re-arrange every node top to bottom (one undo step)"
          onClick={tidy}
        >
          Tidy layout
        </Button>
      </Panel>
      {doc.nodes.length === 0 && (
        <Panel position="top-center">
          <p className="mt-16 rounded bg-white px-4 py-3 text-sm text-slate-600 shadow-sm">
            This agent has no nodes yet. Use “+ Add node” on the left to create the first one.
          </p>
        </Panel>
      )}
    </ReactFlow>
  );
}

/** Minimap colours follow the canvas: the active node, then start and end nodes. */
function minimapColor(node: NodeConfig | undefined, initial: string, active: string | null): string {
  if (node?.name === active) return "#059669";
  if (node?.name === initial) return "#6ee7b7";
  return node?.end ? "#334155" : "#cbd5e1";
}

function AgentNode({ data, selected }: NodeProps<AgentFlowNode>) {
  const { index } = data;
  const node = useEditor((s) => s.doc?.nodes[index]);
  const name = node?.name ?? "";
  const initial = useEditor((s) => s.doc?.initial_node === name);
  const level = useEditor((s) =>
    worstLevel(issuesOf(s.validation).filter((x) => x.path[0] === "nodes" && x.path[1] === index)),
  );
  const active = useEditor((s) => s.trace.active === name);
  const visited = useEditor((s) => s.trace.nodes.has(name));
  if (!node) return null;

  const tools = node.tools?.length ?? 0;
  const task = node.task_messages?.find((m) => typeof m.content === "string")?.content;
  const border = active
    ? "border-emerald-600 ring-4 ring-emerald-200"
    : visited
      ? "border-emerald-400"
      : node.end
        ? "border-slate-700"
        : "border-slate-300";

  return (
    <div
      style={{ width: NODE_WIDTH }}
      className={`rounded-lg border-2 bg-white px-3 py-2 shadow-sm ${border} ${
        selected ? "outline-2 outline-offset-2 outline-sky-500" : ""
      } ${node.end ? "bg-slate-50" : ""}`}
    >
      <Handle type="target" position={Position.Top} className="h-2.5! w-2.5! bg-slate-500!" />
      <div className="flex items-center gap-1.5">
        {initial && (
          <span className="rounded bg-emerald-600 px-1.5 text-[10px] font-semibold text-white">
            START
          </span>
        )}
        {node.end && (
          <span className="rounded bg-slate-700 px-1.5 text-[10px] font-semibold text-white">
            END
          </span>
        )}
        <span className="truncate font-medium">{name}</span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          {tools > 0 && (
            <span
              title={node.tools!.join(", ")}
              className="rounded bg-sky-100 px-1.5 text-[10px] font-medium text-sky-800"
            >
              {tools} tool{tools > 1 ? "s" : ""}
            </span>
          )}
          {level && (
            <span
              title={level === "error" ? "Has errors" : "Has warnings"}
              className={`h-2.5 w-2.5 rounded-full ${level === "error" ? "bg-red-500" : "bg-amber-400"}`}
            />
          )}
        </span>
      </div>
      <p className="mt-1 line-clamp-2 text-xs text-slate-500">
        {typeof task === "string" && task ? task : <span className="italic">No task yet</span>}
      </p>
      <Handle
        type="source"
        position={Position.Bottom}
        isConnectable={!node.end}
        className="h-2.5! w-2.5! bg-slate-500!"
      />
    </div>
  );
}
