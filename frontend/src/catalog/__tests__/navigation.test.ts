import { describe, expect, it } from "vitest";
import type { Catalog, CatalogIssue } from "../../generated/contract";
import { crossRefs, issuesByRecord, lintTarget, matchesFilter } from "../navigation";

const catalog: Catalog = {
  policies: ["Providers only see patients at locations where they work."],
  locations: [
    { id: "loc_000", name: "Mission Bay", address: "1 Main St", city: "SF", phone: "1", hours: "9-5", capabilities: ["lab"] },
    { id: "loc_001", name: "Mission District", address: "2 Main St", city: "SF", phone: "2", hours: "9-5", capabilities: [] },
  ],
  providers: [
    {
      id: "prov_000",
      name: "Dr. Maria Garcia",
      title: "MD",
      specialty: "Pediatrics",
      location_ids: ["loc_000", "loc_001"],
      accepting_new_patients: true,
      languages: ["English"],
      appointment_type_ids: ["appt_000"],
    },
    {
      id: "prov_001",
      name: "Dr. Maria Garcia",
      title: "NP",
      specialty: "Family Medicine",
      location_ids: ["loc_001"],
      accepting_new_patients: false,
      languages: ["Spanish"],
      appointment_type_ids: ["appt_000"],
    },
  ],
  appointment_types: [
    {
      id: "appt_000",
      name: "Checkup",
      specialty: "Primary Care",
      duration_min: 30,
      requires_referral: false,
      new_patients_allowed: true,
      required_capability: null,
    },
    {
      id: "appt_001",
      name: "Eye Exam",
      specialty: "Ophthalmology",
      duration_min: 30,
      requires_referral: false,
      new_patients_allowed: true,
      required_capability: null,
    },
  ],
};

const issue = (path: (string | number)[], level: "error" | "warning" = "warning"): CatalogIssue => ({
  level,
  code: "DUPLICATE_PROVIDER_NAME",
  path,
  refs: [],
  message: "m",
});

describe("lintTarget", () => {
  it("points at the record in the issue's path", () => {
    expect(lintTarget(issue(["providers", 1, "name"]), catalog)).toEqual({ table: "providers", id: "prov_001" });
    expect(lintTarget(issue(["locations", 0, "capabilities", 0]), catalog)).toEqual({
      table: "locations",
      id: "loc_000",
    });
    expect(lintTarget(issue(["appointment_types", 1]), catalog)).toEqual({
      table: "appointment_types",
      id: "appt_001",
    });
  });

  it("is null for paths that don't name a record", () => {
    expect(lintTarget(issue(["policies", 0]), catalog)).toBeNull();
    expect(lintTarget(issue(["providers", 9]), catalog)).toBeNull();
    expect(lintTarget(issue(["providers"]), catalog)).toBeNull();
  });
});

it("groups issues by record id", () => {
  const byId = issuesByRecord([issue(["providers", 0]), issue(["providers", 0, "name"]), issue(["policies"])], catalog);
  expect([...byId.keys()]).toEqual(["prov_000"]);
  expect(byId.get("prov_000")).toHaveLength(2);
});

it("cross-references providers by location and appointment type", () => {
  const refs = crossRefs(catalog);
  expect(refs.providersAt.get("loc_001")).toEqual(["prov_000", "prov_001"]);
  expect(refs.providersOffering.get("appt_000")).toEqual(["prov_000", "prov_001"]);
  expect(refs.providersOffering.get("appt_001")).toBeUndefined();
  expect(refs.name.get("loc_000")).toBe("Mission Bay");
});

it("filters on every word, ignoring case", () => {
  expect(matchesFilter("", ["anything"])).toBe(true);
  expect(matchesFilter("maria np", ["Dr. Maria Garcia", "NP"])).toBe(true);
  expect(matchesFilter("maria pa", ["Dr. Maria Garcia", "NP"])).toBe(false);
  expect(matchesFilter("loc_001", ["prov_001", "loc_000", null, "loc_001"])).toBe(true);
});
