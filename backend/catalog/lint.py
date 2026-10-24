#
# Catalog lint — health checks on the catalog data, reported in the same shape as
# agent issues ({level, path, message}, plus a code and the IDs involved), so the
# UI can show them in one kind of list.
#
#   errors    something a caller can ask for but never book: an appointment type
#             no one can be booked for, or one open to new patients with no
#             provider accepting them
#   warnings  data that invites mistakes: repeated names, "Dr." on NP/PA names,
#             confusable location names, and a capability no provider there uses
#             (e.g. physical therapy rooms with no PT provider)
#

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict

from .index import normalize
from .models import Catalog
from .policy import Policy

LintCode = Literal[
    "UNBOOKABLE_TYPE",
    "NEW_PATIENT_DEAD_END",
    "DUPLICATE_PROVIDER_NAME",
    "DR_TITLE_ON_NON_PHYSICIAN",
    "CONFUSABLE_LOCATIONS",
    "UNUSED_CAPABILITY",
]


class CatalogIssue(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    level: Literal["error", "warning"]
    code: LintCode
    # Locates the record in catalog.json, e.g. ["providers", 3, "name"].
    path: list[Union[str, int]]
    refs: list[str]  # IDs of the records involved
    message: str


def lint(catalog: Catalog, policy: Optional[Policy] = None) -> list[CatalogIssue]:
    policy = policy or Policy(catalog)
    issues: list[CatalogIssue] = []
    issues += _unbookable(catalog, policy)
    issues += _new_patient_dead_ends(catalog, policy)
    issues += _duplicate_names(catalog)
    issues += _dr_titles(catalog)
    issues += _confusable_locations(catalog)
    issues += _unused_capabilities(catalog, policy)
    return issues


def _unbookable(catalog: Catalog, policy: Policy) -> list[CatalogIssue]:
    out = []
    for i, t in enumerate(catalog.appointment_types):
        if policy.options(service_id=t.id):
            continue
        offering = [p for p in catalog.providers if t.id in p.appointment_type_ids]
        if not offering:
            why = "no provider offers it"
        else:
            why = f"its providers don't practice at a location with {t.required_capability}"
        out.append(
            CatalogIssue(
                level="error",
                code="UNBOOKABLE_TYPE",
                path=["appointment_types", i],
                refs=[t.id, *(p.id for p in offering)],
                message=f"{t.name} can't be booked: {why}.",
            )
        )
    return out


def _new_patient_dead_ends(catalog: Catalog, policy: Policy) -> list[CatalogIssue]:
    out = []
    for i, t in enumerate(catalog.appointment_types):
        providers = sorted({o.provider_id for o in policy.options(service_id=t.id)})
        if not t.new_patients_allowed or not providers:
            continue
        if not any(catalog.provider_by_id[p].accepting_new_patients for p in providers):
            out.append(
                CatalogIssue(
                    level="error",
                    code="NEW_PATIENT_DEAD_END",
                    path=["appointment_types", i, "new_patients_allowed"],
                    refs=[t.id, *providers],
                    message=f"{t.name} is open to new patients, but none of its providers are accepting new patients.",
                )
            )
    return out


def _duplicate_names(catalog: Catalog) -> list[CatalogIssue]:
    by_name: dict[tuple[str, ...], list[int]] = {}
    for i, p in enumerate(catalog.providers):
        by_name.setdefault(normalize(p.name), []).append(i)
    out = []
    for indices in by_name.values():
        if len(indices) < 2:
            continue
        people = [catalog.providers[i] for i in indices]
        details = "; ".join(f"{p.id} {p.title}, {p.specialty}" for p in people)
        out.append(
            CatalogIssue(
                level="warning",
                code="DUPLICATE_PROVIDER_NAME",
                path=["providers", indices[0], "name"],
                refs=[p.id for p in people],
                message=f"{len(people)} providers are named {people[0].name} ({details}).",
            )
        )
    return out


def _dr_titles(catalog: Catalog) -> list[CatalogIssue]:
    return [
        CatalogIssue(
            level="warning",
            code="DR_TITLE_ON_NON_PHYSICIAN",
            path=["providers", i, "name"],
            refs=[p.id],
            message=f"{p.name} is listed as {p.title}, but the name carries \"Dr.\".",
        )
        for i, p in enumerate(catalog.providers)
        if p.title in ("NP", "PA") and p.name.lower().startswith("dr")
    ]


def _confusable_locations(catalog: Catalog) -> list[CatalogIssue]:
    """Location names that start with the same word ("Mission Bay" / "Mission District")."""
    groups: dict[str, list[int]] = {}
    for i, loc in enumerate(catalog.locations):
        words = normalize(loc.name)
        if words:
            groups.setdefault(words[0], []).append(i)
    out = []
    for indices in groups.values():
        if len(indices) < 2:
            continue
        locations = [catalog.locations[i] for i in indices]
        out.append(
            CatalogIssue(
                level="warning",
                code="CONFUSABLE_LOCATIONS",
                path=["locations", indices[0], "name"],
                refs=[loc.id for loc in locations],
                message="Easy to confuse: " + " / ".join(loc.name for loc in locations) + ".",
            )
        )
    return out


def _unused_capabilities(catalog: Catalog, policy: Policy) -> list[CatalogIssue]:
    used = {
        (t.location_id, catalog.type_by_id[t.service_id].required_capability)
        for t in policy.triples
    }
    out = []
    for i, loc in enumerate(catalog.locations):
        for j, cap in enumerate(loc.capabilities):
            if (loc.id, cap) not in used:
                out.append(
                    CatalogIssue(
                        level="warning",
                        code="UNUSED_CAPABILITY",
                        path=["locations", i, "capabilities", j],
                        refs=[loc.id],
                        message=f"{loc.name} has {cap.replace('_', ' ')}, but no provider there offers a service that needs it.",
                    )
                )
    return out

