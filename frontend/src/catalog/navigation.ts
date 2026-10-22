// Catalog screen navigation: which record a lint issue points at, which records
// have issues, the cross-references between tables, and the tables' text filter.

import type { Catalog, CatalogIssue } from "../generated/contract";

export const TABLES = ["locations", "providers", "appointment_types"] as const;
export type Table = (typeof TABLES)[number];

export interface Target {
  table: Table;
  id: string;
}

const isTable = (value: unknown): value is Table => (TABLES as readonly unknown[]).includes(value);

/** The record a lint issue's path points at (`["providers", 26, "name"]`), or null. */
export function lintTarget(issue: CatalogIssue, catalog: Catalog): Target | null {
  const [table, index] = issue.path;
  if (!isTable(table) || typeof index !== "number") return null;
  const record = catalog[table][index];
  return record ? { table, id: record.id } : null;
}

/** Lint issues by the id of the record their path points at. */
export function issuesByRecord(issues: CatalogIssue[], catalog: Catalog): Map<string, CatalogIssue[]> {
  const byId = new Map<string, CatalogIssue[]>();
  for (const issue of issues) {
    const target = lintTarget(issue, catalog);
    if (target) byId.set(target.id, [...(byId.get(target.id) ?? []), issue]);
  }
  return byId;
}

export interface CrossRefs {
  name: Map<string, string>; // any record id -> its name
  providersAt: Map<string, string[]>; // location id -> provider ids
  providersOffering: Map<string, string[]>; // appointment type id -> provider ids
}

export function crossRefs(catalog: Catalog): CrossRefs {
  const name = new Map<string, string>();
  for (const table of TABLES) for (const record of catalog[table]) name.set(record.id, record.name);
  const providersAt = new Map<string, string[]>();
  const providersOffering = new Map<string, string[]>();
  const add = (map: Map<string, string[]>, key: string, id: string) =>
    map.set(key, [...(map.get(key) ?? []), id]);
  for (const provider of catalog.providers) {
    for (const loc of provider.location_ids) add(providersAt, loc, provider.id);
    for (const type of provider.appointment_type_ids) add(providersOffering, type, provider.id);
  }
  return { name, providersAt, providersOffering };
}

/** Whether every word of the filter occurs in one of the row's texts (case-insensitive). */
export function matchesFilter(filter: string, texts: (string | number | boolean | null | undefined)[]): boolean {
  const words = filter.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const haystack = texts
    .filter((t) => t !== null && t !== undefined)
    .join(" ")
    .toLowerCase();
  return words.every((w) => haystack.includes(w));
}
