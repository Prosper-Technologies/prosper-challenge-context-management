import type { AgentConfig } from "../../generated/contract";

/** greeting -> collect -> done, with fields the forms don't expose sprinkled in. */
export function agent(): AgentConfig {
  return {
    name: "Test",
    persona: "Be brief.",
    initial_node: "greeting",
    x_owner: "team",
    nodes: [
      {
        name: "greeting",
        task_messages: [{ role: "system", content: "Say hi.", x_note: "kept" }],
        pre_actions: [
          { type: "custom_log", level: "info" },
          { type: "tts_say", text: "Hello!", append_text_to_context: true },
        ],
        edges: [
          {
            function: "choose",
            description: "Pick.",
            target: "collect",
            properties: {
              intent: { type: "string", enum: ["book", "cancel"], x_hint: "kept" },
              age: { type: "integer", minimum: 0 },
              tags: { type: "array", items: { type: "string" } },
            },
            required: ["intent", "age"],
          },
        ],
        ui: { x: 0, y: 0 },
      },
      {
        name: "collect",
        task_messages: [{ role: "developer", content: "Ask for a name." }],
        edges: [{ function: "done", description: "Finish.", target: "done" }],
        ui: { x: 0, y: 200, x_pinned: true },
      },
      {
        name: "done",
        task_messages: [{ role: "developer", content: "Say bye." }],
        end: true,
        ui: { x: 0, y: 400 },
      },
    ],
  };
}
