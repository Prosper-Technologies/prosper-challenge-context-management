"""CatalogIndex: normalisation, tiered ranking, ambiguity before truncation, filters."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from catalog.index import CatalogIndex, default_index, load_aliases, normalize
from catalog.models import default_catalog

CHENS = ["prov_000", "prov_001", "prov_004", "prov_012", "prov_039", "prov_046", "prov_047"]


@pytest.fixture
def index() -> CatalogIndex:
    return default_index()


def ids(result):
    return [m.id for m in result.matches]


def test_normalize():
    assert normalize("Dr. Chen's") == ("chen",)
    assert normalize("Mission Bay Health Center") == ("mission", "bay", "health")
    assert normalize("Tomás  HERNÁNDEZ, MD") == ("tomas", "hernandez")
    assert normalize("an appointment for my X-Ray") == ("x", "ray")
    assert normalize("Dr.") == ()


# ---- ranking ------------------------------------------------------------------------


def test_exact_name_beats_partial_matches(index):
    result = index.providers("David Chen")
    assert ids(result) == ["prov_000"] and not result.ambiguous
    assert result.matches[0].tier == "exact"


def test_same_name_providers_are_ambiguous(index):
    for name, expected in [
        ("Maria Garcia", ["prov_002", "prov_003"]),
        ("Linda Ramirez", ["prov_005", "prov_014"]),
        ("Michael Sato", ["prov_026", "prov_032"]),
    ]:
        result = index.providers(name)
        assert ids(result) == expected and result.ambiguous and result.clarify == "specialty", name


def test_spoken_name_variants_and_misspellings(index):
    assert ids(index.providers("Liz Nguyen")) == ["prov_030"]
    assert ids(index.providers("Dave Chen")) == ["prov_000"]
    result = index.providers("Dr. Chan")
    assert result.matches[0].tier == "fuzzy" and result.total_matches == 7


def test_aliases(index):
    assert ids(index.services("knee MRI")) == ["appt_065"]
    assert ids(index.services("teeth cleaning")) == ["appt_074"]
    assert ids(index.services("colonoscopy")) == ["appt_054"]
    assert ids(index.locations("northgate")) == ["loc_003"]
    # An alias also keeps entries that contain every query word.
    assert ids(index.services("blood test")) == ["appt_072", "appt_073"]
    # Two services share the alias: ambiguous, told apart by specialty.
    checkup = index.services("checkup")
    assert ids(checkup)[:2] == ["appt_002", "appt_003"] and checkup.ambiguous and checkup.clarify == "specialty"


def test_near_duplicates_and_confusable_locations(index):
    assert ids(index.services("MRI")) == ["appt_063", "appt_064", "appt_065"]
    assert ids(index.services("physical")) == ["appt_002", "appt_018", "appt_019", "appt_070", "appt_071"]
    assert ids(index.services("annual physical")) == ["appt_002"]
    mission = index.locations("Mission")
    assert ids(mission) == ["loc_000", "loc_001"] and mission.ambiguous
    assert ids(index.locations("mission bay")) == ["loc_000"]


@pytest.mark.parametrize("query", ["pizza", "pizza delivery", "Dr.", "", "zzz", "moon base"])
def test_unrelated_queries_match_nothing(index, query):
    result = index.services(query)
    assert result.matches == () and result.total_matches == 0 and not result.ambiguous
    assert index.locations(query).matches == ()
    if query:  # an empty provider name is a listing, not a query
        providers = index.providers(query)
        assert providers.matches == () and providers.total_matches == 0 and not providers.ambiguous


def test_ranking_is_stable():
    a = CatalogIndex(default_catalog(), {}).providers("Chen")
    b = CatalogIndex(default_catalog(), {}).providers("Chen")
    assert a == b


RANKED_QUERIES = [("providers", "Chen"), ("providers", "Garcia"), ("services", "checkup"), ("services", "physical"),
                  ("locations", "Mission"), ("locations", "North")]  # fmt: skip


def ranked(index: CatalogIndex) -> list[list[str]]:
    return [ids(getattr(index, kind)(query, limit=50)) for kind, query in RANKED_QUERIES]


def test_ranking_doesnt_depend_on_catalog_order(index):
    catalog = default_catalog()
    shuffled = catalog.model_copy(
        update={field: tuple(reversed(getattr(catalog, field))) for field in ("locations", "providers", "appointment_types")}
    )
    assert ranked(CatalogIndex(shuffled, load_aliases())) == ranked(index)


def test_ranking_doesnt_depend_on_the_hash_seed(index):
    script = (
        "import json; from catalog.index import default_index; from tests.test_index import ranked; "
        "print(json.dumps(ranked(default_index())))"
    )
    for seed in ("1", "2"):
        out = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parent.parent, capture_output=True,
                             text=True, check=True, env={**os.environ, "PYTHONHASHSEED": seed})  # fmt: skip
        assert json.loads(out.stdout.strip().splitlines()[-1]) == ranked(index)


# ---- caps and cursors ---------------------------------------------------------------


def test_ambiguity_is_computed_before_the_cap(index):
    first = index.providers("Dr. Chen")
    assert ids(first) == CHENS[:5]
    assert first.total_matches == 7 and first.ambiguous and first.truncated and first.next_cursor == "5"
    rest = index.providers("Dr. Chen", cursor=first.next_cursor)
    assert ids(rest) == CHENS[5:] and not rest.truncated and rest.next_cursor is None
    assert ids(first) + ids(rest) == CHENS


def test_invalid_cursor(index):
    with pytest.raises(ValueError):
        index.providers("Chen", cursor="next")


# ---- filters ------------------------------------------------------------------------


def test_filters_apply_before_the_cap(index):
    at_mission_bay = index.providers("Dr. Chen", location_id="loc_000")
    assert ids(at_mission_bay) == ["prov_000", "prov_004", "prov_039", "prov_047"]
    assert not at_mission_bay.truncated
    assert ids(index.providers(service_id="appt_065")) == ["prov_014", "prov_015"]
    assert ids(index.providers("Nguyen", language="vietnamese")) == ["prov_015", "prov_030"]


def test_provider_specialty_is_the_providers_own_label(index):
    # prov_002 is a pediatrician who offers Annual Physical; a Family Medicine
    # filter is about the label, so it excludes her, and she is still bookable.
    result = index.providers("Maria Garcia", specialty="Family Medicine")
    assert ids(result) == ["prov_003"]
    assert [(e.id, e.reason) for e in result.excluded] == [("prov_002", "specialty is Pediatrics")]
    assert "prov_002" in ids(index.providers("Maria Garcia", service_id="appt_002"))


def test_a_named_provider_who_cant_be_booked_is_explained_not_replaced(index):
    result = index.providers("David Chen", service_id="appt_065")
    assert result.matches == ()
    assert [(e.id, e.reason) for e in result.excluded] == [("prov_000", "doesn't offer MRI - Knee")]


def test_only_restricts_to_ids(index):
    result = index.locations("Mission", only={"loc_001"})
    assert ids(result) == ["loc_001"] and not result.ambiguous
    assert [e.id for e in result.excluded] == ["loc_000"]


def test_service_specialty_filter(index):
    assert ids(index.services("consultation", specialty="cardiology")) == ["appt_020"]


def test_aliases_for_unknown_ids_are_rejected():
    with pytest.raises(ValueError):
        CatalogIndex(default_catalog(), {"appointment_types": {"appt_999": ["thing"]}})
