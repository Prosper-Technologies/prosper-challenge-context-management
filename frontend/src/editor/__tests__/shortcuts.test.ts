import { describe, expect, it, vi } from "vitest";
import { shortcutFor, shortcutListener } from "../shortcuts";

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

describe("shortcutListener", () => {
  function setup() {
    let active = true;
    const actions = { save: vi.fn(), undo: vi.fn(), redo: vi.fn() };
    const inField = new Set<unknown>();
    const listener = shortcutListener(() => active, actions, (target) => inField.has(target), true);
    const press = (key: string, mods: Parameters<typeof keys>[1], target: unknown = null) => {
      const e = { ...keys(key, mods), target: target as EventTarget | null, preventDefault: vi.fn() };
      listener(e);
      return e.preventDefault;
    };
    return { actions, press, inField, setActive: (value: boolean) => (active = value) };
  }

  it("runs the action and stops the browser's default", () => {
    const { actions, press } = setup();
    expect(press("s", { meta: true })).toHaveBeenCalled();
    press("z", { meta: true });
    press("z", { meta: true, shift: true });
    expect([actions.save, actions.undo, actions.redo].map((f) => f.mock.calls.length)).toEqual([1, 1, 1]);
  });

  it("does nothing while the editor is hidden, and works again once it shows", () => {
    const { actions, press, setActive } = setup();
    setActive(false);
    for (const [key, mods] of [["s", { meta: true }], ["z", { meta: true }], ["z", { meta: true, shift: true }]] as const) {
      expect(press(key, mods)).not.toHaveBeenCalled(); // the browser keeps its own behaviour
    }
    expect([actions.save, actions.undo, actions.redo].every((f) => f.mock.calls.length === 0)).toBe(true);
    setActive(true);
    press("z", { meta: true });
    expect(actions.undo).toHaveBeenCalledTimes(1);
  });

  it("leaves undo inside a text field to the browser", () => {
    const { actions, press, inField } = setup();
    const field = {};
    inField.add(field);
    expect(press("z", { meta: true }, field)).not.toHaveBeenCalled();
    press("s", { meta: true }, field);
    expect(actions.undo).not.toHaveBeenCalled();
    expect(actions.save).toHaveBeenCalledTimes(1);
  });
});
