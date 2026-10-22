"""Replays the scheduling scenarios (backend/data/scenarios.json) step by step and
checks each scenario's intended outcome against the resulting session state."""

from datetime import date, timedelta

import pytest

from catalog.availability import Calendar
from catalog.index import default_index
from catalog.models import default_catalog
from catalog.policy import default_policy
from catalog.scenarios import check_outcome, default_scenarios, prebook
from catalog.session import BookingError, InvalidUpdate, OfferResult, SchedulingSession

SCENARIOS = default_scenarios()


class Replay:
    def __init__(self, scenario):
        self.calendar = Calendar(default_catalog(), clock=lambda: scenario.now)
        self.sessions: dict[str, SchedulingSession] = {}
        self.offers: dict[str, OfferResult] = {}
        self.receipts: dict[str, object] = {}

    def session(self, name: str) -> SchedulingSession:
        if name not in self.sessions:
            self.sessions[name] = SchedulingSession(default_policy(), f"scenario-{name}")
        return self.sessions[name]

    def slot_ref(self, name: str, ref):
        """A slot id, or a reference to one in the session's latest offer."""
        if not isinstance(ref, dict):
            return ref
        offered = self.offers[name].slots
        if "offered" in ref:
            return offered[ref["offered"]].id
        other = self.sessions[ref["overlapping"]].selected_slot()
        (slot,) = [s for s in offered if s.start < other.end and other.start < s.end][:1]
        return slot.id

    def run(self, step):
        """Run one step; returns (result, error)."""
        index = default_index()
        session = self.session(step.session)
        args = dict(step.args)
        try:
            if step.op == "search_services":
                return index.services(**args), None
            if step.op == "find_providers":
                return index.providers(**args), None
            if step.op == "find_locations":
                return index.locations(**args), None
            if step.op == "check":
                return session.check(), None
            if step.op == "update":
                if "slot_id" in args:
                    args["slot_id"] = self.slot_ref(step.session, args["slot_id"])
                return session.update(args), None
            if step.op == "offer":
                day = date.fromisoformat(args["day"]) if "day" in args else None
                offer = session.offer_times(self.calendar, day, args.get("part_of_day"))
                self.offers[step.session] = offer
                return offer, None
            if step.op == "book":
                revision = args.get("revision", session.state.revision)
                if args.get("confirm", True):
                    session.confirm(revision)
                return session.book(self.calendar, revision), None
        except InvalidUpdate as e:
            return None, "invalid"
        except BookingError as e:
            return None, e.code
        raise AssertionError(f"unknown op {step.op}")


def check_step(replay: Replay, step, result, error):
    expect = dict(step.expect)
    session = replay.session(step.session)
    state = session.state
    where = f"{step.op} {step.args}"

    assert error == expect.pop("error", None), f"{where}: error {error}"
    # Session state, whether or not the step succeeded.
    if "revision" in expect:
        assert state.revision == expect.pop("revision"), where
    if "draft" in expect:
        draft = state.draft.model_dump()
        for field, value in expect.pop("draft").items():
            assert draft[field] == value, f"{where}: draft.{field}"
    if error:
        assert not expect, f"{where}: unchecked expectations {expect}"
        return

    if step.op in ("search_services", "find_providers", "find_locations"):
        checks = {
            "ids": [m.id for m in result.matches],
            "total": result.total_matches,
            "ambiguous": result.ambiguous,
            "clarify": result.clarify,
            "truncated": result.truncated,
            "next_cursor": result.next_cursor,
            "excluded": [e.id for e in result.excluded],
        }
    elif step.op in ("update", "check"):
        narrowing = result.narrowing if step.op == "update" else result
        checks = {
            "status": narrowing.status,
            "next": narrowing.next_question.field if narrowing.next_question else None,
            "total_options": narrowing.total_options,
            "truncated": narrowing.truncated,
            "unresolved": list(narrowing.unresolved),
            "violations": sorted(v.code for v in narrowing.violations),
        }
        if step.op == "update":
            checks["changed"] = result.changed
            checks["cleared"] = [c.field for c in result.cleared]
        if "options" in expect:
            options = [o.model_dump() for o in narrowing.options]
            wanted = expect.pop("options")
            assert len(options) == len(wanted), f"{where}: options {options}"
            for got, want in zip(options, wanted):
                assert {k: got[k] for k in want} == want, f"{where}: options {options}"
        if "fix_mentions" in expect:
            fixes = " ".join(v.fix for v in narrowing.violations)
            for text in expect.pop("fix_mentions"):
                assert text in fixes, f"{where}: {fixes!r}"
    elif step.op == "offer":
        checks = {"count": len(result.slots)}
        if "none_overlapping" in expect:
            other = replay.sessions[expect.pop("none_overlapping")].state.reservation.slot
            assert not [s for s in result.slots if s.start < other.end and other.start < s.end], where
    elif step.op == "book":
        checks = {"booked": result is not None}
        if expect.pop("same_receipt", False):
            assert result == replay.receipts[step.session], where
        replay.receipts[step.session] = result
    for key, want in expect.items():
        assert key in checks, f"{where}: unknown expectation {key!r}"
        assert checks[key] == want, f"{where}: {key} = {checks[key]!r}, expected {want!r}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario(scenario):
    replay = Replay(scenario)
    for step in scenario.steps:
        result, error = replay.run(step)
        check_step(replay, step, result, error)
    ok, why = check_outcome(scenario, replay.session("a"))
    assert ok, why


def test_scenario_ids_are_unique():
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))


def test_prebooked_reservations_block_overlapping_times():
    (scenario,) = [s for s in SCENARIOS if s.prebooked]
    (held,) = scenario.prebooked
    calendar = Calendar(default_catalog(), clock=lambda: scenario.now)
    before = Replay(scenario).session("a")
    before.update(scenario.steps[0].args)
    free = before.offer_times(Calendar(default_catalog(), clock=lambda: scenario.now), held.start.date(), limit=50)

    prebook(scenario, calendar)

    end = held.start + timedelta(minutes=default_catalog().type_by_id[held.service_id].duration_min)
    after = Replay(scenario).session("a")
    after.update(scenario.steps[0].args)
    offered = after.offer_times(calendar, held.start.date(), limit=50).slots
    overlapping = [s for s in free.slots if s.start < end and held.start < s.end]
    assert overlapping, "the prebooked time should overlap one of the caller's times"
    assert not [s for s in offered if s.start < end and held.start < s.end]
    assert len(offered) == len(free.slots) - len(overlapping)
