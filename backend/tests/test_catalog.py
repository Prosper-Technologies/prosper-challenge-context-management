"""Catalog loading (models) and catalog lint."""

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from catalog.lint import lint
from catalog.models import CATALOG_PATH, default_catalog, load_catalog

RAW = json.loads(CATALOG_PATH.read_text())


def write(tmp_path, data):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return path


def test_default_catalog_loads_once():
    catalog = default_catalog()
    assert (len(catalog.locations), len(catalog.providers), len(catalog.appointment_types)) == (8, 50, 82)
    assert len(catalog.policies) == 6
    assert default_catalog() is catalog
    assert catalog.type_by_id["appt_065"].required_capability == "imaging"


def test_domain_has_no_pipecat_or_http_dependency():
    script = "import sys, catalog, catalog.scenarios, catalog.tools; print(sorted(m for m in sys.modules if m.split('.')[0] in {'pipecat', 'pipecat_flows', 'fastapi'}))"
    out = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parent.parent, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_catalog_is_immutable():
    catalog = default_catalog()
    with pytest.raises(ValidationError):
        catalog.providers[0].accepting_new_patients = True
    with pytest.raises(AttributeError):
        catalog.providers[0].location_ids.append("loc_001")


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda d: d["providers"].append(dict(d["providers"][0])), "duplicate providers id 'prov_000'"),
        (lambda d: d["providers"][0]["location_ids"].append("loc_099"), "unknown location 'loc_099'"),
        (lambda d: d["providers"][0]["appointment_type_ids"].append("appt_999"), "unknown appointment type"),
        (lambda d: d["appointment_types"][0].update(required_capability="teleport"), "required_capability"),
        (lambda d: d["providers"][0].update(accepting_new_patients="yes"), "accepting_new_patients"),
    ],
)
def test_invalid_catalogs_are_rejected(tmp_path, change, message):
    data = json.loads(json.dumps(RAW))
    change(data)
    with pytest.raises(ValidationError, match=message):
        load_catalog(write(tmp_path, data))


# ---- lint ---------------------------------------------------------------------------


def test_lint_finds_the_catalog_problems():
    issues = lint(default_catalog())
    assert Counter(i.code for i in issues) == {
        "UNBOOKABLE_TYPE": 8,
        "NEW_PATIENT_DEAD_END": 6,
        "DUPLICATE_PROVIDER_NAME": 3,
        "DR_TITLE_ON_NON_PHYSICIAN": 13,
        "CONFUSABLE_LOCATIONS": 2,
        "UNUSED_CAPABILITY": 5,
    }
    by_code = lambda code: [i for i in issues if i.code == code]  # noqa: E731
    unbookable = {i.refs[0] for i in by_code("UNBOOKABLE_TYPE")}
    assert unbookable == {"appt_045", "appt_046", "appt_047", "appt_048", "appt_049", "appt_070", "appt_071", "appt_077"}
    assert {i.refs[0] for i in by_code("NEW_PATIENT_DEAD_END")} >= {"appt_040", "appt_056"}
    assert sorted(i.refs for i in by_code("DUPLICATE_PROVIDER_NAME")) == [
        ["prov_002", "prov_003"],
        ["prov_005", "prov_014"],
        ["prov_026", "prov_032"],
    ]
    assert sorted(i.refs for i in by_code("CONFUSABLE_LOCATIONS")) == [["loc_000", "loc_001"], ["loc_002", "loc_003"]]
    # Physical therapy rooms with no PT provider.
    pt = [i for i in by_code("UNUSED_CAPABILITY") if "physical therapy" in i.message]
    assert [i.refs for i in pt] == [["loc_000"], ["loc_001"]]


def test_lint_paths_point_into_the_catalog():
    for issue in lint(default_catalog()):
        value = RAW
        for part in issue.path:
            value = value[part]
        assert issue.level in ("error", "warning")


def test_lint_levels():
    levels = {i.code: i.level for i in lint(default_catalog())}
    assert levels["UNBOOKABLE_TYPE"] == levels["NEW_PATIENT_DEAD_END"] == "error"
    assert levels["DUPLICATE_PROVIDER_NAME"] == "warning"


def test_lint_unbookable_because_of_capability(tmp_path):
    data = json.loads(json.dumps(RAW))
    for loc in data["locations"]:
        loc["capabilities"] = [c for c in loc["capabilities"] if c != "dental"]
    (issue,) = [i for i in lint(load_catalog(write(tmp_path, data))) if i.refs[0] == "appt_074"]
    assert issue.code == "UNBOOKABLE_TYPE"
    assert "don't practice at a location with dental" in issue.message
