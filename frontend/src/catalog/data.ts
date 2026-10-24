// Catalog data that doesn't change while the server runs: the catalog, its lint
// and the tool/guard registry. Each is fetched once, when a component first asks
// for it, and shared by everyone who asks; after a failure, `retry` fetches again.

import { useSyncExternalStore } from "react";
import * as api from "../api";

export type Loaded<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "failed"; error: string };

export interface Resource<T> {
  subscribe(listener: () => void): () => void;
  get(): Loaded<T>;
  retry(): void;
}

export function resource<T>(load: () => Promise<T>): Resource<T> {
  let state: Loaded<T> = { status: "loading" };
  let started = false;
  const listeners = new Set<() => void>();
  const set = (next: Loaded<T>) => {
    state = next;
    listeners.forEach((listener) => listener());
  };

  const fetch = () => {
    started = true;
    if (state.status !== "loading") set({ status: "loading" });
    load().then(
      (data) => set({ status: "ready", data }),
      (e) => set({ status: "failed", error: e instanceof Error ? e.message : String(e) }),
    );
  };

  return {
    subscribe(listener) {
      listeners.add(listener);
      if (!started) fetch();
      return () => listeners.delete(listener);
    },
    get: () => state,
    retry: fetch,
  };
}

export function useResource<T>(r: Resource<T>): Loaded<T> {
  return useSyncExternalStore(r.subscribe, r.get);
}

export const catalogData = resource(api.getCatalog);
export const lintData = resource(api.getCatalogLint);
export const registryData = resource(api.listTools);
