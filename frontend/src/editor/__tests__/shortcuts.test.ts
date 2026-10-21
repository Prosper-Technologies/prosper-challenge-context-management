import { describe, expect, it } from "vitest";
import { shortcutFor } from "../shortcuts";

const keys = (key: string, mods: { meta?: boolean; ctrl?: boolean; shift?: boolean; alt?: boolean } = {}) => ({
  key,
  metaKey: !!mods.meta,
  ctrlKey: !!mods.ctrl,
  shiftKey: !!mods.shift,
  altKey: !!mods.alt,
});

describe("shortcutFor", () => {
  it("maps Cmd on macOS and Ctrl elsewhere", () => {
    expect(shortcutFor(keys("s", { meta: true }), false, true)).toBe("save");
    expect(shortcutFor(keys("s", { ctrl: true }), false, true)).toBeNull();
    expect(shortcutFor(keys("s", { ctrl: true }), false, false)).toBe("save");
    expect(shortcutFor(keys("s", { meta: true }), false, false)).toBeNull();
  });

  it("maps undo and redo, including Ctrl+Y off macOS", () => {
    expect(shortcutFor(keys("z", { meta: true }), false, true)).toBe("undo");
    // Shift reports the key as upper case.
    expect(shortcutFor(keys("Z", { meta: true, shift: true }), false, true)).toBe("redo");
    expect(shortcutFor(keys("y", { ctrl: true }), false, false)).toBe("redo");
    expect(shortcutFor(keys("y", { meta: true }), false, true)).toBeNull();
  });

  it("leaves undo and redo to the browser inside a text field, but still saves", () => {
    expect(shortcutFor(keys("z", { meta: true }), true, true)).toBeNull();
    expect(shortcutFor(keys("z", { meta: true, shift: true }), true, true)).toBeNull();
    expect(shortcutFor(keys("s", { meta: true }), true, true)).toBe("save");
  });

  it("ignores other combinations", () => {
    expect(shortcutFor(keys("s"), false, true)).toBeNull();
    expect(shortcutFor(keys("s", { meta: true, alt: true }), false, true)).toBeNull();
    expect(shortcutFor(keys("s", { meta: true, shift: true }), false, true)).toBeNull();
    expect(shortcutFor(keys("a", { meta: true }), false, true)).toBeNull();
  });
});
