// The playground's tool forms, generated from each tool's argument schema (the one
// the LLM is given, from GET /api/catalog/tools).
//
// A field's value is its raw text: empty means "leave the argument out", and
// `null` means "send an explicit null", which matters for update_booking, where
// an omitted field stays and a null one is cleared. Booleans and enums are the
// text of the chosen option. Values are checked only as far as JSON needs (a
// number must be a number); everything else is the tool's to accept or reject,
// so the playground shows the same error the model would get.

type JsonSchema = Record<string, unknown>;

export type FieldKind = "string" | "date" | "integer" | "number" | "boolean" | "enum" | "object";

export interface FieldSpec {
  key: string; // the path, dot-joined: "patient.has_referral"
  name: string;
  kind: FieldKind;
  nullable: boolean;
  required: boolean;
  description?: string;
  options?: string[]; // enum values
  fields?: FieldSpec[]; // an object's properties
}

/** Raw text per field key; missing or "" is omitted, null is an explicit null. */
export type FormValues = Record<string, string | null>;

function isObject(value: unknown): value is JsonSchema {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function typesOf(schema: JsonSchema): string[] {
  const { type } = schema;
  if (typeof type === "string") return [type];
  return Array.isArray(type) ? type.filter((t): t is string => typeof t === "string") : [];
}

export function formFields(schema: unknown, prefix = ""): FieldSpec[] {
  if (!isObject(schema) || !isObject(schema.properties)) return [];
  const required = Array.isArray(schema.required) ? schema.required : [];
  return Object.entries(schema.properties).flatMap(([name, property]) => {
    if (!isObject(property)) return [];
    const key = prefix + name;
    const types = typesOf(property);
    const base = types.find((t) => t !== "null") ?? "string";
    const spec: FieldSpec = {
      key,
      name,
      kind: "string",
      nullable: types.includes("null"),
      required: required.includes(name),
      description: typeof property.description === "string" ? property.description : undefined,
    };
    if (Array.isArray(property.enum)) {
      spec.kind = "enum";
      spec.options = property.enum.filter((v) => v !== null).map(String);
    } else if (base === "object") {
      spec.kind = "object";
      spec.fields = formFields(property, `${key}.`);
    } else if (base === "integer" || base === "number" || base === "boolean") {
      spec.kind = base;
    } else if (property.format === "date") {
      spec.kind = "date";
    }
    return [spec];
  });
}

export class FormError extends Error {}

function valueOf(field: FieldSpec, text: string): unknown {
  switch (field.kind) {
    case "integer":
    case "number": {
      const n = Number(text.trim());
      if (text.trim() === "" || !Number.isFinite(n) || (field.kind === "integer" && !Number.isInteger(n)))
        throw new FormError(`${field.key} must be ${field.kind === "integer" ? "an integer" : "a number"}.`);
      return n;
    }
    case "boolean":
      return text === "true";
    default:
      return text;
  }
}

/** The tool arguments the form describes; throws `FormError` for a malformed number. */
export function buildArgs(fields: FieldSpec[], values: FormValues): Record<string, unknown> {
  const args: Record<string, unknown> = {};
  for (const field of fields) {
    const raw = values[field.key];
    if (raw === null) {
      args[field.name] = null;
    } else if (field.kind === "object") {
      const nested = buildArgs(field.fields ?? [], values);
      if (Object.keys(nested).length > 0) args[field.name] = nested;
    } else if (raw !== undefined && raw !== "") {
      args[field.name] = valueOf(field, raw);
    }
  }
  return args;
}
