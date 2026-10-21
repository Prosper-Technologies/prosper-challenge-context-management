#
# Catalog models — the clinic catalog (backend/data/catalog.json) as typed, immutable
# data, plus the per-call facts a scheduler collects about the caller.
#
# The catalog is loaded once and shared (`default_catalog()`), so its models are
# frozen and use tuples: nothing a call does can change it. Loading checks unique
# IDs and cross-references, so the rest of the domain can index by ID without
# re-checking.
#
# `PatientFacts` fields are `bool | None`, where None means "not known yet". An
# unknown status never means an existing patient or a referral on file. These are
# simulated caller-provided facts, not an integration with patient records.
#

from collections import Counter
from functools import cache, cached_property
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_PATH = Path(__file__).parent.parent / "data" / "catalog.json"

Capability = Literal["imaging", "lab", "physical_therapy", "dental", "surgery"]
Title = Literal["MD", "DO", "NP", "PA"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Location(_Frozen):
    id: str
    name: str
    address: str
    city: str
    phone: str
    hours: str
    capabilities: tuple[Capability, ...]


class Provider(_Frozen):
    id: str
    name: str
    title: Title
    # The provider's own label. It does not decide what they can be booked for:
    # `appointment_type_ids` does.
    specialty: str
    location_ids: tuple[str, ...]
    accepting_new_patients: bool
    languages: tuple[str, ...]
    appointment_type_ids: tuple[str, ...]


class AppointmentType(_Frozen):
    id: str
    name: str
    specialty: str
    duration_min: int = Field(gt=0)
    requires_referral: bool
    new_patients_allowed: bool
    required_capability: Optional[Capability] = None


class Catalog(_Frozen):
    policies: tuple[str, ...]
    locations: tuple[Location, ...]
    providers: tuple[Provider, ...]
    appointment_types: tuple[AppointmentType, ...]

    @model_validator(mode="after")
    def _check_references(self) -> "Catalog":
        problems = []
        for field in ("locations", "providers", "appointment_types"):
            counts = Counter(item.id for item in getattr(self, field))
            problems += [f"duplicate {field} id {id!r}" for id, n in counts.items() if n > 1]
        locations = {loc.id for loc in self.locations}
        types = {t.id for t in self.appointment_types}
        for p in self.providers:
            problems += [f"{p.id}: unknown location {i!r}" for i in p.location_ids if i not in locations]
            problems += [f"{p.id}: unknown appointment type {i!r}" for i in p.appointment_type_ids if i not in types]
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @cached_property
    def location_by_id(self) -> dict[str, Location]:
        return {loc.id: loc for loc in self.locations}

    @cached_property
    def provider_by_id(self) -> dict[str, Provider]:
        return {p.id: p for p in self.providers}

    @cached_property
    def type_by_id(self) -> dict[str, AppointmentType]:
        return {t.id: t for t in self.appointment_types}


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    # Parsed as JSON, so strict mode still accepts arrays for the tuple fields.
    return Catalog.model_validate_json(Path(path).read_text())


@cache
def default_catalog() -> Catalog:
    """The checked-in catalog, loaded once per process."""
    return load_catalog()


# ---- per-call facts ---------------------------------------------------------------


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PatientFacts(_State):
    """What the caller has told us about themselves. None means not known yet."""

    is_new_patient: Optional[bool] = None
    has_referral: Optional[bool] = None


DraftField = Literal["service_id", "provider_id", "location_id", "slot_id"]
CHOICE_FIELDS: tuple[DraftField, ...] = ("service_id", "provider_id", "location_id")


class BookingDraft(_State):
    """The caller's current choices. Nothing here is booked."""

    service_id: Optional[str] = None
    provider_id: Optional[str] = None
    location_id: Optional[str] = None
    slot_id: Optional[str] = None
