// The tool playground: run the scheduler's catalog tools by hand, in a sandbox
// session with its own calendar, and see exactly the JSON the model would get.
//
// A sandbox is created when the playground first shows and disposed of when it
// goes (or the page does); Reset disposes of it and starts a fresh one. Patient
// controls call update_booking with explicit values (null for Unknown), like the
// model does. An expired or unknown session is refused by the backend rather than
// replaced, so the last result stays on screen with an explanation until Reset.

import { type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";
import * as api from "../api";
import type {
  Catalog,
  SessionState,
  ToolRunResponse,
  ToolSpec,
} from "../generated/contract";
import { registryData, useResource } from "../catalog/data";
import type { CrossRefs } from "../catalog/navigation";
import { type FieldSpec, type FormValues, FormError, buildArgs, formFields } from "../catalog/toolForm";
import { Button } from "./fields";

type Sandbox =
  | { status: "creating" }
  | { status: "ready"; id: string; ttl: number }
  | { status: "gone"; id: string; message: string } // expired, or unknown to the backend
  | { status: "failed"; error: string };

interface Run {
  tool: string;
  args: Record<string, unknown>;
  response: ToolRunResponse;
}

const messageOf = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** A sandbox session for as long as the component is mounted; bump `generation` to
 * replace it. The previous one is disposed of on the way (also under StrictMode's
 * double effects, and on page unload). */
function useSandbox(generation: number, onState: (state: SessionState | null) => void) {
  const [sandbox, setSandbox] = useState<Sandbox>({ status: "creating" });
  const current = useRef<string | null>(null);
  const report = useRef(onState);
  report.current = onState;

  useEffect(() => {
    let cancelled = false;
    setSandbox({ status: "creating" });
    report.current(null);
    void (async () => {
      try {
        const session = await api.createPlaygroundSession();
        if (cancelled) {
          void api.deletePlaygroundSession(session.session_id).catch(() => undefined);
          return;
        }
        current.current = session.session_id;
        setSandbox({ status: "ready", id: session.session_id, ttl: session.ttl_s });
        report.current(session.state);
      } catch (e) {
        if (!cancelled) setSandbox({ status: "failed", error: messageOf(e) });
      }
    })();
    return () => {
      cancelled = true;
      const id = current.current;
      current.current = null;
      if (id) void api.deletePlaygroundSession(id).catch(() => undefined);
    };
  }, [generation]);

  useEffect(() => {
    const onPageHide = () => {
      if (current.current)
        void api.deletePlaygroundSession(current.current, { keepalive: true }).catch(() => undefined);
    };
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
  }, []);

  const markGone = (id: string, message: string) => {
    if (current.current === id) current.current = null;
    setSandbox({ status: "gone", id, message });
  };
  return { sandbox, markGone };
}

export default function ToolPlayground({ catalog, refs }: { catalog: Catalog; refs: CrossRefs }) {
  const registry = useResource(registryData);
  const [generation, setGeneration] = useState(0);
  const [state, setState] = useState<SessionState | null>(null);
  const { sandbox, markGone } = useSandbox(generation, setState);
  const [toolName, setToolName] = useState("search_services");
  const [forms, setForms] = useState<Record<string, FormValues>>({});
  const [run, setRun] = useState<Run | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tools = registry.status === "ready" ? registry.data.tools : [];
  const tool = tools.find((t) => t.name === toolName) ?? tools[0];

  const reset = () => {
    setRun(null);
    setError(null);
    setGeneration((g) => g + 1);
  };

  const call = async (name: string, args: Record<string, unknown>) => {
    if (sandbox.status !== "ready") return;
    setRunning(true);
    setError(null);
    try {
      const response = await api.runTool(name, sandbox.id, args);
      setRun({ tool: name, args, response });
      setState(response.state);
    } catch (e) {
      if (e instanceof api.ApiError && (e.status === 404 || e.status === 410))
        markGone(sandbox.id, messageOf(e).replace(/^\d+ /, ""));
      else setError(messageOf(e));
    } finally {
      setRunning(false);
    }
  };

  const submit = (spec: ToolSpec, fields: FieldSpec[]) => {
    let args: Record<string, unknown>;
    try {
      args = buildArgs(fields, forms[spec.name] ?? {});
    } catch (e) {
      if (e instanceof FormError) return setError(e.message);
      throw e;
    }
    void call(spec.name, args);
  };

  const ready = sandbox.status === "ready" && !running;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="font-semibold">Tool playground</h2>
          <p className="text-xs text-slate-500">
            The scheduler's tools, in a sandbox with its own calendar. Voice calls never see it.
          </p>
        </div>
        <Button
          disabled={sandbox.status === "creating"}
          onClick={reset}
          title="Dispose of this sandbox and start an empty one"
        >
          Reset
        </Button>
      </div>

      <SandboxStatus sandbox={sandbox} onRetry={reset} />

      {state && (
        <>
          <PatientControls
            state={state}
            disabled={!ready}
            onChange={(fact, value) => void call("update_booking", { patient: { [fact]: value } })}
          />
          <DraftView state={state} refs={refs} />
        </>
      )}

      <section className="flex flex-col gap-2 border-t border-slate-200 pt-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Run a tool</h3>
        {registry.status === "loading" && <p className="text-sm text-slate-500">Loading tools…</p>}
        {registry.status === "failed" && (
          <p className="text-sm text-red-700">
            Couldn't load the tools: {registry.error}{" "}
            <button type="button" className="underline" onClick={registryData.retry}>
              Retry
            </button>
          </p>
        )}
        {tool && (
          <ToolForm
            key={tool.name}
            tools={tools}
            tool={tool}
            onSelectTool={(name) => {
              setToolName(name);
              setError(null);
            }}
            values={forms[tool.name] ?? {}}
            onValues={(values) => setForms((f) => ({ ...f, [tool.name]: values }))}
            catalog={catalog}
            state={state}
            disabled={!ready}
            running={running}
            onRun={(fields) => submit(tool, fields)}
          />
        )}
        {error && <p className="text-sm text-red-700">{error}</p>}
      </section>

      {run && <RunResult run={run} />}
    </div>
  );
}

function SandboxStatus({ sandbox, onRetry }: { sandbox: Sandbox; onRetry: () => void }) {
  switch (sandbox.status) {
    case "creating":
      return <p className="text-xs text-slate-500">Starting a sandbox session…</p>;
    case "ready":
      return (
        <p className="text-xs text-slate-500">
          Session <span className="font-mono">{sandbox.id}</span>, expires after{" "}
          {Math.round(sandbox.ttl / 60)} minutes without a request.
        </p>
      );
    case "gone":
      return (
        <div className="rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">
          <p>{sandbox.message}</p>
          <p className="mt-1">
            The last result below is kept. <strong>Reset</strong> starts a new, empty sandbox.
          </p>
        </div>
      );
    case "failed":
      return (
        <div className="rounded border border-red-200 bg-red-50 p-2 text-xs text-red-800">
          <p>Couldn't start a sandbox session: {sandbox.error}</p>
          <button type="button" className="mt-1 underline" onClick={onRetry}>
            Try again
          </button>
        </div>
      );
  }
}

// ---- session state -------------------------------------------------------------------

type Fact = "is_new_patient" | "has_referral";

const FACTS: { fact: Fact; label: string }[] = [
  { fact: "is_new_patient", label: "New patient" },
  { fact: "has_referral", label: "Referral on file" },
];

function PatientControls({
  state,
  disabled,
  onChange,
}: {
  state: SessionState;
  disabled: boolean;
  onChange: (fact: Fact, value: boolean | null) => void;
}) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
        Patient <span className="font-normal normal-case text-slate-400">via update_booking</span>
      </h3>
      {FACTS.map(({ fact, label }) => (
        <div key={fact} className="flex items-center justify-between gap-2 text-sm">
          <span>{label}</span>
          <div className="flex overflow-hidden rounded border border-slate-300 text-xs">
            {(
              [
                [null, "Unknown"],
                [true, "Yes"],
                [false, "No"],
              ] as const
            ).map(([value, text]) => (
              <button
                key={text}
                type="button"
                disabled={disabled}
                className={`px-2 py-1 disabled:opacity-50 ${
                  state.patient[fact] === value ? "bg-slate-900 text-white" : "bg-white hover:bg-slate-50"
                }`}
                onClick={() => state.patient[fact] !== value && onChange(fact, value)}
              >
                {text}
              </button>
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}

function DraftView({ state, refs }: { state: SessionState; refs: CrossRefs }) {
  const { draft, offer, reservation } = state;
  const named = (id: string | null) =>
    id ? (
      <>
        {refs.name.get(id) ?? id} <span className="font-mono text-[11px] text-slate-400">{id}</span>
      </>
    ) : (
      <span className="text-slate-400">—</span>
    );
  const slot = offer?.slots.find((s) => s.id === draft.slot_id);
  const rows: [string, ReactNode][] = [
    ["Service", named(draft.service_id)],
    ["Provider", named(draft.provider_id)],
    ["Location", named(draft.location_id)],
    [
      "Time",
      draft.slot_id ? (
        slot ? (
          formatTime(slot.start)
        ) : (
          <span className="font-mono text-[11px]">{draft.slot_id}</span>
        )
      ) : (
        <span className="text-slate-400">—</span>
      ),
    ],
    [
      "Offered",
      offer ? `${offer.slots.length} time${offer.slots.length === 1 ? "" : "s"} for revision ${offer.revision}` : "—",
    ],
    ["Confirmed", state.confirmed ? "yes" : "no"],
  ];

  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
        Draft <span className="font-normal normal-case text-slate-400">revision {state.revision}</span>
      </h3>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-xs text-slate-500">{label}</dt>
            <dd className="min-w-0 break-words">{value}</dd>
          </div>
        ))}
      </dl>
      {reservation && (
        <p className="rounded bg-emerald-50 px-2 py-1 text-sm text-emerald-800">
          Booked: <span className="font-mono">{reservation.confirmation}</span>,{" "}
          {formatTime(reservation.slot.start)}
        </p>
      )}
    </section>
  );
}

function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/Los_Angeles",
  });
}

// ---- the form ------------------------------------------------------------------------

const NULL = "\u0000null"; // a select's "explicit null" option

const inputClass =
  "w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm disabled:bg-slate-100 disabled:text-slate-400";

function ToolForm({
  tools,
  tool,
  onSelectTool,
  values,
  onValues,
  catalog,
  state,
  disabled,
  running,
  onRun,
}: {
  tools: ToolSpec[];
  tool: ToolSpec;
  onSelectTool: (name: string) => void;
  values: FormValues;
  onValues: (values: FormValues) => void;
  catalog: Catalog;
  state: SessionState | null;
  disabled: boolean;
  running: boolean;
  onRun: (fields: FieldSpec[]) => void;
}) {
  const fields = useMemo(() => formFields(tool.parameters), [tool.parameters]);

  // Suggestions for id fields: catalog records, and the times currently offered.
  const suggestions: Record<string, { value: string; label: string }[]> = useMemo(
    () => ({
      service_id: catalog.appointment_types.map((t) => ({ value: t.id, label: t.name })),
      provider_id: catalog.providers.map((p) => ({ value: p.id, label: `${p.name} (${p.title}, ${p.specialty})` })),
      location_id: catalog.locations.map((l) => ({ value: l.id, label: l.name })),
      slot_id: (state?.offer?.slots ?? []).map((s) => ({ value: s.id, label: formatTime(s.start) })),
    }),
    [catalog, state?.offer],
  );

  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (!disabled) onRun(fields);
      }}
    >
      <select className={`${inputClass} font-mono`} value={tool.name} onChange={(e) => onSelectTool(e.target.value)}>
        {tools.map((t) => (
          <option key={t.name} value={t.name}>
            {t.name}
          </option>
        ))}
      </select>
      <p className="text-xs text-slate-500">{tool.description}</p>
      {fields.length === 0 && <p className="text-xs text-slate-400">No arguments.</p>}
      {fields.map((field) => (
        <FieldInput
          key={field.key}
          field={field}
          values={values}
          onValues={onValues}
          suggestions={suggestions}
          current={field.key === "draft_revision" ? state?.revision : undefined}
        />
      ))}
      <p className="text-[11px] text-slate-400">
        Empty fields are left out. “null” sends an explicit null, which clears a field in
        update_booking.
      </p>
      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={disabled}
          className="rounded bg-slate-900 px-3 py-1 text-sm text-white hover:bg-slate-700 disabled:opacity-40"
        >
          {running ? "Running…" : `Run ${tool.name}`}
        </button>
        <Button tone="ghost" onClick={() => onValues({})}>
          Clear
        </Button>
      </div>
    </form>
  );
}

function FieldInput({
  field,
  values,
  onValues,
  suggestions,
  current,
}: {
  field: FieldSpec;
  values: FormValues;
  onValues: (values: FormValues) => void;
  suggestions: Record<string, { value: string; label: string }[]>;
  current?: number;
}) {
  const id = useId();
  const raw = values[field.key];
  const set = (value: string | null) => onValues({ ...values, [field.key]: value });

  const label = (
    <span className="text-xs font-medium text-slate-600">
      <span className="font-mono">{field.name}</span>
      {field.required && <span className="text-red-600"> *</span>}
      {field.description && <span className="ml-1 font-normal text-slate-400">{field.description}</span>}
    </span>
  );

  if (field.kind === "object") {
    return (
      <fieldset className="flex flex-col gap-2 rounded border border-slate-200 p-2">
        <legend className="px-1">{label}</legend>
        {(field.fields ?? []).map((child) => (
          <FieldInput key={child.key} field={child} values={values} onValues={onValues} suggestions={suggestions} />
        ))}
      </fieldset>
    );
  }

  let control;
  if (field.kind === "boolean" || field.kind === "enum") {
    const options = field.kind === "boolean" ? ["true", "false"] : (field.options ?? []);
    control = (
      <select
        id={id}
        className={inputClass}
        value={raw === null ? NULL : (raw ?? "")}
        onChange={(e) => set(e.target.value === NULL ? null : e.target.value)}
      >
        <option value="">(leave out)</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
        {field.nullable && <option value={NULL}>null</option>}
      </select>
    );
  } else {
    const list = suggestions[field.name];
    control = (
      <div className="flex items-center gap-2">
        <input
          id={id}
          className={`${inputClass} ${field.kind === "string" ? "font-mono text-xs" : ""}`}
          type={field.kind === "date" ? "date" : field.kind === "string" ? "text" : "number"}
          step={field.kind === "number" ? "any" : undefined}
          list={list?.length ? `${id}-list` : undefined}
          disabled={raw === null}
          value={raw ?? ""}
          placeholder={current !== undefined ? `current: ${current}` : undefined}
          onChange={(e) => set(e.target.value)}
        />
        {current !== undefined && (
          <Button tone="ghost" onClick={() => set(String(current))}>
            Use {current}
          </Button>
        )}
        {field.nullable && (
          <label className="flex shrink-0 items-center gap-1 text-xs text-slate-500">
            <input type="checkbox" checked={raw === null} onChange={(e) => set(e.target.checked ? null : "")} />
            null
          </label>
        )}
        {list?.length ? (
          <datalist id={`${id}-list`}>
            {list.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </datalist>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id}>{label}</label>
      {control}
    </div>
  );
}

// ---- the result ------------------------------------------------------------------------

function RunResult({ run }: { run: Run }) {
  const failed = "error" in run.response.result;
  return (
    <section className="flex flex-col gap-2 border-t border-slate-200 pt-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Result <span className="font-mono normal-case">{run.tool}</span>
        </h3>
        <span
          className="text-xs text-slate-500"
          title="Estimated from the JSON's length (about 4 characters a token), not a tokenizer count"
        >
          ≈{run.response.approx_tokens} tokens (estimate)
        </span>
      </div>
      <p className="font-mono text-[11px] break-all text-slate-500">args: {JSON.stringify(run.args)}</p>
      {failed && (
        <p className="rounded bg-red-50 px-2 py-1 text-xs text-red-800">
          The tool refused this call; this is the error the model would get.
        </p>
      )}
      <pre className="max-h-[32rem] overflow-auto rounded bg-slate-900 p-2 text-[11px] text-slate-100">
        {JSON.stringify(run.response.result, null, 2)}
      </pre>
    </section>
  );
}
