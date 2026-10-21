#
# SchedulingSession — one caller's scheduling state, and the only way to change it.
#
#   update(args)        explicit facts, choices and corrections (omitted fields stay,
#                       null clears); stale downstream choices are cleared
#   check()             policy.narrow on the current draft (read-only)
#   offer_times(...)    up to 3 free slots for a ready draft, recorded as offered
#   confirm(revision)   the caller confirmed this exact draft and slot
#   book(calendar, revision)  revalidate and reserve, atomically and idempotently
#
# Searches never write here, and nothing here picks a choice for the caller.
#
# Every effective change increments `revision` and clears confirmation. Changing a
# patient fact or the service/provider/location invalidates offered times and the
# selected slot. Selecting an offered slot carries that offer forward to the new
# revision. A completed reservation is kept apart from the editable draft.
#
# Correction rule: explicit choices in an update always stay, even when infeasible,
# so `explain()` can report them. Earlier choices *downstream* of what changed
# (facts -> service -> provider -> location) are cleared if they now conflict; an
# upstream one is kept and the conflict reported. A fact change never clears the
# service: the caller still needs it, and the violation says why it can't be booked.
#
# `SessionState` is plain JSON-compatible data, so a voice call can keep it in
# `flow_manager.state`. It lasts as long as the call.
#

import json
from datetime import date
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .availability import Calendar, PartOfDay, Receipt, Slot, SlotUnavailable
from .models import CHOICE_FIELDS, BookingDraft, PatientFacts
from .policy import ChoiceField, Narrowing, Policy, UnknownId, Violation

MAX_OFFERED = 30  # offered slots remembered per revision
_RANK = {"patient": 0, "service_id": 1, "provider_id": 2, "location_id": 3}


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_serialization_defaults_required=True)


class Offer(_State):
    revision: int
    slots: list[Slot]


class SessionState(_State):
    id: str
    patient: PatientFacts = Field(default_factory=PatientFacts)
    draft: BookingDraft = Field(default_factory=BookingDraft)
    revision: int = 0
    confirmed: bool = False
    offer: Optional[Offer] = None
    reservation: Optional[Receipt] = None

    def dump(self) -> dict:
        """JSON-compatible data, e.g. for `flow_manager.state`."""
        return self.model_dump(mode="json")

    @classmethod
    def load(cls, data: dict) -> "SessionState":
        # Through JSON, so strict mode accepts the ISO datetimes `dump()` wrote.
        return cls.model_validate_json(json.dumps(data))


class PatientUpdate(_State):
    is_new_patient: Optional[bool] = None
    has_referral: Optional[bool] = None


class BookingUpdate(_State):
    """Omitted fields stay as they are; an explicit null clears the field."""

    patient: Optional[PatientUpdate] = None
    service_id: Optional[str] = None
    provider_id: Optional[str] = None
    location_id: Optional[str] = None
    slot_id: Optional[str] = None


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)


class Cleared(_Out):
    """An earlier choice an update removed because it no longer fits."""

    field: Literal[ChoiceField, "slot_id"]
    value: str
    reason: str  # a violation code, or OFFER_INVALIDATED for a slot
    message: str


class UpdateResult(_Out):
    changed: bool
    revision: int
    cleared: tuple[Cleared, ...]
    narrowing: Narrowing


class OfferResult(_Out):
    revision: int
    slots: tuple[Slot, ...]
    total: int
    truncated: bool


class InvalidUpdate(ValueError):
    """The update was rejected as a whole; the session is unchanged."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(f"{'.'.join(map(str, e['path']))}: {e['message']}" for e in errors))


BookingErrorCode = Literal[
    "NOT_READY", "NO_SLOT", "STALE_REVISION", "NOT_CONFIRMED", "SLOT_NOT_OFFERED", "SLOT_UNAVAILABLE", "ALREADY_BOOKED"
]


class BookingError(Exception):
    def __init__(self, code: BookingErrorCode, message: str, violations: tuple[Violation, ...] = ()):
        self.code = code
        self.violations = violations
        super().__init__(message)


class SchedulingSession:
    def __init__(self, policy: Policy, state: Union[SessionState, str]):
        """Resume `state`, or start an empty session with that id."""
        self.policy = policy
        self.state = state if isinstance(state, SessionState) else SessionState(id=state)

    @property
    def id(self) -> str:
        return self.state.id

    # ---- reading ----------------------------------------------------------------

    def check(self) -> Narrowing:
        return self.policy.narrow(self.state.draft, self.state.patient)

    def explain(self) -> list[Violation]:
        return self.policy.explain(self.state.draft, self.state.patient)

    def selected_slot(self) -> Optional[Slot]:
        return self._offered(self.state.draft.slot_id)

    # ---- updating ---------------------------------------------------------------

    def update(self, args: Union[dict, BookingUpdate]) -> UpdateResult:
        try:
            update = args if isinstance(args, BookingUpdate) else BookingUpdate.model_validate(args)
        except ValidationError as e:
            raise InvalidUpdate([{"path": list(err["loc"]), "message": err["msg"]} for err in e.errors()]) from None
        old = self.state
        explicit = update.model_fields_set - {"patient"}

        patient = old.patient
        if "patient" in update.model_fields_set:
            facts = update.patient
            patient = (
                PatientFacts()
                if facts is None
                else patient.model_copy(update={k: getattr(facts, k) for k in facts.model_fields_set})
            )
        draft = old.draft.model_copy(update={f: getattr(update, f) for f in explicit if f != "slot_id"})
        try:
            self.policy.check_ids(draft)
        except UnknownId as e:
            raise InvalidUpdate([{"path": [e.field], "message": str(e)}]) from None

        changed = [f for f in CHOICE_FIELDS if getattr(draft, f) != getattr(old.draft, f)]
        if patient != old.patient:
            changed.insert(0, "patient")
        cleared: list[Cleared] = []
        if changed:
            draft = self._clear_stale(draft, patient, explicit, min(_RANK[f] for f in changed), cleared)

        # Offers and the selected slot belong to the old choices once anything changed.
        offer = old.offer if not changed and old.offer and old.offer.revision == old.revision else None
        if "slot_id" in explicit and update.slot_id is not None:
            if offer is None or self._offered(update.slot_id, offer) is None:
                why = "isn't among the times offered for the current choices"
                if changed:
                    why = "can't be selected in the same update that changes the choices"
                raise InvalidUpdate([{"path": ["slot_id"], "message": f"Slot {update.slot_id!r} {why}."}])
            draft = draft.model_copy(update={"slot_id": update.slot_id})
        elif "slot_id" in explicit:
            draft = draft.model_copy(update={"slot_id": None})
        elif changed and draft.slot_id is not None:
            cleared.append(
                Cleared(
                    field="slot_id",
                    value=draft.slot_id,
                    reason="OFFER_INVALIDATED",
                    message="The selected time was for the previous choices.",
                )
            )
            draft = draft.model_copy(update={"slot_id": None})

        if patient == old.patient and draft == old.draft:
            return UpdateResult(changed=False, revision=old.revision, cleared=(), narrowing=self.check())
        revision = old.revision + 1
        self.state = old.model_copy(
            update={
                "patient": patient,
                "draft": draft,
                "revision": revision,
                "confirmed": False,
                "offer": offer.model_copy(update={"revision": revision}) if offer else None,
            }
        )
        return UpdateResult(changed=True, revision=revision, cleared=tuple(cleared), narrowing=self.check())

    def _clear_stale(self, draft, patient, explicit, changed_rank, cleared) -> BookingDraft:
        def clearable(field: str, violation: Violation) -> bool:
            return (
                field not in explicit
                and _RANK[field] > changed_rank
                and getattr(draft, field) is not None
                and not (violation.facts and field == "service_id")
            )

        while True:
            # Direct conflicts, or else the choices that together leave no option.
            violations = self.policy.explain(draft, patient)
            candidates = [
                (_RANK[f], f, v)
                for v in violations
                for f in (v.fields or CHOICE_FIELDS)
                if clearable(f, v)
            ]
            if not candidates:
                return draft
            _, field, violation = max(candidates, key=lambda c: c[0])
            cleared.append(
                Cleared(field=field, value=getattr(draft, field), reason=violation.code, message=violation.message)
            )
            draft = draft.model_copy(update={field: None})

    # ---- times and booking ------------------------------------------------------

    def offer_times(
        self,
        calendar: Calendar,
        day: Optional[date] = None,
        part_of_day: Optional[PartOfDay] = None,
        limit: int = 3,
    ) -> OfferResult:
        """Up to `limit` free slots for the current draft, remembered as offered for
        this revision. Refused unless the draft is ready."""
        narrowing = self._require_ready()
        (option,) = narrowing.options
        slots = calendar.slots(option.service_id, option.provider_id, option.location_id, day, part_of_day)
        shown = slots[:limit]
        state = self.state
        previous = state.offer.slots if state.offer and state.offer.revision == state.revision else []
        known = {s.id for s in previous}
        merged = (previous + [s for s in shown if s.id not in known])[-MAX_OFFERED:]
        self.state = state.model_copy(update={"offer": Offer(revision=state.revision, slots=merged)})
        return OfferResult(revision=state.revision, slots=tuple(shown), total=len(slots), truncated=len(slots) > limit)

    def confirm(self, revision: int) -> None:
        """Record that the caller confirmed the current draft and selected slot."""
        self._require_current(revision)
        self._require_ready()
        if self.selected_slot() is None:
            raise BookingError("NO_SLOT", "Select one of the offered times first.")
        self.state = self.state.model_copy(update={"confirmed": True})

    def book(self, calendar: Calendar, revision: int) -> Receipt:
        state = self.state
        if state.reservation is not None:
            if state.reservation.draft_revision == revision:
                return state.reservation  # a retry of the booking that succeeded
            raise BookingError("ALREADY_BOOKED", "This call already has a booking; changing it isn't supported.")
        self._require_current(revision)
        if not state.confirmed:
            raise BookingError("NOT_CONFIRMED", "The caller hasn't confirmed this booking.")
        self._require_ready()
        slot = self.selected_slot()
        if slot is None:
            message = "Select one of the offered times first."
            if state.draft.slot_id:
                message = "The selected time wasn't offered for the current choices."
            raise BookingError("NO_SLOT" if not state.draft.slot_id else "SLOT_NOT_OFFERED", message)
        try:
            receipt = calendar.reserve(slot, f"{state.id}:{revision}", state.patient, state.id, revision)
        except SlotUnavailable as e:
            raise BookingError("SLOT_UNAVAILABLE", str(e)) from None
        self.state = state.model_copy(update={"reservation": receipt})
        return receipt

    # ---- helpers ----------------------------------------------------------------

    def _offered(self, slot_id: Optional[str], offer: Optional[Offer] = None) -> Optional[Slot]:
        offer = offer or self.state.offer
        if slot_id is None or offer is None or offer.revision != self.state.revision:
            return None
        return next((s for s in offer.slots if s.id == slot_id), None)

    def _require_current(self, revision: int) -> None:
        if revision != self.state.revision:
            raise BookingError(
                "STALE_REVISION", f"The draft changed (now revision {self.state.revision}); confirm it again."
            )

    def _require_ready(self) -> Narrowing:
        narrowing = self.check()
        if narrowing.status != "ready":
            question = narrowing.next_question
            message = question.prompt if question else "The current choices can't be booked."
            raise BookingError("NOT_READY", message, narrowing.violations)
        return narrowing
