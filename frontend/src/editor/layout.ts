// Canvas positions. Layout runs when a document is loaded or replaced and some
// nodes have no `ui` position; it never moves a node that already has one, and
// editing prompts never reruns it.

import dagre from "@dagrejs/dagre";
import type { AgentConfig } from "../generated/contract";
import type { XY } from "./document";

// Matches the node card's size on the canvas (AgentNode).
export const NODE_WIDTH = 240;
export const NODE_HEIGHT = 96;
const GAP = 80;

/** Top-left positions for every node, by index, from a top-to-bottom dagre layout. */
export function layoutPositions(doc: AgentConfig): XY[] {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 60, ranksep: GAP });
  g.setDefaultEdgeLabel(() => ({}));

  const byName = new Map<string, string>();
  doc.nodes.forEach((node, i) => {
    g.setNode(`${i}`, { width: NODE_WIDTH, height: NODE_HEIGHT });
    if (!byName.has(node.name)) byName.set(node.name, `${i}`);
  });
  doc.nodes.forEach((node, i) => {
    for (const edge of node.edges ?? []) {
      const target = byName.get(edge.target);
      if (target !== undefined && target !== `${i}`) g.setEdge(`${i}`, target);
    }
  });
  dagre.layout(g);

  return doc.nodes.map((_, i) => {
    const { x, y } = g.node(`${i}`);
    return { x: Math.round(x - NODE_WIDTH / 2), y: Math.round(y - NODE_HEIGHT / 2) };
  });
}

/** Give nodes without a position one. With no positions at all, lay out the whole
 * graph; otherwise place the new ones, in their dagre arrangement, below the rest. */
export function layoutMissing(doc: AgentConfig): AgentConfig {
  const missing = doc.nodes.flatMap((n, i) => (n.ui ? [] : [i]));
  if (missing.length === 0) return doc;

  const layout = layoutPositions(doc);
  let offset: XY = { x: 0, y: 0 };
  const placed = doc.nodes.filter((n) => n.ui);
  if (placed.length > 0) {
    const left = Math.min(...placed.map((n) => n.ui!.x));
    const bottom = Math.max(...placed.map((n) => n.ui!.y)) + NODE_HEIGHT + GAP;
    const top = Math.min(...missing.map((i) => layout[i].y));
    const minX = Math.min(...missing.map((i) => layout[i].x));
    offset = { x: left - minX, y: bottom - top };
  }

  const missingSet = new Set(missing);
  return {
    ...doc,
    nodes: doc.nodes.map((node, i) =>
      missingSet.has(i)
        ? { ...node, ui: { x: layout[i].x + offset.x, y: layout[i].y + offset.y } }
        : node,
    ),
  };
}
