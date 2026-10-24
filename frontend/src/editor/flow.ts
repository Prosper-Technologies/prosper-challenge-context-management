// The canvas is a view of the document: `toFlow` derives React Flow nodes and
// edges from it, and canvas events map back to document actions (never a reverse
// serialization of the React Flow graph).
//
// Node ids are node names and edge ids are `${node}::${function}`, so they stay
// stable across unrelated edits and change only on a rename. Documents with
// colliding names never reach the canvas (see `identifierProblems`).

import type { Edge as FlowEdge, Node as FlowNode } from "@xyflow/react";
import type { AgentConfig } from "../generated/contract";

export type NodeData = { index: number };
export type EdgeData = { node: number; edge: number };
export type AgentFlowNode = FlowNode<NodeData, "agent">;
export type AgentFlowEdge = FlowEdge<EdgeData>;

export const edgeId = (node: string, fn: string) => `${node}::${fn}`;

export function toFlow(doc: AgentConfig): { nodes: AgentFlowNode[]; edges: AgentFlowEdge[] } {
  const names = new Set(doc.nodes.map((n) => n.name));
  const nodes: AgentFlowNode[] = doc.nodes.map((node, index) => ({
    id: node.name,
    type: "agent",
    position: node.ui ? { x: node.ui.x, y: node.ui.y } : { x: 0, y: 0 },
    data: { index },
  }));
  // Dangling edges and self-loops (both validation errors) stay off the canvas;
  // the issue list and the node inspector still reach them.
  const edges: AgentFlowEdge[] = doc.nodes.flatMap((node, i) =>
    (node.edges ?? []).flatMap((edge, j) =>
      names.has(edge.target) && edge.target !== node.name
        ? [
            {
              id: edgeId(node.name, edge.function),
              source: node.name,
              target: edge.target,
              label: edge.function,
              data: { node: i, edge: j },
            },
          ]
        : [],
    ),
  );
  return { nodes, edges };
}
