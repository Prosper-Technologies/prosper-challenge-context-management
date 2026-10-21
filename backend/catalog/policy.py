#
# Policy engine — the catalog's booking rules as code.
#
#   triples     every statically valid (service, provider, location), rules 1-3:
#               the provider offers the service, practices at the location, and the
#               location has the capability the service needs
#   feasible()  those triples, with known patient facts applied (rules 4-5); facts
#               that are still unknown are reported as conditions, never assumed
#   explain()   why the current draft can't be booked: violations with a fix each
#   narrow()    ready | needs_choice | blocked, and the one question to ask next
#
# Rule 6 (disambiguate a multi-location provider's location) is `narrow()`: a draft
# is only `ready` once service, provider and location are all chosen. `ready` means
# the combination is bookable for this caller, not that a time is picked or booked.
#
# The engine never chooses for the caller. It narrows the options and says which
# question is missing; the session records explicit choices.
#

from functools import cache
from typing import Literal, NamedTuple, Optional

from pydantic import BaseModel, ConfigDict

from .models import (
    CHOICE_FIELDS,
    AppointmentType,
    BookingDraft,
    Catalog,
    Location,
    PatientFacts,
    Provider,
    default_catalog,
)

ViolationCode = Literal[
    "PROVIDER_NOT_AT_LOCATION",
    "PROVIDER_DOESNT_OFFER",
    "LOCATION_LACKS_CAPABILITY",
    "REFERRAL_REQUIRED",
    "TYPE_NOT_FOR_NEW_PATIENTS",
    "PROVIDER_NOT_ACCEPTING",
    "NOT_OFFERED",
    # The choices are compatible pairwise, but nothing completes them (e.g. no
    # cardiologist works at a location that needs no capability for cardiology).
    "NO_FEASIBLE_OPTION",
]
ChoiceField = Literal["service_id", "provider_id", "location_id"]
Fact = Literal["has_referral", "is_new_patient"]
FACTS: tuple[Fact, ...] = ("has_referral", "is_new_patient")  # the order they're asked in

NARROW_LIMIT = 3


class Triple(NamedTuple):
    service_id: str
    provider_id: str
    location_id: str


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)


class Violation(_Out):
    code: ViolationCode
    message: str
    fix: str
    # The draft choices and patient facts the violation is about.
    fields: tuple[ChoiceField, ...] = ()
    facts: tuple[Fact, ...] = ()


class Option(_Out):
    """A way to complete the draft. Fields that differ within the group are None."""

    service_id: str
    provider_id: Optional[str]
    location_id: Optional[str]
    # Unknown patient facts that decide whether this option is bookable.
    conditions: tuple[Fact, ...] = ()


class Question(_Out):
    kind: Literal["fact", "choice"]
    field: Literal[ChoiceField, Fact]
    prompt: str


class Narrowing(_Out):
    status: Literal["ready", "needs_choice", "blocked"]
    next_question: Optional[Question]
    # Options for the field in `next_question` (the bookable combination when ready).
    options: tuple[Option, ...]
    total_options: int
    truncated: bool
    # How many distinct values each choice still has among the feasible options.
    remaining: dict[ChoiceField, int]
    violations: tuple[Violation, ...]
    # Patient facts some remaining option depends on.
    unresolved: tuple[Fact, ...]


class UnknownId(ValueError):
    def __init__(self, field: str, value: str):
        self.field = field
        self.value = value
        super().__init__(f"Unknown {field.removesuffix('_id')} id {value!r}")


class Policy:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        locations = catalog.location_by_id
        types = catalog.type_by_id
        self.triples: tuple[Triple, ...] = tuple(
            sorted(
                Triple(t, p.id, loc)
                for p in catalog.providers
                for t in p.appointment_type_ids
                for loc in p.location_ids
                if _capable(types[t], locations[loc])
            )
        )
        self._offered = {t.service_id for t in self.triples}

    # ---- lookups ----------------------------------------------------------------

    def check_ids(self, draft: BookingDraft) -> None:
        """Raise UnknownId for a draft choice that isn't in the catalog."""
        maps = {
            "service_id": self.catalog.type_by_id,
            "provider_id": self.catalog.provider_by_id,
            "location_id": self.catalog.location_by_id,
        }
        for field, by_id in maps.items():
            value = getattr(draft, field)
            if value is not None and value not in by_id:
                raise UnknownId(field, value)

    def options(
        self,
        service_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        location_id: Optional[str] = None,
    ) -> list[Triple]:
        """Statically valid triples matching the given choices (patient facts ignored)."""
        return [
            t
            for t in self.triples
            if (service_id is None or t.service_id == service_id)
            and (provider_id is None or t.provider_id == provider_id)
            and (location_id is None or t.location_id == location_id)
        ]

    def feasible(self, draft: BookingDraft, patient: PatientFacts) -> list[tuple[Triple, tuple[Fact, ...]]]:
        """Triples matching the draft that known facts allow, each with the unknown
        facts it still depends on."""
        result = []
        for triple in self.options(draft.service_id, draft.provider_id, draft.location_id):
            violations, conditions = self._eligibility(patient, triple.service_id, triple.provider_id)
            if not violations:
                result.append((triple, conditions))
        return result

    # ---- explain ----------------------------------------------------------------

    def conflicts(self, draft: BookingDraft, patient: PatientFacts) -> list[Violation]:
        """Violations of the choices themselves: each rule checked on the fields it
        involves. A session uses `fields` to decide which stale choice to clear."""
        self.check_ids(draft)
        s, p, loc = self._entities(draft)
        out = []
        if s and s.id not in self._offered:
            out.append(self._not_offered(s))
        if s and p and s.id not in p.appointment_type_ids:
            out.append(self._doesnt_offer(s, p))
        if p and loc and loc.id not in p.location_ids:
            out.append(self._not_at_location(p, loc))
        if s and loc and not _capable(s, loc):
            out.append(self._lacks_capability(s, loc, p))
        if s:
            out += self._service_eligibility(patient, s)
        if p and patient.is_new_patient is True and not p.accepting_new_patients:
            out.append(self._not_accepting(p, s))
        return out

    def explain(self, draft: BookingDraft, patient: PatientFacts) -> list[Violation]:
        """Why the draft can't be booked. Empty if some option still completes it."""
        out = self.conflicts(draft, patient)
        if out or self.feasible(draft, patient):
            return out
        # Each choice is fine on its own, but no option satisfies all of them.
        static = self.options(draft.service_id, draft.provider_id, draft.location_id)
        desc = self._describe(draft)
        if not static:
            return [
                Violation(
                    code="NO_FEASIBLE_OPTION",
                    message=f"Nothing can be booked for {desc}.",
                    fix="Change one of the choices; search again with fewer constraints.",
                    fields=tuple(f for f in CHOICE_FIELDS if getattr(draft, f) is not None),
                )
            ]
        codes: dict[ViolationCode, Violation] = {}
        for t in static:
            violations, _ = self._eligibility(patient, t.service_id, t.provider_id)
            for v in violations:
                codes.setdefault(v.code, v)
        return [self._aggregate(v, desc) for v in codes.values()]

    # ---- narrow -----------------------------------------------------------------

    def narrow(self, draft: BookingDraft, patient: PatientFacts, limit: int = NARROW_LIMIT) -> Narrowing:
        violations = self.explain(draft, patient)
        feasible = [] if violations else self.feasible(draft, patient)
        remaining = {
            field: len({getattr(t, field) for t, _ in feasible}) for field in CHOICE_FIELDS
        }
        unresolved = tuple(f for f in FACTS if any(f in c for _, c in feasible))
        if violations:
            return Narrowing(
                status="blocked",
                next_question=None,
                options=(),
                total_options=0,
                truncated=False,
                remaining=remaining,
                violations=tuple(violations),
                unresolved=(),
            )

        question = self._next_question(draft, unresolved, feasible)
        if question is None:
            ((triple, _),) = feasible  # all three chosen: exactly one triple
            options = [Option(**triple._asdict())]
        else:
            # A fact question lists the options for the next choice it would unlock.
            unset = [f for f in CHOICE_FIELDS if getattr(draft, f) is None]
            field = question.field if question.kind == "choice" else (unset[0] if unset else None)
            options = _group(feasible, field)
        return Narrowing(
            status="ready" if question is None else "needs_choice",
            next_question=question,
            options=tuple(options[:limit]),
            total_options=len(options),
            truncated=len(options) > limit,
            remaining=remaining,
            violations=(),
            unresolved=unresolved,
        )

    def _next_question(self, draft, unresolved, feasible) -> Optional[Question]:
        names = self.catalog
        if draft.service_id is None:
            return Question(kind="choice", field="service_id", prompt="Ask what kind of appointment the caller needs.")
        service = names.type_by_id[draft.service_id].name
        if "has_referral" in unresolved:
            return Question(
                kind="fact", field="has_referral", prompt=f"Ask whether the caller has a referral on file for {service}."
            )
        if "is_new_patient" in unresolved:
            return Question(
                kind="fact",
                field="is_new_patient",
                prompt="Ask whether the caller has been seen at one of our clinics before (new or existing patient).",
            )
        if draft.provider_id is None:
            return Question(kind="choice", field="provider_id", prompt=f"Ask which provider the caller wants for {service}.")
        if draft.location_id is None:
            provider = names.provider_by_id[draft.provider_id].name
            locations = sorted({t.location_id for t, _ in feasible})
            if len(locations) == 1:
                where = names.location_by_id[locations[0]].name
                prompt = f"{provider} offers {service} only at {where}; check that location suits the caller."
            else:
                prompt = f"Ask which location the caller wants; {provider} offers {service} at {len(locations)}."
            return Question(kind="choice", field="location_id", prompt=prompt)
        return None

    # ---- rules ------------------------------------------------------------------

    def _eligibility(self, patient: PatientFacts, service_id: str, provider_id: str):
        """Rules 4-5 for one service and provider: (violations, unknown facts it depends on)."""
        s = self.catalog.type_by_id[service_id]
        p = self.catalog.provider_by_id[provider_id]
        violations = self._service_eligibility(patient, s)
        if patient.is_new_patient is True and not p.accepting_new_patients:
            violations.append(self._not_accepting(p, s))
        conditions: list[Fact] = []
        if s.requires_referral and patient.has_referral is None:
            conditions.append("has_referral")
        if patient.is_new_patient is None and not (s.new_patients_allowed and p.accepting_new_patients):
            conditions.append("is_new_patient")
        return violations, tuple(conditions)

    def _service_eligibility(self, patient: PatientFacts, s: AppointmentType) -> list[Violation]:
        out = []
        if s.requires_referral and patient.has_referral is False:
            out.append(
                Violation(
                    code="REFERRAL_REQUIRED",
                    message=f"{s.name} needs a referral on file before it can be booked.",
                    fix="The caller needs a referral from their doctor first, or a service that doesn't need one.",
                    fields=("service_id",),
                    facts=("has_referral",),
                )
            )
        if patient.is_new_patient is True and not s.new_patients_allowed:
            alternatives = [
                t.name
                for t in self.catalog.appointment_types
                if t.specialty == s.specialty and t.new_patients_allowed and t.id in self._offered
            ]
            fix = "New patients need a different appointment type"
            fix += f", e.g. {_names(alternatives)}." if alternatives else "."
            out.append(
                Violation(
                    code="TYPE_NOT_FOR_NEW_PATIENTS",
                    message=f"{s.name} is only for existing patients.",
                    fix=fix,
                    fields=("service_id",),
                    facts=("is_new_patient",),
                )
            )
        return out

    def _not_accepting(self, p: Provider, s: Optional[AppointmentType]) -> Violation:
        fix = "Choose a provider who is accepting new patients"
        if s is not None:
            others = sorted(
                {
                    t.provider_id
                    for t in self.options(service_id=s.id)
                    if self.catalog.provider_by_id[t.provider_id].accepting_new_patients
                }
            )
            fix += f" for {s.name}: {_names([self._provider(i) for i in others])}." if others else (
                f"; none who offer {s.name} are."
            )
        else:
            fix += "."
        return Violation(
            code="PROVIDER_NOT_ACCEPTING",
            message=f"{self._provider(p.id)} isn't accepting new patients.",
            fix=fix,
            fields=("provider_id",),
            facts=("is_new_patient",),
        )

    def _not_offered(self, s: AppointmentType) -> Violation:
        return Violation(
            code="NOT_OFFERED",
            message=f"No provider currently offers {s.name}.",
            fix="Offer a different service or a handoff to clinic staff.",
            fields=("service_id",),
        )

    def _doesnt_offer(self, s: AppointmentType, p: Provider) -> Violation:
        others = sorted({t.provider_id for t in self.options(service_id=s.id)})
        fix = f"{s.name} is offered by {_names([self._provider(i) for i in others])}." if others else (
            f"No provider offers {s.name}."
        )
        return Violation(
            code="PROVIDER_DOESNT_OFFER",
            message=f"{self._provider(p.id)} doesn't offer {s.name}.",
            fix=fix,
            fields=("service_id", "provider_id"),
        )

    def _not_at_location(self, p: Provider, loc: Location) -> Violation:
        at = _names([self.catalog.location_by_id[i].name for i in p.location_ids])
        return Violation(
            code="PROVIDER_NOT_AT_LOCATION",
            message=f"{self._provider(p.id)} doesn't practice at {loc.name}.",
            fix=f"Choose one of their locations ({at}) or another provider at {loc.name}.",
            fields=("provider_id", "location_id"),
        )

    def _lacks_capability(self, s: AppointmentType, loc: Location, p: Optional[Provider]) -> Violation:
        cap = s.required_capability.replace("_", " ")
        where = sorted({t.location_id for t in self.options(service_id=s.id, provider_id=p.id if p else None)})
        names = _names([self.catalog.location_by_id[i].name for i in where])
        if p is not None and where:
            fix = f"{self._provider(p.id)} offers {s.name} at {names}."
        elif where:
            fix = f"{s.name} can be booked at {names}."
        else:
            fix = f"{s.name} can't be booked anywhere{' with this provider' if p else ''}."
        return Violation(
            code="LOCATION_LACKS_CAPABILITY",
            message=f"{s.name} needs {cap}, which {loc.name} doesn't have.",
            fix=fix,
            fields=("service_id", "location_id"),
        )

    def _aggregate(self, v: Violation, desc: str) -> Violation:
        """A rule that rules out every remaining option, reported once for the draft."""
        messages = {
            "REFERRAL_REQUIRED": f"Every option for {desc} needs a referral on file.",
            "TYPE_NOT_FOR_NEW_PATIENTS": f"Every option for {desc} is only for existing patients.",
            "PROVIDER_NOT_ACCEPTING": f"No provider who offers {desc} is accepting new patients.",
        }
        fixes = {
            "REFERRAL_REQUIRED": "The caller needs a referral first, or a service that doesn't need one.",
            "TYPE_NOT_FOR_NEW_PATIENTS": "Choose an appointment type open to new patients.",
            "PROVIDER_NOT_ACCEPTING": "Offer a different service, or a handoff to clinic staff.",
        }
        return v.model_copy(update={"message": messages[v.code], "fix": fixes[v.code], "fields": ()})

    # ---- names ------------------------------------------------------------------

    def _entities(self, draft: BookingDraft):
        c = self.catalog
        return (
            c.type_by_id.get(draft.service_id) if draft.service_id else None,
            c.provider_by_id.get(draft.provider_id) if draft.provider_id else None,
            c.location_by_id.get(draft.location_id) if draft.location_id else None,
        )

    def _provider(self, provider_id: str) -> str:
        """Names repeat across providers, so mention role and specialty too."""
        p = self.catalog.provider_by_id[provider_id]
        return f"{p.name} ({p.title}, {p.specialty})"

    def _describe(self, draft: BookingDraft) -> str:
        s, p, loc = self._entities(draft)
        parts = [s.name if s else "any service"]
        if p:
            parts.append(f"with {self._provider(p.id)}")
        if loc:
            parts.append(f"at {loc.name}")
        return " ".join(parts)


def _capable(s: AppointmentType, loc: Location) -> bool:
    return s.required_capability is None or s.required_capability in loc.capabilities


def _group(feasible: list[tuple[Triple, tuple[Fact, ...]]], field: Optional[ChoiceField]) -> list[Option]:
    """One option per distinct value of `field` (per triple when None). Other fields
    are filled in when they're the same across the group."""
    groups: dict[object, list[tuple[Triple, tuple[Fact, ...]]]] = {}
    for triple, conditions in feasible:
        key = getattr(triple, field) if field else triple
        groups.setdefault(key, []).append((triple, conditions))
    options = []
    for key in sorted(groups):
        members = groups[key]
        values = {f: {getattr(t, f) for t, _ in members} for f in CHOICE_FIELDS}
        conditions = {c for _, cs in members for c in cs}
        options.append(
            Option(
                service_id=members[0][0].service_id,  # always chosen, or the grouping field
                provider_id=next(iter(values["provider_id"])) if len(values["provider_id"]) == 1 else None,
                location_id=next(iter(values["location_id"])) if len(values["location_id"]) == 1 else None,
                conditions=tuple(f for f in FACTS if f in conditions),
            )
        )
    return options


def _names(names: list[str], limit: int = 3) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


@cache
def default_policy() -> Policy:
    return Policy(default_catalog())
