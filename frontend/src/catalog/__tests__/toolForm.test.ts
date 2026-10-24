import { describe, expect, it } from "vitest";
import { FormError, buildArgs, formFields } from "../toolForm";

// Shapes as GET /api/catalog/tools serves them.
const updateBooking = {
  type: "object",
  additionalProperties: false,
  required: [],
  properties: {
    patient: {
      type: ["object", "null"],
      additionalProperties: false,
      properties: {
        is_new_patient: { type: ["boolean", "null"], description: "Never seen here." },
        has_referral: { type: ["boolean", "null"] },
      },
    },
    service_id: { type: ["string", "null"], description: "A service id." },
    slot_id: { type: ["string", "null"] },
  },
};

const bookAppointment = {
  type: "object",
  required: ["draft_revision", "confirmed"],
  properties: {
    draft_revision: { type: "integer" },
    confirmed: { type: "boolean" },
  },
};

const getAvailability = {
  type: "object",
  properties: {
    day: { type: ["string", "null"], format: "date" },
    part_of_day: { type: ["string", "null"], enum: ["morning", "afternoon"] },
  },
};

describe("formFields", () => {
  it("describes each argument's kind, nullability and requiredness", () => {
    const fields = formFields(updateBooking);
    expect(fields.map((f) => [f.key, f.kind, f.nullable, f.required])).toEqual([
      ["patient", "object", true, false],
      ["service_id", "string", true, false],
      ["slot_id", "string", true, false],
    ]);
    expect(fields[0].fields!.map((f) => [f.key, f.kind, f.description])).toEqual([
      ["patient.is_new_patient", "boolean", "Never seen here."],
      ["patient.has_referral", "boolean", undefined],
    ]);
    expect(formFields(bookAppointment).map((f) => [f.kind, f.required])).toEqual([
      ["integer", true],
      ["boolean", true],
    ]);
  });

  it("knows dates and enums", () => {
    const [day, part] = formFields(getAvailability);
    expect(day.kind).toBe("date");
    expect(part).toMatchObject({ kind: "enum", options: ["morning", "afternoon"], nullable: true });
  });

  it("has no fields for a tool without arguments", () => {
    expect(formFields({ type: "object", properties: {} })).toEqual([]);
    expect(formFields(null)).toEqual([]);
  });
});

describe("buildArgs", () => {
  it("leaves empty fields out and sends explicit nulls", () => {
    const fields = formFields(updateBooking);
    expect(buildArgs(fields, {})).toEqual({});
    expect(buildArgs(fields, { service_id: "", slot_id: null })).toEqual({ slot_id: null });
    expect(buildArgs(fields, { service_id: "appt_001" })).toEqual({ service_id: "appt_001" });
  });

  it("builds nested objects only from fields that are set", () => {
    const fields = formFields(updateBooking);
    expect(buildArgs(fields, { "patient.has_referral": "true" })).toEqual({
      patient: { has_referral: true },
    });
    expect(buildArgs(fields, { "patient.is_new_patient": null, "patient.has_referral": "false" })).toEqual({
      patient: { is_new_patient: null, has_referral: false },
    });
    expect(buildArgs(fields, { patient: null, "patient.has_referral": "true" })).toEqual({ patient: null });
  });

  it("parses numbers and refuses text that isn't one", () => {
    const fields = formFields(bookAppointment);
    expect(buildArgs(fields, { draft_revision: "3", confirmed: "true" })).toEqual({
      draft_revision: 3,
      confirmed: true,
    });
    expect(() => buildArgs(fields, { draft_revision: "2.5" })).toThrow(FormError);
    expect(() => buildArgs(fields, { draft_revision: "three" })).toThrow("an integer");
  });

  it("leaves a missing required field to the tool to reject", () => {
    expect(buildArgs(formFields(bookAppointment), { confirmed: "false" })).toEqual({ confirmed: false });
  });
});
