#
# Catalog tools — the scheduler's tools and edge guards, as one registry shared by
# every adapter (voice: catalog_voice.py; HTTP: catalog_api.py; evaluation later).
#
#   Tool    name, description, argument model, result model, and a handler taking a
#           ToolContext (the caller's session, a calendar and the index)
#   Guard   a named check on the session that a graph edge can require before it
#           transitions (service_chosen, booking_ready, slot_selected,
#           booking_completed)
#
# Adapters call `Tool.run(ctx, raw_args)` and get JSON back. Arguments are validated
# by the argument models only, and every failure is a structured `{"error": {...}}`
# result rather than an exception, so the model can recover from it. Tool schemas
# for the LLM come from the same models (`Tool.parameters()`).
#
# Results are compact projections, not catalog entities: at most 5 candidates (3
# for narrowing options and times), with match counts, ambiguity, truncation and a
# cursor or a refinement hint wherever something was left out. Fields at their
# default (no hint, nothing excluded, not truncated) are left out of the JSON.
#
# Searches never write to the session; get_availability records the times it
# offers; update_booking and book_appointment own every other change.
#

import json
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .availability import CLINIC_TZ, Calendar, PartOfDay, Slot
from .index import CatalogIndex, Excluded, SearchResult
from .models import BookingDraft, Catalog, Location, Provider
from .policy import FACTS, Fact, Narrowing, Policy, Question, Violation
from .session import BookingError, BookingUpdate, Cleared, InvalidUpdate, SchedulingSession

AVAILABILITY_LIMIT = 3
OTHER_DAYS_LIMIT = 5


@dataclass
class ToolContext:
    """What a tool call works on: one caller's session and the calendar it books in."""

    session: SchedulingSession
    calendar: Calendar
    index: CatalogIndex

    @property
    def policy(self) -> Policy:
        return self.session.policy

    @property
    def catalog(self) -> Catalog:
        return self.session.policy.catalog


# ---- arguments --------------------------------------------------------------------


class _Args(BaseModel):
    # Not strict: arguments come from an LLM, so "3" is accepted for 3. Unknown
    # arguments are rejected rather than ignored.
    model_config = ConfigDict(extra="forbid")


Cursor = Annotated[str, Field(pattern=r"^\d{1,4}$", description="next_cursor from the previous result.")]
ServiceId = Annotated[str, Field(description="A service id from search_services.")]
ProviderId = Annotated[str, Field(description="A provider id from find_providers.")]
LocationId = Annotated[str, Field(description="A location id from find_locations.")]


class OverviewArgs(_Args):
    cursor: Optional[Cursor] = None


class SearchServicesArgs(_Args):
    query: str = Field(min_length=1, description="What the caller asked for, in their words (e.g. 'knee MRI').")
    specialty: Optional[str] = Field(None, description="Only services with this specialty label.")
    cursor: Optional[Cursor] = None


class FindProvidersArgs(_Args):
    name: Optional[str] = Field(None, description="The name the caller said, e.g. 'Dr. Chen'.")
    service_id: Optional[ServiceId] = None
    location_id: Optional[LocationId] = None
    language: Optional[str] = None
    specialty: Optional[str] = Field(None, description="The provider's own specialty label.")
    cursor: Optional[Cursor] = None


class FindLocationsArgs(_Args):
    query: Optional[str] = Field(None, description="The place the caller said, e.g. 'downtown'.")
    service_id: Optional[ServiceId] = None
    provider_id: Optional[ProviderId] = None
    cursor: Optional[Cursor] = None


class PatientArgs(_Args):
    is_new_patient: Optional[bool] = Field(None, description="True if the caller has never been seen at our clinics.")
    has_referral: Optional[bool] = Field(None, description="Whether a referral is on file for the service.")


class UpdateBookingArgs(_Args):
    patient: Optional[PatientArgs] = Field(None, description="Facts the caller stated; null for one fact clears it.")
    service_id: Optional[ServiceId] = None
    provider_id: Optional[ProviderId] = None
    location_id: Optional[LocationId] = None
    slot_id: Optional[str] = Field(None, description="A slot id from get_availability that the caller picked.")


class CheckBookingArgs(_Args):
    pass


class AvailabilityArgs(_Args):
    day: Optional[date] = Field(None, description="Only this local date (YYYY-MM-DD).")
    part_of_day: Optional[PartOfDay] = None


class BookAppointmentArgs(_Args):
    draft_revision: int = Field(description="The draft revision the caller confirmed.")
    confirmed: bool = Field(description="True only after the caller said yes to the read-back.")


# ---- results ----------------------------------------------------------------------


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class Ref(_Out):
    id: str
    name: str


class ServiceCard(_Out):
    id: str
    name: str
    specialty: str
    duration_min: int
    requires_referral: bool
    new_patients_allowed: bool
    notes: tuple[str, ...] = ()


class ProviderCard(_Out):
    id: str
    name: str
    title: str
    specialty: str
    accepting_new_patients: bool
    languages: tuple[str, ...]
    # id -> name; with a service filter, only where they can be booked for it.
    locations: dict[str, str]


class LocationCard(_Out):
    id: str
    name: str
    capabilities: tuple[str, ...] = ()
    # Unknown patient facts that decide whether the requested service is bookable here.
    conditions: tuple[Fact, ...] = ()


class _Page(_Out):
    total_matches: int
    ambiguous: bool = False
    clarify: Optional[str] = None  # what tells the equally good matches apart
    truncated: bool = False
    next_cursor: Optional[str] = None
    excluded: tuple[Excluded, ...] = ()  # named matches a filter ruled out, and why
    hint: Optional[str] = None


class ServiceSearch(_Page):
    services: tuple[ServiceCard, ...]


class ProviderSearch(_Page):
    providers: tuple[ProviderCard, ...]


class LocationSearch(_Page):
    locations: tuple[LocationCard, ...]
    violations: tuple[Violation, ...] = ()


class Overview(_Out):
    section: Literal["locations", "specialties"]
    locations: tuple[LocationCard, ...] = ()
    # Bookable services per specialty label, and specialties nobody offers.
    specialties: dict[str, int] = Field(default_factory=dict)
    not_offered: tuple[str, ...] = ()
    next_cursor: Optional[str] = None
    hint: Optional[str] = None


class Facts(_Out):
    """The caller's facts; null means not known yet."""

    is_new_patient: Optional[bool]
    has_referral: Optional[bool]


class SlotView(_Out):
    id: str
    start: str  # local ISO time
    label: str  # how to say it


class DraftView(_Out):
    revision: int
    patient: Facts
    service: Optional[Ref] = None
    provider: Optional[Ref] = None
    location: Optional[Ref] = None
    slot: Optional[SlotView] = None
    confirmed: bool = False
    confirmation: Optional[str] = None  # set once booked


class OptionView(_Out):
    service_id: str
    provider_id: Optional[str] = None
    location_id: Optional[str] = None
    label: str
    conditions: tuple[Fact, ...] = ()


class StatusView(_Out):
    status: Literal["ready", "needs_choice", "blocked"]
    next_question: Optional[Question] = None
    options: tuple[OptionView, ...] = ()
    total_options: int = 0
    truncated: bool = False
    violations: tuple[Violation, ...] = ()
    hint: Optional[str] = None


class BookingView(_Out):
    draft: DraftView
    check: StatusView


class UpdateView(BookingView):
    changed: bool
    cleared: tuple[Cleared, ...] = ()


class Availability(_Out):
    revision: int
    today: str
    slots: tuple[SlotView, ...]
    total: int
    truncated: bool = False
    other_days: tuple[str, ...] = ()  # other days with openings, to refine by
    hint: Optional[str] = None


class ReceiptView(_Out):
    confirmation: str
    service: str
    provider: str
    location: str
    start: str
    duration_min: int


class ArgError(_Out):
    path: list[Union[str, int]]
    message: str


class ErrorDetail(_Out):
    code: str
    message: str
    errors: tuple[ArgError, ...] = ()
    violations: tuple[Violation, ...] = ()
    next_question: Optional[Question] = None
    hint: Optional[str] = None


class ToolErrorResult(_Out):
    error: ErrorDetail


class ToolError(Exception):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.detail = ErrorDetail(code=code, message=message, **detail)


def dump(model: BaseModel) -> dict:
    return model.model_dump(mode="json", exclude_defaults=True)


def approx_tokens(value: Any) -> int:
    """A rough token count for a JSON value (about 4 characters a token). An estimate,
    not a tokenizer: evaluation uses the provider's reported usage."""
    return math.ceil(len(json.dumps(value, separators=(",", ":"))) / 4)


# ---- the registry -----------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[_Args]
    result: type[_Out]
    handler: Callable[[ToolContext, Any], _Out]

    def parameters(self) -> dict:
        """The argument schema as an LLM tool takes it: inline, without titles."""
        return _llm_schema(self.args)

    def run(self, ctx: ToolContext, args: Any) -> dict:
        """Validate `args`, run the handler and return its JSON result, or a
        structured error. Rejected calls leave the session unchanged."""
        try:
            parsed = self.args.model_validate({} if args is None else args)
        except ValidationError as e:
            errors = tuple(ArgError(path=list(err["loc"]), message=err["msg"]) for err in e.errors())
            detail = ErrorDetail(
                code="INVALID_ARGUMENTS", message=f"Invalid arguments for {self.name}.", errors=errors
            )
            return dump(ToolErrorResult(error=detail))
        try:
            return dump(self.handler(ctx, parsed))
        except ToolError as e:
            return dump(ToolErrorResult(error=e.detail))
        except InvalidUpdate as e:
            errors = tuple(ArgError(path=err["path"], message=err["message"]) for err in e.errors)
            detail = ErrorDetail(code="INVALID_UPDATE", message="Nothing was changed.", errors=errors)
            return dump(ToolErrorResult(error=detail))
        except BookingError as e:
            return dump(ToolErrorResult(error=_booking_error(ctx, e)))


@dataclass(frozen=True)
class Guard:
    name: str
    description: str
    check: Callable[[SchedulingSession], Optional[str]]  # why the edge can't be taken yet
    # Agents exposing any of these tools must end only through this guard (see
    # agent_builder.registry.GuardBinding).
    completes: tuple[str, ...] = ()

    def run(self, session: SchedulingSession) -> Optional[dict]:
        """None if the edge may be taken, else what's missing and how to recover."""
        message = self.check(session)
        if message is None:
            return None
        narrowing = session.check()
        failure = {"guard": self.name, "message": message}
        if narrowing.next_question is not None:
            failure["next_question"] = narrowing.next_question.model_dump(mode="json")
        if narrowing.violations:
            failure["violations"] = [dump(v) for v in narrowing.violations]
        return failure


# ---- handlers: catalog searches (read-only) ---------------------------------------


def catalog_overview(ctx: ToolContext, args: OverviewArgs) -> Overview:
    c, offered = ctx.catalog, _offered(ctx.policy)
    if args.cursor in (None, "0"):
        locations = tuple(_location_card(loc) for loc in c.locations)
        return Overview(section="locations", locations=locations, next_cursor="1", hint="Cursor 1 lists specialties.")
    if args.cursor != "1":
        raise ToolError("INVALID_CURSOR", "The overview has pages 0 (locations) and 1 (specialties).")
    counts = Counter(t.specialty for t in c.appointment_types if t.id in offered)
    none = sorted({t.specialty for t in c.appointment_types} - set(counts))
    return Overview(
        section="specialties",
        specialties=dict(sorted(counts.items())),
        not_offered=tuple(none),
        hint="search_services(specialty=...) lists a specialty's services.",
    )


def search_services(ctx: ToolContext, args: SearchServicesArgs) -> ServiceSearch:
    result = ctx.index.services(args.query, specialty=args.specialty, cursor=args.cursor)
    types, offered = ctx.catalog.type_by_id, _offered(ctx.policy)
    cards = []
    for m in result.matches:
        t = types[m.id]
        notes = []
        if t.id not in offered:
            notes.append("No provider offers this, so it can't be booked.")
        elif t.required_capability:
            where = sorted({tr.location_id for tr in ctx.policy.options(service_id=t.id)})
            notes.append(f"Needs {t.required_capability.replace('_', ' ')}: bookable at {len(where)} location(s).")
        cards.append(
            ServiceCard(
                id=t.id,
                name=t.name,
                specialty=t.specialty,
                duration_min=t.duration_min,
                requires_referral=t.requires_referral,
                new_patients_allowed=t.new_patients_allowed,
                notes=tuple(notes),
            )
        )
    return ServiceSearch(services=tuple(cards), **_page(result, "services"))


def find_providers(ctx: ToolContext, args: FindProvidersArgs) -> ProviderSearch:
    _check_ids(ctx, service_id=args.service_id, location_id=args.location_id)
    result = ctx.index.providers(
        args.name,
        service_id=args.service_id,
        location_id=args.location_id,
        language=args.language,
        specialty=args.specialty,
        cursor=args.cursor,
    )
    c = ctx.catalog
    cards = []
    for m in result.matches:
        p = c.provider_by_id[m.id]
        location_ids = p.location_ids
        if args.service_id is not None:
            location_ids = sorted({t.location_id for t in ctx.policy.options(args.service_id, p.id)})
        cards.append(
            ProviderCard(
                id=p.id,
                name=p.name,
                title=p.title,
                specialty=p.specialty,
                accepting_new_patients=p.accepting_new_patients,
                languages=p.languages,
                locations={i: c.location_by_id[i].name for i in location_ids},
            )
        )
    return ProviderSearch(providers=tuple(cards), **_page(result, "providers"))


def find_locations(ctx: ToolContext, args: FindLocationsArgs) -> LocationSearch:
    _check_ids(ctx, service_id=args.service_id, provider_id=args.provider_id)
    patient = ctx.session.state.patient
    conditions: Optional[dict[str, set[str]]] = None
    violations: tuple[Violation, ...] = ()
    if args.service_id is not None or args.provider_id is not None:
        draft = BookingDraft(service_id=args.service_id, provider_id=args.provider_id)
        conditions = {}
        for triple, facts in ctx.policy.feasible(draft, patient):
            conditions.setdefault(triple.location_id, set()).update(facts)
        if not conditions:
            violations = tuple(ctx.policy.explain(draft, patient))
    result = ctx.index.locations(args.query, only=conditions, cursor=args.cursor)

    def reason(e: Excluded) -> Excluded:
        # Say why a named location doesn't work, rather than only that it doesn't.
        draft = BookingDraft(service_id=args.service_id, provider_id=args.provider_id, location_id=e.id)
        found = ctx.policy.conflicts(draft, patient)
        return e.model_copy(update={"reason": found[0].message}) if found else e

    c = ctx.catalog
    cards = tuple(
        _location_card(c.location_by_id[m.id], conditions.get(m.id, ()) if conditions else ())
        for m in result.matches
    )
    page = _page(result, "locations")
    page["excluded"] = tuple(reason(e) for e in result.excluded)
    if violations:
        page["hint"] = "Nothing can be booked for that: explain the problem and its fix."
    return LocationSearch(locations=cards, violations=violations, **page)


# ---- handlers: the booking --------------------------------------------------------


def update_booking(ctx: ToolContext, args: UpdateBookingArgs) -> UpdateView:
    data = args.model_dump(exclude_unset=True)
    if data.get("patient", {}) is None:
        del data["patient"]  # a null patient changes nothing; null single facts clear them
    result = ctx.session.update(BookingUpdate.model_validate(data))
    view = _booking_view(ctx, result.narrowing)
    return UpdateView(changed=result.changed, cleared=result.cleared, **view)


def check_booking(ctx: ToolContext, args: CheckBookingArgs) -> BookingView:
    return BookingView(**_booking_view(ctx, ctx.session.check()))


def get_availability(ctx: ToolContext, args: AvailabilityArgs) -> Availability:
    session, calendar = ctx.session, ctx.calendar
    offer = session.offer_times(calendar, args.day, args.part_of_day, limit=AVAILABILITY_LIMIT)
    draft = session.state.draft
    open_days = {
        s.start.astimezone(CLINIC_TZ).date()
        for s in calendar.slots(draft.service_id, draft.provider_id, draft.location_id)
    }
    shown_days = {s.start.astimezone(CLINIC_TZ).date() for s in offer.slots} | {args.day}
    other = sorted(open_days - shown_days)[:OTHER_DAYS_LIMIT]
    hint = None
    if not offer.slots:
        hint = "No openings for that; offer one of other_days." if other else "No openings in the next two weeks."
    elif offer.truncated:
        hint = "More times exist: refine by day or part_of_day."
    return Availability(
        revision=offer.revision,
        today=_day_label(calendar.now().date()),
        slots=tuple(_slot_view(s) for s in offer.slots),
        total=offer.total,
        truncated=offer.truncated,
        other_days=tuple(_day_label(d) for d in other),
        hint=hint,
    )


def book_appointment(ctx: ToolContext, args: BookAppointmentArgs) -> ReceiptView:
    session = ctx.session
    if not args.confirmed:
        raise ToolError(
            "NOT_CONFIRMED", "Read the appointment back and get the caller's explicit yes before booking."
        )
    if session.state.reservation is None:  # a retry of a completed booking skips this
        session.confirm(args.draft_revision)
    receipt = session.book(ctx.calendar, args.draft_revision)
    c, slot = ctx.catalog, receipt.slot
    return ReceiptView(
        confirmation=receipt.confirmation,
        service=c.type_by_id[slot.service_id].name,
        provider=_provider_label(c.provider_by_id[slot.provider_id]),
        location=c.location_by_id[slot.location_id].name,
        start=_time_label(slot.start),
        duration_min=c.type_by_id[slot.service_id].duration_min,
    )


# ---- guards -----------------------------------------------------------------------


def _service_chosen(session: SchedulingSession) -> Optional[str]:
    if session.state.draft.service_id is None:
        return "No service is recorded: record the caller's choice with update_booking first."
    return None


def _booking_ready(session: SchedulingSession) -> Optional[str]:
    narrowing = session.check()
    if narrowing.status == "ready":
        return None
    if narrowing.status == "blocked":
        return "The current choices can't be booked: explain the violations and their fixes."
    return f"The booking isn't ready yet. {narrowing.next_question.prompt}"


def _slot_selected(session: SchedulingSession) -> Optional[str]:
    if (problem := _booking_ready(session)) is not None:
        return problem
    if session.selected_slot() is None:
        return "No offered time is selected: call get_availability, then record the caller's pick with update_booking."
    return None


def _booking_completed(session: SchedulingSession) -> Optional[str]:
    if session.state.reservation is None:
        return "Nothing is booked yet: call book_appointment once the caller confirms."
    return None


# ---- definitions ------------------------------------------------------------------

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "catalog_overview",
            "The clinic's locations (page 0) and bookable specialties (page 1). For when the caller "
            "asks what is offered or where.",
            OverviewArgs,
            Overview,
            catalog_overview,
        ),
        Tool(
            "search_services",
            "Find appointment types matching what the caller asked for. If several match, ask which "
            "one they mean; never pick one for them.",
            SearchServicesArgs,
            ServiceSearch,
            search_services,
        ),
        Tool(
            "find_providers",
            "Find providers by name and/or filters. Names repeat: use specialty, title and locations "
            "to tell them apart. Pass service_id to see only those who offer it, and where.",
            FindProvidersArgs,
            ProviderSearch,
            find_providers,
        ),
        Tool(
            "find_locations",
            "Find locations by name, or where a service and/or provider can be booked for this caller. "
            "`conditions` are facts still to ask about.",
            FindLocationsArgs,
            LocationSearch,
            find_locations,
        ),
        Tool(
            "update_booking",
            "Record facts and choices the caller stated, including corrections. Omitted fields stay as "
            "they are; null clears one. Returns the draft and the next question to ask.",
            UpdateBookingArgs,
            UpdateView,
            update_booking,
        ),
        Tool(
            "check_booking",
            "The current draft: whether it's ready, the next question, and any violations with fixes.",
            CheckBookingArgs,
            BookingView,
            check_booking,
        ),
        Tool(
            "get_availability",
            "Up to 3 open times for the current draft, which must be ready. Optionally for one day or "
            "part of day.",
            AvailabilityArgs,
            Availability,
            get_availability,
        ),
        Tool(
            "book_appointment",
            "Book the selected time. Only after reading the appointment back and the caller saying yes; "
            "pass the draft revision they confirmed.",
            BookAppointmentArgs,
            ReceiptView,
            book_appointment,
        ),
    )
}

GUARDS: dict[str, Guard] = {
    g.name: g
    for g in (
        Guard("service_chosen", "A service is recorded in the draft.", _service_chosen),
        Guard("booking_ready", "Service, provider and location are chosen and bookable for this caller.", _booking_ready),
        Guard("slot_selected", "The draft is ready and the caller picked one of the offered times.", _slot_selected),
        Guard(
            "booking_completed",
            "The appointment is booked and has a confirmation number.",
            _booking_completed,
            completes=("book_appointment",),
        ),
    )
}


# ---- projections ------------------------------------------------------------------


def _offered(policy: Policy) -> set[str]:
    return {t.service_id for t in policy.triples}


def _check_ids(ctx: ToolContext, **ids: Optional[str]) -> None:
    maps = {
        "service_id": ctx.catalog.type_by_id,
        "provider_id": ctx.catalog.provider_by_id,
        "location_id": ctx.catalog.location_by_id,
    }
    errors = tuple(
        ArgError(path=[field], message=f"Unknown id {value!r}.")
        for field, value in ids.items()
        if value is not None and value not in maps[field]
    )
    if errors:
        raise ToolError("UNKNOWN_ID", "Use ids returned by the catalog tools.", errors=errors)


def _page(result: SearchResult, noun: str) -> dict:
    # Without a query the result is a listing, and every entry "matches equally".
    ambiguous = result.ambiguous and result.query is not None
    hint = None
    if not result.matches and result.excluded:
        hint = f"No {noun} fit the filters: tell the caller why (see excluded)."
    elif not result.matches:
        hint = f"No {noun} matched: ask the caller to rephrase." if result.query else f"No {noun} fit the filters."
    elif ambiguous:
        hint = f"Several {noun} match equally well: ask which one the caller means, by {result.clarify}."
    if result.truncated:
        more = f"Showing {len(result.matches)} of {result.total_matches}; cursor {result.next_cursor} for more."
        hint = f"{hint} {more}" if hint else more
    return {
        "total_matches": result.total_matches,
        "ambiguous": ambiguous,
        "clarify": result.clarify if ambiguous else None,
        "truncated": result.truncated,
        "next_cursor": result.next_cursor,
        "excluded": result.excluded,
        "hint": hint,
    }


def _location_card(loc: Location, conditions=()) -> LocationCard:
    return LocationCard(
        id=loc.id,
        name=loc.name,
        capabilities=loc.capabilities,
        conditions=tuple(f for f in FACTS if f in conditions),
    )


def _provider_label(p: Provider) -> str:
    """Names repeat across providers, so the label adds role and specialty."""
    return f"{p.name} ({p.title}, {p.specialty})"


def _booking_view(ctx: ToolContext, narrowing: Narrowing) -> dict:
    c, state = ctx.catalog, ctx.session.state
    draft = state.draft
    slot = ctx.session.selected_slot()
    view = DraftView(
        revision=state.revision,
        patient=Facts(**state.patient.model_dump()),
        service=Ref(id=draft.service_id, name=c.type_by_id[draft.service_id].name) if draft.service_id else None,
        provider=(
            Ref(id=draft.provider_id, name=_provider_label(c.provider_by_id[draft.provider_id]))
            if draft.provider_id
            else None
        ),
        location=Ref(id=draft.location_id, name=c.location_by_id[draft.location_id].name) if draft.location_id else None,
        slot=_slot_view(slot) if slot else None,
        confirmed=state.confirmed,
        confirmation=state.reservation.confirmation if state.reservation else None,
    )
    return {"draft": view, "check": _status_view(ctx, narrowing)}


def _status_view(ctx: ToolContext, n: Narrowing) -> StatusView:
    c = ctx.catalog

    def label(o) -> str:
        parts = [c.type_by_id[o.service_id].name]
        if o.provider_id:
            parts.append(f"with {_provider_label(c.provider_by_id[o.provider_id])}")
        if o.location_id:
            parts.append(f"at {c.location_by_id[o.location_id].name}")
        return " ".join(parts)

    hint = None
    if n.status == "ready":
        hint = "Bookable: get_availability offers times." if ctx.session.selected_slot() is None else None
    elif n.status == "blocked":
        hint = "Explain the problem and its fix; offer an alternative or a handoff to staff."
    elif n.truncated:
        hint = f"{n.total_options} options; find_providers or find_locations with service_id lists them all."
    options = tuple(
        OptionView(
            service_id=o.service_id,
            provider_id=o.provider_id,
            location_id=o.location_id,
            label=label(o),
            conditions=o.conditions,
        )
        for o in n.options
    )
    return StatusView(
        status=n.status,
        next_question=n.next_question,
        options=options,
        total_options=n.total_options,
        truncated=n.truncated,
        violations=n.violations,
        hint=hint,
    )


def _booking_error(ctx: ToolContext, e: BookingError) -> ErrorDetail:
    hints = {
        "STALE_REVISION": "Read the current draft back to the caller and confirm again.",
        "SLOT_UNAVAILABLE": "Apologize; that time was just taken. Offer other times with get_availability.",
        "SLOT_NOT_OFFERED": "Offer times with get_availability and record the caller's pick.",
        "NO_SLOT": "Offer times with get_availability and record the caller's pick.",
        "ALREADY_BOOKED": "This call already booked an appointment; changes need clinic staff.",
    }
    narrowing = ctx.session.check() if e.code == "NOT_READY" else None
    return ErrorDetail(
        code=e.code,
        message=str(e),
        violations=e.violations,
        next_question=narrowing.next_question if narrowing else None,
        hint=hints.get(e.code),
    )


def _slot_view(slot: Slot) -> SlotView:
    start = slot.start.astimezone(CLINIC_TZ)
    return SlotView(id=slot.id, start=start.isoformat(timespec="minutes"), label=_time_label(start))


def _day_label(d: date) -> str:
    return f"{d:%A} {d.isoformat()}"


def _time_label(dt: datetime) -> str:
    dt = dt.astimezone(CLINIC_TZ)
    hour = dt.hour % 12 or 12
    return f"{dt:%A}, {dt:%B} {dt.day} at {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _llm_schema(model: type[BaseModel]) -> dict:
    """A model's JSON schema with $refs inlined, no titles or defaults, and
    `anyOf [X, null]` folded into `type: [X, "null"]`."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {**target, **walk({k: v for k, v in node.items() if k != "$ref"})}
        out = {}
        for key, value in node.items():
            if key in ("title", "default"):
                continue
            out[key] = {k: walk(v) for k, v in value.items()} if key == "properties" else walk(value)
        variants = out.get("anyOf")
        if isinstance(variants, list) and len(variants) == 2 and {"type": "null"} in variants:
            (other,) = [v for v in variants if v != {"type": "null"}]
            if isinstance(other.get("type"), str):
                rest = {k: v for k, v in out.items() if k != "anyOf"}
                return {**other, **rest, "type": [other["type"], "null"]}
        return out

    result = walk(schema)
    result.setdefault("properties", {})
    result.setdefault("required", [])
    return result
