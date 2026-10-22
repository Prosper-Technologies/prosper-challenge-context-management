import { describe, expect, it, vi } from "vitest";
import { resource } from "../data";

const settle = () => new Promise((resolve) => setTimeout(resolve));

describe("resource", () => {
  it("loads once, on the first subscriber, and shares the result", async () => {
    const load = vi.fn(async () => 42);
    const r = resource(load);
    expect(load).not.toHaveBeenCalled();

    r.subscribe(() => undefined);
    r.subscribe(() => undefined);
    expect(r.get()).toEqual({ status: "loading" });
    await settle();
    expect(r.get()).toEqual({ status: "ready", data: 42 });
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("reports a failure and loads again on retry", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("down")).mockResolvedValueOnce("ok");
    const r = resource<string>(load);
    const listener = vi.fn();
    r.subscribe(listener);
    await settle();
    expect(r.get()).toEqual({ status: "failed", error: "down" });

    r.retry();
    expect(r.get()).toEqual({ status: "loading" });
    await settle();
    expect(r.get()).toEqual({ status: "ready", data: "ok" });
    expect(listener).toHaveBeenCalledTimes(3); // failed, loading, ready
  });
});
