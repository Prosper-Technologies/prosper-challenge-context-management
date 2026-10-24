// Field buffers: an inspector field edits a local text buffer and commits it to
// the document as one transaction (on blur, Enter or Apply), so keystrokes don't
// each become an undo step and half-typed or malformed values never reach the
// document.
//
// A field registers here while its buffer holds uncommitted text. Save and test
// call commit every registered buffer first, and stop on any rejection, so they
// never silently use a document older than what's on screen.

/** Commit the buffer: null on success, else why it was rejected (the text stays). */
export type Commit = () => string | null | Promise<string | null>;

const pending = new Map<symbol, Commit>();
const listeners = new Set<() => void>();

const changed = () => listeners.forEach((listener) => listener());

export function registerBuffer(commit: Commit): () => void {
  const key = Symbol();
  pending.set(key, commit);
  changed();
  return () => {
    pending.delete(key);
    changed();
  };
}

/** Whether any field holds uncommitted text (for useSyncExternalStore). */
export const hasPendingBuffers = () => pending.size > 0;

export function subscribeBuffers(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Commit every pending buffer; returns the rejections. */
export async function commitBuffers(): Promise<string[]> {
  const errors: string[] = [];
  for (const commit of [...pending.values()]) {
    const error = await commit();
    if (error) errors.push(error);
  }
  return errors;
}
