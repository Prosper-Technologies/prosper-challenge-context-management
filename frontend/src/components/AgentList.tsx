// Left sidebar: saved agents (new, duplicate, delete) and "Add node". Switching
// agents is disabled during a test call.

import { useReactFlow } from "@xyflow/react";
import { useState } from "react";
import * as d from "../editor/document";
import { NODE_HEIGHT, NODE_WIDTH } from "../editor/layout";
import { deleteAgent, duplicateAgent, newAgent, openAgent } from "../editor/session";
import { editorStore, repairProblems, useEditor } from "../editor/store";
import { Button } from "./fields";

export default function AgentList() {
  const agents = useEditor((s) => s.agents);
  const agentId = useEditor((s) => s.agentId);
  const saved = useEditor((s) => s.saved);
  const openName = useEditor((s) => s.doc?.name);
  const locked = useEditor((s) => s.locked);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  const items = agents.map((a) => ({ id: a.id, name: a.name ?? a.id, saved: true }));
  if (agentId && !saved) items.push({ id: agentId, name: openName || agentId, saved: false });

  const create = () => {
    if (name.trim()) newAgent(name.trim());
    setCreating(false);
    setName("");
  };

  return (
    <aside className="flex w-56 shrink-0 flex-col gap-3 border-r border-slate-200 bg-white p-3">
      <AddNodeButton />
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Agents</h2>
        <div className="flex">
          <Button tone="ghost" disabled={locked} onClick={() => setCreating(true)}>
            + New
          </Button>
          <Button tone="ghost" disabled={locked || !agentId} onClick={duplicateAgent} title="Duplicate the open agent">
            Copy
          </Button>
        </div>
      </div>
      {creating && (
        <input
          autoFocus
          className="rounded border border-slate-300 px-2 py-1 text-sm"
          placeholder="Agent name, then Enter"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={create}
          onKeyDown={(e) => {
            if (e.key === "Enter") create();
            if (e.key === "Escape") {
              setName("");
              setCreating(false);
            }
          }}
        />
      )}
      <ul className="flex flex-col gap-0.5 overflow-y-auto">
        {items.map((item) => (
          <li key={item.id} className="group flex items-center">
            <button
              type="button"
              disabled={locked && item.id !== agentId}
              className={`min-w-0 flex-1 truncate rounded px-2 py-1 text-left text-sm disabled:opacity-40 ${
                item.id === agentId ? "bg-slate-900 text-white" : "hover:bg-slate-100"
              }`}
              onClick={() => item.id !== agentId && void openAgent(item.id)}
            >
              {item.name}
              {!item.saved && <span className="ml-1 text-xs opacity-70">(unsaved)</span>}
            </button>
            <button
              type="button"
              title={`Delete ${item.id}`}
              disabled={locked}
              className="ml-1 hidden rounded px-1 text-xs text-slate-400 group-hover:block hover:text-red-600 disabled:opacity-40"
              onClick={() => void deleteAgent(item.id)}
            >
              ✕
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}

/** Add a node at the centre of the visible canvas and select it. */
function AddNodeButton() {
  const flow = useReactFlow();
  const locked = useEditor((s) => s.locked);
  const canvas = useEditor((s) => s.doc !== null && repairProblems(s.doc).length === 0 && s.view === "canvas");

  const add = () => {
    const pane = document.querySelector(".react-flow")?.getBoundingClientRect();
    const center = pane
      ? flow.screenToFlowPosition({ x: pane.x + pane.width / 2, y: pane.y + pane.height / 2 })
      : { x: 0, y: 0 };
    const position = { x: center.x - NODE_WIDTH / 2, y: center.y - NODE_HEIGHT / 2 };
    editorStore.getState().edit((doc) => {
      const { doc: next, index } = d.addNode(doc, position);
      return [next, { kind: "node", node: index }];
    });
  };

  return (
    <Button tone="primary" disabled={locked || !canvas} onClick={add}>
      + Add node
    </Button>
  );
}
