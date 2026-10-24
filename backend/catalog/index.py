#
# CatalogIndex — matches free text (what the caller said) to catalog entries.
#
# Text is normalised (lowercase, no accents or punctuation; titles such as "Dr.",
# filler words and the words "clinic"/"center" dropped; spoken first-name variants
# mapped for providers), then ranked in tiers:
#
#   exact    the whole name          alias    a curated alias (catalog_aliases.json)
#   tokens   every query word        fuzzy    every query word, allowing misspelling
#   partial  at least half the query words
#
# Only the best tier is returned (exact and alias matches also bring in `tokens`
# matches, so "blood test" still lists Fasting Blood Test). Below `partial` there is
# no match: an unrelated query returns nothing rather than the least-bad entry. Ties
# break by ID, so results are stable.
#
# Ambiguity is computed over every match before the result is capped: `ambiguous`,
# `total_matches`, a `clarify` attribute that tells the best matches apart, and a
# `next_cursor` to page through the rest. Seven providers match "Dr. Chen"; a cap of
# five never hides the other two.
#
# Filters apply before the cap. A provider's specialty filter compares the
# provider's own label only; what they can be booked for is `service_id`. A named
# match that a filter rules out is listed in `excluded` with the reason, rather than
# silently replaced by someone else.
#

import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path
from typing import Literal, NamedTuple, Optional

from pydantic import BaseModel, ConfigDict

from .models import Catalog, default_catalog

ALIASES_PATH = Path(__file__).parent.parent / "data" / "catalog_aliases.json"
SEARCH_LIMIT = 5
EXCLUDED_LIMIT = 3

_DROP = {
    # titles
    "dr", "doctor", "md", "do", "np", "pa",
    # generic location words
    "clinic", "center", "centre",
    # filler
    "a", "an", "the", "of", "my", "for", "at", "with", "to", "in", "on", "and",
    "appointment", "appointments", "appt",
}  # fmt: skip
FUZZY_WORD = 0.75  # a misspelt word, e.g. "chan" for "chen"
FUZZY_MIN_LEN = 4  # shorter words must match exactly
FUZZY_WHOLE = 0.85  # the whole query, ignoring spaces ("xray", "mammo gram")

Tier = Literal["exact", "alias", "tokens", "fuzzy", "partial"]
_TIERS: tuple[Tier, ...] = ("exact", "alias", "tokens", "fuzzy", "partial")
Kind = Literal["services", "providers", "locations"]


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True, json_schema_serialization_defaults_required=True)


class Match(_Out):
    id: str
    name: str
    tier: Optional[Tier]  # None when listing without a query
    score: float


class Excluded(_Out):
    id: str
    name: str
    reason: str


class SearchResult(_Out):
    query: Optional[str]
    matches: tuple[Match, ...]
    total_matches: int
    # More than one entry fits equally well: ask the caller, e.g. about `clarify`.
    ambiguous: bool
    clarify: Optional[str]
    truncated: bool
    next_cursor: Optional[str]
    excluded: tuple[Excluded, ...]


class _Entry(NamedTuple):
    id: str
    name: str
    name_key: tuple[str, ...]
    alias_keys: tuple[tuple[str, ...], ...]


# A filter returns None if the entry passes, or why it doesn't.
Filter = Callable[[str], Optional[str]]


def normalize(text: str, variants: Optional[dict[str, str]] = None) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['\u2019]s\b", "", text)  # "Dr. Chen's" -> "dr. chen"
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    variants = variants or {}
    return tuple(variants.get(w, w) for w in words if w not in _DROP)


class CatalogIndex:
    def __init__(self, catalog: Catalog, aliases: Optional[dict] = None):
        self.catalog = catalog
        aliases = aliases or {}
        self._variants = dict(aliases.get("name_variants", {}))
        self._entries: dict[Kind, list[_Entry]] = {
            "services": self._build(catalog.appointment_types, aliases.get("appointment_types", {})),
            "providers": self._build(catalog.providers, aliases.get("providers", {}), self._variants),
            "locations": self._build(catalog.locations, aliases.get("locations", {})),
        }

    @staticmethod
    def _build(items, aliases: dict[str, list[str]], variants=None) -> list[_Entry]:
        unknown = set(aliases) - {item.id for item in items}
        if unknown:
            raise ValueError(f"Aliases for unknown ids: {sorted(unknown)}")
        return [
            _Entry(
                item.id,
                item.name,
                normalize(item.name, variants),
                tuple(normalize(a, variants) for a in aliases.get(item.id, ())),
            )
            for item in sorted(items, key=lambda i: i.id)
        ]

    # ---- searches ---------------------------------------------------------------

    def services(
        self,
        query: Optional[str] = None,
        specialty: Optional[str] = None,
        only: Optional[Iterable[str]] = None,
        limit: int = SEARCH_LIMIT,
        cursor: Optional[str] = None,
    ) -> SearchResult:
        """Appointment types. `specialty` filters the appointment type's own label."""
        types = self.catalog.type_by_id
        filters = [_only(only, "isn't bookable for the current choices")]
        if specialty is not None:
            filters.append(_label(specialty, lambda i: types[i].specialty))
        return self._search("services", query, filters, limit, cursor)

    def providers(
        self,
        name: Optional[str] = None,
        service_id: Optional[str] = None,
        location_id: Optional[str] = None,
        language: Optional[str] = None,
        specialty: Optional[str] = None,
        only: Optional[Iterable[str]] = None,
        limit: int = SEARCH_LIMIT,
        cursor: Optional[str] = None,
    ) -> SearchResult:
        """Providers. `service_id` checks what they can be booked for; `specialty`
        only filters their own label."""
        c = self.catalog
        by_id = c.provider_by_id
        filters = [_only(only, "isn't bookable for the current choices")]
        if service_id is not None:
            service = c.type_by_id[service_id].name
            filters.append(lambda i: None if service_id in by_id[i].appointment_type_ids else f"doesn't offer {service}")
        if location_id is not None:
            location = c.location_by_id[location_id].name
            filters.append(lambda i: None if location_id in by_id[i].location_ids else f"doesn't practice at {location}")
        if language is not None:
            wanted = language.strip().lower()
            filters.append(
                lambda i: None if wanted in {lang.lower() for lang in by_id[i].languages} else f"doesn't speak {language}"
            )
        if specialty is not None:
            filters.append(_label(specialty, lambda i: by_id[i].specialty))
        return self._search("providers", name, filters, limit, cursor)

    def locations(
        self,
        query: Optional[str] = None,
        only: Optional[Iterable[str]] = None,
        limit: int = SEARCH_LIMIT,
        cursor: Optional[str] = None,
    ) -> SearchResult:
        return self._search("locations", query, [_only(only, "can't be booked for the current choices")], limit, cursor)

    # ---- ranking ----------------------------------------------------------------

    def _search(self, kind: Kind, query, filters: list[Filter], limit: int, cursor) -> SearchResult:
        offset = _offset(cursor)
        entries = self._entries[kind]
        key = normalize(query, self._variants if kind == "providers" else None) if query is not None else None

        if key is None:
            scored = [(e, None, 1.0) for e in entries]
        else:
            scored = sorted(
                ((e, *s) for e in entries if (s := _score(key, e)) is not None),
                key=lambda m: (_TIERS.index(m[1]), -m[2], m[0].id),
            )

        def reasons(entry: _Entry) -> Optional[str]:
            return next((r for f in filters if (r := f(entry.id)) is not None), None)

        passing = [m for m in scored if reasons(m[0]) is None]
        selected = _best(passing)
        excluded = []
        if key is not None:
            chosen = {m[0].id for m in selected}
            excluded = [
                Excluded(id=e.id, name=e.name, reason=reasons(e))
                for e, _, _ in _best(scored)
                if e.id not in chosen and reasons(e) is not None
            ][:EXCLUDED_LIMIT]

        # Exact and alias matches are equally strong: two of them are ambiguous.
        strong = {"exact", "alias"}
        top = [
            m for m in selected if m[1] == selected[0][1] or (m[1] in strong and selected[0][1] in strong)
        ]
        ambiguous = len(top) > 1
        page = selected[offset : offset + limit]
        truncated = offset + limit < len(selected)
        return SearchResult(
            query=query,
            matches=tuple(Match(id=e.id, name=e.name, tier=tier, score=round(score, 3)) for e, tier, score in page),
            total_matches=len(selected),
            ambiguous=ambiguous,
            clarify=self._clarify(kind, [m[0].id for m in top]) if ambiguous else None,
            truncated=truncated,
            next_cursor=str(offset + limit) if truncated else None,
            excluded=tuple(excluded),
        )

    def _clarify(self, kind: Kind, ids: list[str]) -> str:
        """The first attribute whose values tell the equally good matches apart."""
        c = self.catalog
        attributes: dict[Kind, list[tuple[str, Callable]]] = {
            "services": [("specialty", lambda i: c.type_by_id[i].specialty)],
            "providers": [
                ("specialty", lambda i: c.provider_by_id[i].specialty),
                ("location", lambda i: c.provider_by_id[i].location_ids),
                ("title", lambda i: c.provider_by_id[i].title),
                ("language", lambda i: c.provider_by_id[i].languages),
            ],
            "locations": [],
        }
        for name, value in attributes[kind]:
            if len({value(i) for i in ids}) > 1:
                return name
        return "name"


def _score(query: tuple[str, ...], entry: _Entry) -> Optional[tuple[Tier, float]]:
    """The entry's best (tier, score) for the query, or None if it doesn't match."""
    if not query:
        return None
    if query == entry.name_key:
        return "exact", 1.0
    if query in entry.alias_keys:
        return "alias", 1.0
    best: Optional[tuple[Tier, float]] = None
    for key in (entry.name_key, *entry.alias_keys):
        found = _score_key(query, key)
        if found and (best is None or (_TIERS.index(found[0]), -found[1]) < (_TIERS.index(best[0]), -best[1])):
            best = found
    return best


def _score_key(query: tuple[str, ...], key: tuple[str, ...]) -> Optional[tuple[Tier, float]]:
    if not key:
        return None
    words = set(key)
    if words.issuperset(query):
        return "tokens", len(set(query)) / len(words)
    ratios = [max(_word_ratio(q, w) for w in key) for q in query]
    if all(r >= FUZZY_WORD for r in ratios):
        return "fuzzy", sum(ratios) / len(ratios)
    whole = SequenceMatcher(None, "".join(query), "".join(key)).ratio()
    if whole >= FUZZY_WHOLE:
        return "fuzzy", whole
    hits = sum(1 for q in query if q in words)
    if len(query) > 1 and hits * 2 >= len(query):
        return "partial", hits / len(query)
    return None


def _word_ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) < FUZZY_MIN_LEN or len(b) < FUZZY_MIN_LEN:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _best(scored: list) -> list:
    """The matches in the best tier; exact and alias matches also keep `tokens` ones."""
    if not scored or scored[0][1] is None:
        return scored
    best = scored[0][1]
    keep = {"exact", "alias", "tokens"} if best in ("exact", "alias") else {best}
    return [m for m in scored if m[1] in keep]


def _only(ids: Optional[Iterable[str]], reason: str) -> Filter:
    allowed = None if ids is None else set(ids)
    return lambda i: None if allowed is None or i in allowed else reason


def _label(wanted: str, label: Callable[[str], str]) -> Filter:
    key = normalize(wanted)
    return lambda i: None if normalize(label(i)) == key else f"specialty is {label(i)}"


def _offset(cursor: Optional[str]) -> int:
    if cursor is None:
        return 0
    if not cursor.isdigit():
        raise ValueError(f"Invalid cursor {cursor!r}")
    return int(cursor)


def load_aliases(path: Path = ALIASES_PATH) -> dict:
    return json.loads(Path(path).read_text())


@cache
def default_index() -> CatalogIndex:
    return CatalogIndex(default_catalog(), load_aliases())
