#
# Scheduling scenarios — fixtures for the tricky catalog cases, shared by the pytest
# replay (tests/test_scenarios.py) and, later, the live evaluation.
#
# Each scenario has:
#   caller    the simulated caller's facts and what they want, in words
#   outcome   what counts as success, checked against session state (a reservation
#             that matches, or a block for an expected reason), never against text
#   steps     a deterministic script of session operations with expected results,
#             so the domain's answers to the scenario are pinned without an LLM
#
# Steps run against session "a" unless they name another. A scenario with more than
# one session exercises the calendar; its outcome is about session "a".
#

import json
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .policy import ViolationCode
from .session import SchedulingSession

SCENARIOS_PATH = Path(__file__).parent.parent / "data" / "scenarios.json"


class _Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Caller(_Fixture):
    is_new_patient: bool
    has_referral: bool
    opening: str  # what the caller says first
    # What the caller says if asked, including preferences and corrections.
    details: list[str] = Field(default_factory=list)


class BookingPattern(_Fixture):
    """A reservation that counts as success; an omitted field matches anything."""

    service_id: str
    provider_id: Optional[str] = None
    location_id: Optional[str] = None


class Outcome(_Fixture):
    booked: list[BookingPattern] = Field(default_factory=list)
    # Ending without a booking is correct if the draft is blocked for one of these.
    blocked: list[ViolationCode] = Field(default_factory=list)


class Step(_Fixture):
    op: Literal["search_services", "find_providers", "find_locations", "update", "check", "offer", "book"]
    session: str = "a"
    args: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict)


class Scenario(_Fixture):
    id: str
    description: str
    now: datetime
    caller: Caller
    outcome: Outcome
    steps: list[Step]


def check_outcome(scenario: Scenario, session: SchedulingSession) -> tuple[bool, str]:
    """Whether the session ended as the scenario intends, and why not."""
    receipt = session.state.reservation
    if receipt is not None:
        slot = receipt.slot
        for p in scenario.outcome.booked:
            if (
                p.service_id == slot.service_id
                and p.provider_id in (None, slot.provider_id)
                and p.location_id in (None, slot.location_id)
            ):
                return True, "booked as intended"
        return False, f"booked {slot.service_id} with {slot.provider_id} at {slot.location_id}, which isn't intended"
    codes = {v.code for v in session.explain()}
    if codes & set(scenario.outcome.blocked):
        return True, f"blocked: {', '.join(sorted(codes))}"
    if scenario.outcome.booked:
        return False, "no booking was made"
    return False, f"expected a block for {scenario.outcome.blocked}, got {sorted(codes) or 'no violations'}"


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[Scenario]:
    return [Scenario.model_validate(s) for s in json.loads(Path(path).read_text())]


@cache
def default_scenarios() -> tuple[Scenario, ...]:
    return tuple(load_scenarios())
