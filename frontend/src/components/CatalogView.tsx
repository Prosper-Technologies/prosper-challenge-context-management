// The catalog screen: the clinic catalog as filterable, cross-linked tables, its
// lint, and the tool playground. Everything here is read-only; the playground
// books only in its own sandbox session.
//
// Cross-links either focus one record (a provider's location: switch to that
// table, clear its filter, highlight and scroll to the row) or filter a table by
// an id (a location's providers: the providers table filtered by `loc_004`).

import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import type {
  AppointmentType,
  Catalog,
  CatalogIssue,
  Location,
  Provider,
} from "../generated/contract";
import { catalogData, type Loaded, lintData, useResource } from "../catalog/data";
import {
  type CrossRefs,
  type Table,
  type Target,
  crossRefs,
  issuesByRecord,
  lintTarget,
  matchesFilter,
} from "../catalog/navigation";
import { worstLevel } from "../editor/validation";
import { Button, IssueRows } from "./fields";
import ToolPlayground from "./ToolPlayground";

type Tab = Table | "lint";

const TAB_LABELS: Record<Tab, string> = {
  locations: "Locations",
  providers: "Providers",
  appointment_types: "Appointment types",
  lint: "Lint",
};

export default function CatalogView() {
  const catalog = useResource(catalogData);
  if (catalog.status === "loading")
    return <p className="p-8 text-center text-sm text-slate-500">Loading the catalog…</p>;
  if (catalog.status === "failed")
    return (
      <div className="flex flex-col items-center gap-3 p-8 text-center text-sm text-slate-600">
        <p className="font-medium text-slate-800">Couldn't load the catalog</p>
        <p className="font-mono text-xs break-all text-slate-400">{catalog.error}</p>
        <Button onClick={catalogData.retry}>Retry</Button>
      </div>
    );
  return <CatalogScreen catalog={catalog.data} />;
}

interface Nav {
  focus(target: Target): void;
  filter(table: Table, text: string): void;
}

function CatalogScreen({ catalog }: { catalog: Catalog }) {
  const lint = useResource(lintData);
  const [tab, setTab] = useState<Tab>("locations");
  const [filters, setFilters] = useState<Record<Table, string>>({
    locations: "",
    providers: "",
    appointment_types: "",
  });
  const [focus, setFocus] = useState<Target | null>(null);
  const refs = useMemo(() => crossRefs(catalog), [catalog]);
  const issues = lint.status === "ready" ? lint.data.issues : NO_ISSUES;
  const byRecord = useMemo(() => issuesByRecord(issues, catalog), [issues, catalog]);

  const nav: Nav = {
    focus(target) {
      setTab(target.table);
      setFilters((f) => ({ ...f, [target.table]: "" }));
      setFocus(target);
    },
    filter(table, text) {
      setTab(table);
      setFilters((f) => ({ ...f, [table]: text }));
      setFocus(null);
    },
  };
  const counts: Record<Tab, number> = {
    locations: catalog.locations.length,
    providers: catalog.providers.length,
    appointment_types: catalog.appointment_types.length,
    lint: issues.length,
  };

  return (
    <div className="flex h-full min-h-0">
      <section className="flex min-w-0 flex-1 flex-col">
        <nav className="flex gap-1 border-b border-slate-200 bg-white px-4 pt-2">
          {(Object.keys(TAB_LABELS) as Tab[]).map((t) => (
            <button
              key={t}
              type="button"
              className={`-mb-px rounded-t border px-3 py-1.5 text-sm ${
                tab === t
                  ? "border-slate-200 border-b-white bg-white font-medium"
                  : "border-transparent text-slate-500 hover:text-slate-800"
              }`}
              onClick={() => setTab(t)}
            >
              {TAB_LABELS[t]}
              <span className="ml-1 text-xs text-slate-400">{counts[t]}</span>
            </button>
          ))}
        </nav>
        <div className="min-h-0 flex-1 overflow-auto">
          {tab === "lint" ? (
            <LintPanel catalog={catalog} lint={lint} onNavigate={nav.focus} />
          ) : (
            <TableView
              key={tab}
              table={tab}
              catalog={catalog}
              refs={refs}
              byRecord={byRecord}
              filter={filters[tab]}
              onFilter={(text) => setFilters((f) => ({ ...f, [tab]: text }))}
              focus={focus?.table === tab ? focus.id : null}
              nav={nav}
            />
          )}
        </div>
      </section>
      <aside className="w-[26rem] shrink-0 overflow-y-auto border-l border-slate-200 bg-white p-4">
        <ToolPlayground catalog={catalog} refs={refs} />
      </aside>
    </div>
  );
}

const NO_ISSUES: CatalogIssue[] = [];

// ---- lint -----------------------------------------------------------------------------

function LintPanel({
  catalog,
  lint,
  onNavigate,
}: {
  catalog: Catalog;
  lint: Loaded<{ issues: CatalogIssue[] }>;
  onNavigate: (target: Target) => void;
}) {
  return (
    <div className="flex max-w-3xl flex-col gap-4 p-4">
      <section className="flex flex-col gap-1">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Booking rules</h3>
        <ol className="list-decimal pl-5 text-sm text-slate-700">
          {catalog.policies.map((p, k) => (
            <li key={k}>{p}</li>
          ))}
        </ol>
        <p className="text-[11px] text-slate-400">
          The policy engine checks these in code before any booking (backend/catalog/policy.py).
        </p>
      </section>
      <section className="flex flex-col gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
          Catalog health
        </h3>
        {lint.status === "loading" && <p className="text-sm text-slate-500">Checking…</p>}
        {lint.status === "failed" && (
          <p className="text-sm text-red-700">
            Couldn't load the lint: {lint.error}{" "}
            <button type="button" className="underline" onClick={lintData.retry}>
              Retry
            </button>
          </p>
        )}
        {lint.status === "ready" && lint.data.issues.length === 0 && (
          <p className="text-sm text-slate-500">No issues.</p>
        )}
        {lint.status === "ready" && (
          <IssueRows
            issues={lint.data.issues}
            label={(issue) => issue.code}
            onIssueClick={(issue) => {
              const target = lintTarget(issue, catalog);
              if (target) onNavigate(target);
            }}
          />
        )}
      </section>
    </div>
  );
}

// ---- tables ---------------------------------------------------------------------------

interface Column<T> {
  header: string;
  cell: (row: T) => ReactNode;
  className?: string;
}

interface TableProps {
  table: Table;
  catalog: Catalog;
  refs: CrossRefs;
  byRecord: Map<string, CatalogIssue[]>;
  filter: string;
  onFilter: (text: string) => void;
  focus: string | null;
  nav: Nav;
}

function TableView(props: TableProps) {
  const { table, catalog } = props;
  if (table === "locations")
    return <RecordTable rows={catalog.locations} {...props} {...locationTable(props)} />;
  if (table === "providers")
    return <RecordTable rows={catalog.providers} {...props} {...providerTable(props)} />;
  return <RecordTable rows={catalog.appointment_types} {...props} {...typeTable(props)} />;
}

function RecordTable<T extends { id: string; name: string }>({
  rows,
  columns,
  searchText,
  filter,
  onFilter,
  focus,
  byRecord,
}: TableProps & {
  rows: T[];
  columns: Column<T>[];
  searchText: (row: T) => (string | number | boolean | null | undefined)[];
}) {
  const container = useRef<HTMLTableSectionElement>(null);
  const shown = rows.filter((row) => matchesFilter(filter, searchText(row)));

  useEffect(() => {
    if (!focus) return;
    container.current
      ?.querySelector(`[data-row-id="${CSS.escape(focus)}"]`)
      ?.scrollIntoView({ block: "center" });
  }, [focus]);

  return (
    <div className="flex flex-col gap-2 p-4">
      <div className="flex items-center gap-2">
        <input
          className="w-72 rounded border border-slate-300 bg-white px-2 py-1 text-sm"
          placeholder="Filter: names, ids, specialties…"
          value={filter}
          onChange={(e) => onFilter(e.target.value)}
        />
        <span className="text-xs text-slate-500">
          {shown.length === rows.length ? `${rows.length} records` : `${shown.length} of ${rows.length}`}
        </span>
        {filter && (
          <Button tone="ghost" onClick={() => onFilter("")}>
            Clear
          </Button>
        )}
      </div>
      <table className="w-full border-collapse bg-white text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-xs text-slate-500">
            {columns.map((c) => (
              <th key={c.header} className="px-2 py-1.5 font-medium">
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody ref={container}>
          {shown.map((row) => {
            const issues = byRecord.get(row.id) ?? [];
            const level = worstLevel(issues);
            return (
              <tr
                key={row.id}
                data-row-id={row.id}
                className={`border-b border-slate-100 align-top ${
                  row.id === focus ? "bg-sky-50 outline-2 -outline-offset-2 outline-sky-400" : ""
                }`}
              >
                {columns.map((c, k) => (
                  <td key={c.header} className={`px-2 py-1.5 ${c.className ?? ""}`}>
                    {k === 1 && level ? (
                      <span className="flex items-start gap-1.5">
                        <span className="min-w-0">{c.cell(row)}</span>
                        <span
                          title={issues.map((x) => x.message).join("\n")}
                          className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${
                            level === "error" ? "bg-red-500" : "bg-amber-400"
                          }`}
                        />
                      </span>
                    ) : (
                      c.cell(row)
                    )}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
      {shown.length === 0 && <p className="text-sm text-slate-500">Nothing matches the filter.</p>}
    </div>
  );
}

const idColumn = <T extends { id: string }>(): Column<T> => ({
  header: "ID",
  cell: (row) => row.id,
  className: "font-mono text-xs text-slate-500 whitespace-nowrap",
});

const nameColumn = <T extends { name: string }>(): Column<T> => ({
  header: "Name",
  cell: (row) => row.name,
  className: "font-medium",
});

const yesNo = (value: boolean) => (value ? "yes" : "no");

function Chip({ children, onClick, title }: { children: ReactNode; onClick?: () => void; title?: string }) {
  const className = "rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700";
  return onClick ? (
    <button type="button" title={title} className={`${className} hover:bg-sky-100`} onClick={onClick}>
      {children}
    </button>
  ) : (
    <span className={className}>{children}</span>
  );
}

function CountLink({ count, noun, onClick }: { count: number; noun: string; onClick: () => void }) {
  if (count === 0) return <span className="text-xs text-red-700">none</span>;
  return (
    <button type="button" className="text-xs whitespace-nowrap text-sky-700 underline" onClick={onClick}>
      {count} {noun}
      {count > 1 ? "s" : ""}
    </button>
  );
}

function locationTable({ refs, nav }: TableProps) {
  const providers = (l: Location) => refs.providersAt.get(l.id) ?? [];
  const columns: Column<Location>[] = [
    idColumn(),
    nameColumn(),
    { header: "Address", cell: (l) => `${l.address}, ${l.city}` },
    { header: "Phone", cell: (l) => l.phone, className: "whitespace-nowrap" },
    { header: "Hours", cell: (l) => l.hours, className: "text-xs" },
    {
      header: "Capabilities",
      cell: (l) => (
        <span className="flex flex-wrap gap-1">
          {l.capabilities.map((c) => (
            <Chip key={c}>{c}</Chip>
          ))}
        </span>
      ),
    },
    {
      header: "Providers",
      cell: (l) => (
        <CountLink count={providers(l).length} noun="provider" onClick={() => nav.filter("providers", l.id)} />
      ),
    },
  ];
  const searchText = (l: Location) => [l.id, l.name, l.address, l.city, l.phone, ...l.capabilities];
  return { columns, searchText };
}

function providerTable({ refs, nav }: TableProps) {
  const columns: Column<Provider>[] = [
    idColumn(),
    nameColumn(),
    { header: "Title", cell: (p) => p.title },
    { header: "Specialty", cell: (p) => p.specialty },
    { header: "New patients", cell: (p) => yesNo(p.accepting_new_patients) },
    { header: "Languages", cell: (p) => p.languages.join(", "), className: "text-xs" },
    {
      header: "Locations",
      cell: (p) => (
        <span className="flex flex-wrap gap-1">
          {p.location_ids.map((id) => (
            <Chip key={id} title={id} onClick={() => nav.focus({ table: "locations", id })}>
              {refs.name.get(id) ?? id}
            </Chip>
          ))}
        </span>
      ),
    },
    {
      header: "Services",
      cell: (p) => (
        <CountLink
          count={p.appointment_type_ids.length}
          noun="type"
          onClick={() => nav.filter("appointment_types", p.id)}
        />
      ),
    },
  ];
  const searchText = (p: Provider) => [
    p.id,
    p.name,
    p.title,
    p.specialty,
    ...p.languages,
    ...p.location_ids,
    ...p.location_ids.map((id) => refs.name.get(id)),
    ...p.appointment_type_ids,
  ];
  return { columns, searchText };
}

function typeTable({ refs, nav }: TableProps) {
  const providers = (t: AppointmentType) => refs.providersOffering.get(t.id) ?? [];
  const columns: Column<AppointmentType>[] = [
    idColumn(),
    nameColumn(),
    { header: "Specialty", cell: (t) => t.specialty },
    { header: "Duration", cell: (t) => `${t.duration_min} min`, className: "whitespace-nowrap" },
    { header: "Referral", cell: (t) => (t.requires_referral ? "required" : "—") },
    { header: "New patients", cell: (t) => yesNo(t.new_patients_allowed) },
    { header: "Needs", cell: (t) => (t.required_capability ? <Chip>{t.required_capability}</Chip> : "—") },
    {
      header: "Providers",
      cell: (t) => (
        <CountLink count={providers(t).length} noun="provider" onClick={() => nav.filter("providers", t.id)} />
      ),
    },
  ];
  const searchText = (t: AppointmentType) => [
    t.id,
    t.name,
    t.specialty,
    t.required_capability,
    t.requires_referral ? "referral" : null,
    ...providers(t),
  ];
  return { columns, searchText };
}
