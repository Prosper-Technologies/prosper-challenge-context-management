// Inspector fields. Each edits a local buffer and commits it as one document
// transaction on blur (or Enter in a single-line input); Escape reverts. A
// rejected commit keeps the text and shows why. See editor/buffers.ts.

import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import type { Issue } from "../generated/contract";
import { registerBuffer } from "../editor/buffers";
import { type Selection, useEditor } from "../editor/store";
import { issueSelection } from "../editor/validation";

type CommitFn = (text: string) => string | null;

export function useBuffer(value: string, commit: CommitFn) {
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const latest = useRef({ draft, value, commit });
  latest.current = { draft, value, commit };

  const flush = useRef(() => {
    const { draft, value, commit } = latest.current;
    if (draft === null) return null;
    const rejected = draft === value ? null : commit(draft);
    if (rejected) {
      setError(rejected);
      return rejected;
    }
    setDraft(null);
    setError(null);
    return null;
  }).current;

  const dirty = draft !== null;
  useEffect(() => (dirty ? registerBuffer(flush) : undefined), [dirty, flush]);

  return {
    text: draft ?? value,
    error,
    change(text: string) {
      setDraft(text);
      setError(null);
    },
    commit: flush,
    revert() {
      setDraft(null);
      setError(null);
    },
  };
}

const inputClass =
  "w-full rounded border border-slate-300 bg-white px-2 py-1 text-sm read-only:bg-slate-100 read-only:text-slate-500 aria-invalid:border-red-500";

export function Label({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-medium text-slate-600">
        {label}
        {hint && <span className="ml-1 font-normal text-slate-400">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

export function FieldError({ error }: { error: string | null }) {
  return error ? <span className="text-xs text-red-700">{error}</span> : null;
}

export function TextField({
  label,
  hint,
  value,
  onCommit,
  multiline = false,
  rows = 3,
  placeholder,
  mono = false,
}: {
  label?: string;
  hint?: string;
  value: string;
  onCommit: CommitFn;
  multiline?: boolean;
  rows?: number;
  placeholder?: string;
  mono?: boolean;
}) {
  const locked = useEditor((s) => s.locked);
  const buffer = useBuffer(value, onCommit);
  const props = {
    className: `${inputClass} ${mono ? "font-mono text-xs" : ""}`,
    value: buffer.text,
    placeholder,
    readOnly: locked,
    "aria-invalid": buffer.error ? true : undefined,
    onChange: (e: { target: { value: string } }) => buffer.change(e.target.value),
    onBlur: () => buffer.commit(),
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key === "Escape") buffer.revert();
      if (e.key === "Enter" && !multiline) buffer.commit();
    },
  };
  const control = multiline ? (
    <textarea {...props} rows={rows} spellCheck={!mono} />
  ) : (
    <input {...props} spellCheck={false} />
  );
  return (
    <div className="flex flex-col gap-1">
      {label ? (
        <Label label={label} hint={hint}>
          {control}
        </Label>
      ) : (
        control
      )}
      <FieldError error={buffer.error} />
    </div>
  );
}

/** Raw JSON for a value the forms don't cover; `parse` rejects bad shapes. */
export function JsonField<T>({
  label,
  hint,
  value,
  parse,
  onCommit,
  rows = 4,
}: {
  label: string;
  hint?: string;
  value: unknown;
  parse: (text: string) => T;
  onCommit: (value: T) => string | null;
  rows?: number;
}) {
  const commit = (text: string) => {
    let parsed: T;
    try {
      parsed = parse(text);
    } catch (e) {
      return e instanceof Error ? e.message : String(e);
    }
    return onCommit(parsed);
  };
  return (
    <TextField
      label={label}
      hint={hint}
      value={JSON.stringify(value ?? [], null, 2)}
      onCommit={commit}
      multiline
      rows={rows}
      mono
    />
  );
}

export function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  const locked = useEditor((s) => s.locked);
  return (
    <Label label={label}>
      <select
        className={inputClass}
        value={value}
        disabled={locked}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </Label>
  );
}

export function Checkbox({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  const locked = useEditor((s) => s.locked);
  const id = useId();
  return (
    <span className="flex items-center gap-2 text-sm">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={locked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <label htmlFor={id}>{label}</label>
    </span>
  );
}

export function Button({
  children,
  onClick,
  disabled,
  tone = "default",
  title,
}: {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  tone?: "default" | "primary" | "danger" | "ghost";
  title?: string;
}) {
  const tones = {
    default: "border border-slate-300 bg-white hover:bg-slate-50",
    primary: "bg-slate-900 text-white hover:bg-slate-700",
    danger: "border border-red-200 bg-white text-red-700 hover:bg-red-50",
    ghost: "text-slate-600 hover:bg-slate-100",
  };
  return (
    <button
      type="button"
      title={title}
      className={`rounded px-2 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-40 ${tones[tone]}`}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

/** Issues as a clickable list; clicking selects what the issue points at. */
export function IssueList({
  issues,
  onSelect,
}: {
  issues: Issue[];
  onSelect?: (selection: Selection) => void;
}) {
  const select = useEditor((s) => s.select);
  if (issues.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1">
      {issues.map((issue, k) => (
        <li key={k}>
          <button
            type="button"
            className={`w-full rounded px-2 py-1 text-left text-xs ${
              issue.level === "error" ? "bg-red-50 text-red-800" : "bg-amber-50 text-amber-800"
            }`}
            onClick={() => (onSelect ?? select)(issueSelection(issue))}
          >
            {(issue.node || issue.edge) && (
              <span className="mr-1 font-mono text-[11px] opacity-70">
                {[issue.node, issue.edge].filter(Boolean).join(" › ")}
              </span>
            )}
            {issue.message}
          </button>
        </li>
      ))}
    </ul>
  );
}

export function Section({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="flex flex-col gap-2 border-t border-slate-200 pt-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
        {action}
      </div>
      {children}
    </section>
  );
}
