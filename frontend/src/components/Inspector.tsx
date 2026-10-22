// The right-hand inspector: agent settings, or the selected node or edge.
// Forms edit only the fields they show; everything else in the document (extra
// fields, message roles, schema keywords, other actions) is kept as-is.

import type { AgentConfig, Edge, Node, PropertySchema } from "../generated/contract";
import { registryData, useResource } from "../catalog/data";
import * as d from "../editor/document";
import { useEditor } from "../editor/store";
import { agentIssues, edgeIssues, issuesOf, nodeIssues } from "../editor/validation";
import {
  Button,
  Checkbox,
  IssueList,
  JsonField,
  Section,
  SelectField,
  TextField,
} from "./fields";

export default function Inspector() {
  const selection = useEditor((s) => s.selection);
  const doc = useEditor((s) => s.doc);
  if (!doc) return null;

  // Keyed by what's selected, so field buffers never carry over to another item.
  if (selection.kind === "node" && doc.nodes[selection.node])
    return <NodeInspector key={`n${selection.node}`} index={selection.node} />;
  if (selection.kind === "edge" && doc.nodes[selection.node]?.edges?.[selection.edge])
    return (
      <EdgeInspector
        key={`e${selection.node}:${selection.edge}`}
        node={selection.node}
        edge={selection.edge}
      />
    );
  return <AgentSettings />;
}

function useEdit() {
  return useEditor((s) => s.edit);
}

function useIssues() {
  return useEditor((s) => issuesOf(s.validation));
}

function Header({ kind, title, onDelete }: { kind: string; title: string; onDelete?: () => void }) {
  const locked = useEditor((s) => s.locked);
  return (
    <div className="flex items-start justify-between gap-2">
      <div className="min-w-0">
        <div className="text-xs uppercase tracking-wide text-slate-400">{kind}</div>
        <h2 className="truncate font-semibold">{title}</h2>
      </div>
      {onDelete && (
        <Button tone="danger" disabled={locked} onClick={onDelete}>
          Delete
        </Button>
      )}
    </div>
  );
}

// ---- agent ------------------------------------------------------------------------

function AgentSettings() {
  const doc = useEditor((s) => s.doc) as AgentConfig;
  const edit = useEdit();
  const issues = agentIssues(useIssues());
  const names = doc.nodes.map((n) => n.name);
  const set = (patch: d.AgentPatch) => edit((doc) => d.updateAgent(doc, patch));

  return (
    <div className="flex flex-col gap-3">
      <Header kind="Agent" title={doc.name || "Untitled agent"} />
      <IssueList issues={issues} />
      <TextField label="Name" value={doc.name} onCommit={(name) => set({ name })} />
      <TextField
        label="Persona"
        hint="the role message every node starts from"
        value={doc.persona ?? ""}
        onCommit={(persona) => set({ persona })}
        multiline
        rows={6}
      />
      <SelectField
        label="Initial node"
        value={doc.initial_node}
        options={[
          ...(names.includes(doc.initial_node)
            ? []
            : [{ value: doc.initial_node, label: `${doc.initial_node || "(none)"} — missing` }]),
          ...names.map((n) => ({ value: n, label: n })),
        ]}
        onChange={(initial_node) => set({ initial_node })}
      />
      <TextField
        label="Voice ID"
        hint="ElevenLabs"
        value={doc.voice_id ?? ""}
        placeholder="default"
        onCommit={(v) => set({ voice_id: v.trim() || undefined })}
      />
      <TextField
        label="Model"
        hint="OpenAI"
        value={doc.model ?? ""}
        placeholder="default"
        onCommit={(v) => set({ model: v.trim() || undefined })}
      />
    </div>
  );
}

// ---- node ---------------------------------------------------------------------------

function NodeInspector({ index }: { index: number }) {
  const node = useEditor((s) => s.doc!.nodes[index]) as Node;
  const initial = useEditor((s) => s.doc!.initial_node === node.name);
  const locked = useEditor((s) => s.locked);
  const select = useEditor((s) => s.select);
  const edit = useEdit();
  const issues = nodeIssues(useIssues(), index);
  const say = d.onEntrySay(node);

  return (
    <div className="flex flex-col gap-3">
      <Header
        kind={initial ? "Start node" : node.end ? "End node" : "Node"}
        title={node.name}
        onDelete={() => edit((doc) => [d.deleteElements(doc, [index], []), { kind: "agent" }])}
      />
      <IssueList issues={issues} />
      <TextField
        label="Name"
        hint="edges and the initial node follow a rename"
        value={node.name}
        onCommit={(name) => edit((doc) => d.renameNode(doc, index, name))}
      />
      <div className="flex items-center justify-between">
        <Checkbox
          label="End the call after this node"
          checked={node.end === true}
          onChange={(end) => edit((doc) => d.updateNode(doc, index, { end: end || undefined }))}
        />
        {!initial && (
          <Button
            tone="ghost"
            disabled={locked}
            onClick={() => edit((doc) => d.updateAgent(doc, { initial_node: node.name }))}
          >
            Make start
          </Button>
        )}
      </div>

      <Section
        title="Task messages"
        action={
          <Button tone="ghost" disabled={locked} onClick={() => edit((doc) => d.addMessage(doc, index))}>
            + Add
          </Button>
        }
      >
        {(node.task_messages ?? []).length === 0 && (
          <p className="text-xs text-slate-400">No task messages: what should this node do?</p>
        )}
        {(node.task_messages ?? []).map((message, k) => (
          <div key={k} className="flex flex-col gap-1">
            <div className="flex items-center justify-between">
              <span className="font-mono text-[11px] text-slate-400">{message.role}</span>
              <Button
                tone="ghost"
                disabled={locked}
                onClick={() => edit((doc) => d.removeMessage(doc, index, k))}
              >
                Remove
              </Button>
            </div>
            {typeof message.content === "string" ? (
              <TextField
                value={message.content}
                onCommit={(content) => edit((doc) => d.setMessageContent(doc, index, k, content))}
                multiline
                rows={4}
                placeholder="What the agent should do in this node."
              />
            ) : (
              <pre className="max-h-32 overflow-auto rounded bg-slate-100 p-2 text-[11px]">
                {JSON.stringify(message.content, null, 2)}
                {"\n"}(structured content: edit it in the JSON view)
              </pre>
            )}
          </div>
        ))}
      </Section>

      <Section title="Behaviour">
        <TextField
          label="Role message override"
          hint="replaces the agent persona here"
          value={node.role_message ?? ""}
          onCommit={(text) => edit((doc) => d.updateNode(doc, index, { role_message: text || undefined }))}
          multiline
        />
        {say.editable ? (
          <TextField
            label="On entry, say…"
            hint="spoken before the LLM replies"
            value={say.text}
            onCommit={(text) => edit((doc) => d.setOnEntrySay(doc, index, text))}
          />
        ) : (
          <p className="text-xs text-slate-500">
            The first <code>tts_say</code> pre-action is malformed; fix it in the pre-actions below.
          </p>
        )}
        <JsonField
          label="Pre-actions"
          hint="JSON, run on entry"
          value={node.pre_actions}
          parse={d.parseActions}
          onCommit={(actions) => edit((doc) => d.setActions(doc, index, "pre_actions", actions))}
        />
        <JsonField
          label="Post-actions"
          hint="JSON, run after entry"
          value={node.post_actions}
          parse={d.parseActions}
          onCommit={(actions) => edit((doc) => d.setActions(doc, index, "post_actions", actions))}
        />
      </Section>

      <NodeTools index={index} tools={node.tools} />

      <Section title="Transitions">
        {(node.edges ?? []).length === 0 ? (
          <p className="text-xs text-slate-400">
            Drag from this node's bottom handle to another node to add one.
          </p>
        ) : (
          <ul className="flex flex-col gap-1">
            {(node.edges ?? []).map((edge, j) => (
              <li key={j}>
                <button
                  type="button"
                  className="w-full rounded border border-slate-200 px-2 py-1 text-left text-xs hover:bg-slate-50"
                  onClick={() => select({ kind: "edge", node: index, edge: j })}
                >
                  <span className="font-mono">{edge.function}</span>
                  <span className="text-slate-400"> → {edge.target}</span>
                  {edge.guard && <span className="ml-1 text-sky-700">if {edge.guard}</span>}
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

const firstSentence = (text: string) => text.split(/(?<=\.)\s/)[0];

/** One checkbox per registry tool, plus any the node names that the registry
 * doesn't have (the validator reports those), so they can be unticked. */
function NodeTools({ index, tools }: { index: number; tools: string[] | undefined }) {
  const registry = useResource(registryData);
  const edit = useEdit();
  const listed = tools ?? [];
  const known = registry.status === "ready" ? registry.data.tools : [];
  const unknown = listed.filter((t) => !known.some((k) => k.name === t));
  const toggle = (name: string, enabled: boolean) =>
    edit((doc) => d.setNodeTools(doc, index, d.withTool(doc.nodes[index]?.tools, name, enabled)));

  return (
    <Section title="Tools">
      <p className="text-[11px] text-slate-400">
        Catalog tools the LLM can call here without leaving the node.
      </p>
      {registry.status === "failed" && (
        <p className="text-xs text-red-700">
          Couldn't load the tool list: {registry.error}{" "}
          <button type="button" className="underline" onClick={registryData.retry}>
            Retry
          </button>
        </p>
      )}
      {registry.status === "loading" && listed.length === 0 && (
        <p className="text-xs text-slate-400">Loading tools…</p>
      )}
      {known.map((tool) => (
        <Checkbox
          key={tool.name}
          label={tool.name}
          hint={firstSentence(tool.description)}
          mono
          checked={listed.includes(tool.name)}
          onChange={(on) => toggle(tool.name, on)}
        />
      ))}
      {unknown.map((name) => (
        <Checkbox
          key={name}
          label={name}
          hint={registry.status === "ready" ? "not a known tool" : undefined}
          mono
          checked
          onChange={(on) => toggle(name, on)}
        />
      ))}
    </Section>
  );
}

/** The edge's guard: none, a registry guard, or (kept selectable) an unknown name. */
function GuardField({ node: i, edge: j, guard }: { node: number; edge: number; guard: string }) {
  const registry = useResource(registryData);
  const edit = useEdit();
  const guards = registry.status === "ready" ? registry.data.guards : [];
  const current = guards.find((g) => g.name === guard);
  const options = [
    { value: "", label: "none" },
    ...(guard && !current
      ? [{ value: guard, label: `${guard}${registry.status === "ready" ? " — unknown" : ""}` }]
      : []),
    ...guards.map((g) => ({ value: g.name, label: g.name })),
  ];

  return (
    <div className="flex flex-col gap-1">
      <SelectField
        label="Guard"
        hint="must pass before the transition"
        value={guard}
        options={options}
        onChange={(next) => edit((doc) => d.setEdgeGuard(doc, i, j, next || null))}
      />
      {current && <p className="text-[11px] text-slate-500">{current.description}</p>}
      {registry.status === "failed" && (
        <p className="text-xs text-red-700">
          Couldn't load the guards: {registry.error}{" "}
          <button type="button" className="underline" onClick={registryData.retry}>
            Retry
          </button>
        </p>
      )}
    </div>
  );
}

// ---- edge ---------------------------------------------------------------------------

function EdgeInspector({ node: i, edge: j }: { node: number; edge: number }) {
  const source = useEditor((s) => s.doc!.nodes[i]) as Node;
  const names = useEditor((s) => s.doc!.nodes.map((n) => n.name).join("\n")).split("\n");
  const locked = useEditor((s) => s.locked);
  const select = useEditor((s) => s.select);
  const edit = useEdit();
  const issues = edgeIssues(useIssues(), i, j);
  const edge = source.edges![j] as Edge;
  const properties = Object.entries(edge.properties ?? {});
  const targets = names.filter((n) => n !== source.name);

  return (
    <div className="flex flex-col gap-3">
      <Header
        kind="Edge"
        title={`${source.name} → ${edge.target}`}
        onDelete={() =>
          edit((doc) => [d.deleteElements(doc, [], [[i, j]]), { kind: "node", node: i }])
        }
      />
      <IssueList issues={issues} />
      <TextField
        label="Function name"
        hint="the tool the LLM calls"
        value={edge.function}
        onCommit={(name) => edit((doc) => d.renameFunction(doc, i, j, name))}
        mono
      />
      <TextField
        label="Description"
        hint="when the LLM should call it"
        value={edge.description}
        onCommit={(description) => edit((doc) => d.updateEdge(doc, i, j, { description }))}
        multiline
      />
      <SelectField
        label="Target"
        value={edge.target}
        options={[
          ...(targets.includes(edge.target)
            ? []
            : [{ value: edge.target, label: `${edge.target || "(none)"} — invalid` }]),
          ...targets.map((n) => ({ value: n, label: n })),
        ]}
        onChange={(target) => edit((doc) => d.updateEdge(doc, i, j, { target }))}
      />
      <GuardField node={i} edge={j} guard={edge.guard ?? ""} />
      <button
        type="button"
        className="self-start text-xs text-slate-500 underline"
        onClick={() => select({ kind: "node", node: i })}
      >
        Back to {source.name}
      </button>

      <Section
        title="Variables to collect"
        action={
          <Button tone="ghost" disabled={locked} onClick={() => edit((doc) => d.addProperty(doc, i, j))}>
            + Add
          </Button>
        }
      >
        {properties.length === 0 && (
          <p className="text-xs text-slate-400">
            None. Add one to have the LLM pass a value (e.g. the caller's name) on this edge.
          </p>
        )}
        {properties.map(([name, schema]) => (
          <PropertyCard
            key={name}
            node={i}
            edge={j}
            name={name}
            schema={schema}
            required={edge.required?.includes(name) ?? false}
          />
        ))}
      </Section>
    </div>
  );
}

function PropertyCard({
  node: i,
  edge: j,
  name,
  schema,
  required,
}: {
  node: number;
  edge: number;
  name: string;
  schema: PropertySchema;
  required: boolean;
}) {
  const locked = useEditor((s) => s.locked);
  const edit = useEdit();
  const rawReason = d.rawOnlyReason(schema);
  const others = d.otherKeywords(schema);
  const update = (patch: Parameters<typeof d.updateProperty>[4]) =>
    edit((doc) => d.updateProperty(doc, i, j, name, patch));

  return (
    <div className="flex flex-col gap-2 rounded border border-slate-200 p-2">
      <div className="flex items-start gap-2">
        <div className="flex-1">
          <TextField
            value={name}
            onCommit={(next) => edit((doc) => d.renameProperty(doc, i, j, name, next))}
            mono
          />
        </div>
        <Button
          tone="ghost"
          disabled={locked}
          onClick={() => edit((doc) => d.removeProperty(doc, i, j, name))}
        >
          Remove
        </Button>
      </div>
      {rawReason ? (
        <JsonField
          label="Schema"
          hint={`JSON: ${rawReason} needs the raw editor`}
          value={schema}
          parse={d.parsePropertySchema}
          onCommit={(next) => edit((doc) => d.setPropertySchema(doc, i, j, name, next))}
          rows={5}
        />
      ) : (
        <>
          <SelectField
            label="Type"
            value={typeof schema.type === "string" ? schema.type : ""}
            options={[
              { value: "", label: "any" },
              ...d.FORM_TYPES.map((t) => ({ value: t, label: t })),
            ]}
            onChange={(type) => update({ type: type || undefined })}
          />
          <TextField
            label="Allowed values"
            hint="comma-separated; empty for any"
            value={d.formatEnum(schema.enum)}
            onCommit={(text) => {
              try {
                return update({ enum: d.parseEnum(text, schema.type) });
              } catch (e) {
                return e instanceof Error ? e.message : String(e);
              }
            }}
          />
          <TextField
            label="Description"
            value={schema.description ?? ""}
            onCommit={(description) => update({ description: description || undefined })}
          />
          {others.length > 0 && (
            <p className="text-[11px] text-slate-400">Also keeps: {others.join(", ")}</p>
          )}
        </>
      )}
      <Checkbox
        label="Required"
        checked={required}
        onChange={(checked) => edit((doc) => d.setRequired(doc, i, j, name, checked))}
      />
    </div>
  );
}
