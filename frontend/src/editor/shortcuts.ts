// Keyboard shortcuts: Save (Mod+S), Undo (Mod+Z) and Redo (Shift+Mod+Z, or
// Ctrl+Y), where Mod is Cmd on macOS and Ctrl elsewhere.
//
// Inside a text field, undo and redo stay the browser's own (they undo typing in
// that field, whose buffer isn't committed yet); document undo is a click away.
// Save works everywhere, and commits the field's buffer first.

export type Shortcut = "save" | "undo" | "redo";

interface Keys {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
}

export const isMac =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

/** How a shortcut is written on this platform, for tooltips. */
export function shortcutLabel(shortcut: Shortcut): string {
  const mod = isMac ? "⌘" : "Ctrl+";
  const shift = isMac ? "⇧" : "Shift+";
  return { save: `${mod}S`, undo: `${mod}Z`, redo: `${shift}${mod}Z` }[shortcut];
}

export function shortcutFor(e: Keys, inTextField: boolean, mac = isMac): Shortcut | null {
  const mod = mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
  if (!mod || e.altKey) return null;
  const key = e.key.toLowerCase();
  if (key === "s" && !e.shiftKey) return "save";
  if (inTextField) return null;
  if (key === "z") return e.shiftKey ? "redo" : "undo";
  if (key === "y" && !mac && !e.shiftKey) return "redo";
  return null;
}

/** Whether keys typed here edit text (so the browser's own undo applies). */
export function isTextField(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable || target instanceof HTMLTextAreaElement) return true;
  if (!(target instanceof HTMLInputElement)) return false;
  return !["checkbox", "radio", "button", "submit", "reset", "file", "range", "color"].includes(
    target.type,
  );
}
