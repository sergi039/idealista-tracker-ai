"""What may be recorded as a hazardous neighbour, and what may not.

The sibling of `services/place_rules.py`, for the question #437 asks: *is there
anything bad near this plot?* It is the same lesson as #171 (Google's `airport`
type covers helipads and aeroclubs) and #325 (a hospital campus indexed room by
room), arriving through OpenStreetMap instead of Places.

**Everything below was measured before it was written.** One Overpass query at
property 793's own coordinate (43.5702843, -5.7276638, the Xivares locality
centroid in Carreño), 6 km, the ten candidate tags `candidate_tags()`
declares,
answered on 2026-08-20 by `overpass.openstreetmap.fr` -- the three instances
this deployment is configured against were all refusing at the time, which is
#434's finding and the reason that ticket exists. The answer is committed
verbatim as `tests/data/osm_hazards_xivares_793.json`: **144 elements**, of
which

* 82 are `man_made=storage_tank`. Fourteen of them carry
  `content=gas, operator=Repsol Butano` and are the LPG spheres visible on the
  horizon in the advert's own photograph. Most of the rest carry
  `building=yes` and nothing else -- individual tanks in a tank farm, saying
  nothing about what is in them.
* 42 are `landuse=industrial`. Eight of those are a *polígono industrial* --
  a light-industry estate -- and among the named ones are `Alskin Cosmetics`
  (`industrial=laboratory`), `Neoalgae`, and `Centro de Transportes de Gijón`,
  a lorry park.
* 7 are `man_made=works`, and they include `Fábrica de Hielo` (an ice factory
  on the fish dock) and `Talleres Prendes` (a workshop) beside
  `Fábrica de Cementos Tudela Veguín`.

So the tag is not the severity, and a rules table is what separates them. The
rule this module owns, in one sentence: **a hazard has to say what it is.**
`landuse=industrial` and `man_made=works` are claims about zoning and about a
building; `plant:source=coal`, `content=gas`, `industrial=steelmaking`,
`landuse=landfill` and the word *cementos* in a name are claims about the
thing. Only the second kind qualifies, and each one carries the evidence that
admitted it, so the page can say *why* rather than only *that*.

Deliberately a leaf module, like `place_rules`: standard library only, no repo
imports, so the service, the scorer and the tests can all import it without an
import cycle. It decides one element at a time and knows nothing about
distance, grouping or storage -- those belong to `services/hazard_service.py`.

What it cannot answer is written down in the issue and is worth repeating here,
because a block that lists four facilities reads as a survey: OSM says a cement
works exists and says nothing about its emissions (PRTR-España publishes those
and is a separate ticket), nothing about measured air quality (Asturias runs a
station named *Xivares* inside this very urbanisation), and nothing at all
about a plant that is approved but not yet built.
"""

import unicodedata
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# What one Overpass query asks for
# ---------------------------------------------------------------------------

# 6 km, because that is the distance the issue's own measurement covers and
# every facility in it sits inside 5.4 km. It is not a free choice in the other
# direction either: this query runs on the *free* pass, once per ingested
# listing, and Overpass is the fragile part of this system (#434) -- a wider
# radius is a bigger answer from an instance that is already refusing this
# machine half the time. What a 6 km scan does and does not guarantee for an
# approximate coordinate is `hazard_service`'s to disclose, not to hide.
SEARCH_RADIUS_M = 6_000

# The whole answer is capped server-side, the way `services/pool_service.py`
# caps its own discovery query. 400 is headroom over the measurement rather
# than a guess: property 793's coordinate -- an industrial estuary, about the
# densest this search area gets -- answers with **144** elements at 6 km, and
# 82 of those are individual tanks in two tank farms. A scan that reaches the
# cap is disclosed rather than silently shortened, because a truncated list
# read as a complete one is the defect this whole feature exists to remove.
ELEMENT_LIMIT = 400

# The candidate tags. Each is a *candidate* set and none of them qualifies on
# its own -- see `classify`.
_CANDIDATE_TAGS: Tuple[Tuple[str, str], ...] = (
    ("power", "plant"),
    ("man_made", "works"),
    ("man_made", "storage_tank"),
    ("man_made", "chimney"),
    ("man_made", "wastewater_plant"),
    ("landuse", "industrial"),
    ("landuse", "quarry"),
    ("landuse", "landfill"),
    ("landuse", "port"),
    ("amenity", "waste_transfer_station"),
)


def candidate_tags() -> Tuple[Tuple[str, str], ...]:
    """The tags one hazard scan asks Overpass for."""
    return _CANDIDATE_TAGS


def overpass_query(lat: float, lon: float) -> str:
    """One union over every candidate tag, for `_overpass_elements`.

    `nwr` because a works, a quarry and a tank farm are ways and relations far
    more often than nodes, and `out center` is what gives each of them a point
    to measure from.

    It is deliberately **not** folded into the spec set
    `PropertyTravelService._osm_specs` builds, and that is a decision rather
    than an oversight. The issue suggested it, on the grounds that the presets
    already cost one shared round trip and hazards could therefore cost none.
    But the presets only run on the *paid* path, which this scan is not on:
    since #434 `enrich_property` measures travel in its decisive pass and
    reaches `enrich_free_sources` afterwards, so the presets run *before* this
    scan on a press and not at all on an ingest, while this scan runs on every
    ingested listing, free. (This paragraph said "travel after
    `enrich_free_sources`" until that was checked -- the ordering flipped the
    same day, and the conclusion survives the correction because it never
    rested on which came first.) Sharing the spec set would drag a 100 km aerodrome
    query and a 30 km beach query into every ingest, where neither runs today,
    to save one round trip on an Enrich press that is already spending on
    Google; and it would invalidate every cached preset cell at a moment when
    Overpass will not talk to the mini at all (#434). Two small queries beat
    one large one here. What is *not* duplicated is the client: the transport,
    the gate, the User-Agent and the three refusals stay in
    `EnrichmentService._overpass_elements`, reached the way
    `services/pool_service.py` reaches it.
    """
    around = f"around:{SEARCH_RADIUS_M},{lat},{lon}"
    clauses = "".join(
        f'nwr["{key}"="{value}"]({around});' for key, value in _CANDIDATE_TAGS
    )
    # 60 s rather than the 90 the preset query asks for: this one is a single
    # 6 km box and returned in a couple of seconds when it was measured.
    return f"[out:json][timeout:60];({clauses});out center tags {ELEMENT_LIMIT};"


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

# Two levels and no more. A third would invite a false precision this data
# cannot support: OSM says a landfill is there, not how it is run. `high` is a
# facility whose ordinary operation emits something -- combustion, dust,
# solvents, stored fuel or gas. `moderate` is a nuisance whose ordinary
# operation is noise, traffic, odour or spoil.
SEVERITY_HIGH = "high"
SEVERITY_MODERATE = "moderate"

_SEVERITY_RANK = {SEVERITY_HIGH: 0, SEVERITY_MODERATE: 1}


def severity_rank(severity: Any) -> int:
    """Sort key: 0 is worst. An unknown severity sorts last, never first."""
    return _SEVERITY_RANK.get(str(severity or ""), len(_SEVERITY_RANK))


@dataclass(frozen=True)
class HazardVerdict:
    """What this element is, and the tag that said so."""

    kind: str
    severity: str
    evidence: str


# ---------------------------------------------------------------------------
# The evidence tables
# ---------------------------------------------------------------------------

# A name is evidence when it names an industry. These are matched
# accent-folded and case-folded against `name`, and every one of them was
# chosen against the 144 elements above rather than from a dictionary: the
# entries that fire there are `cemento` (Tudela Veguín), `central termica`
# and `termica` (Aboño), `carbon` (Parque de Carbones), `regasificadora` (El
# Musel/Enagás), `aceria` (Veriña), `vertedero` (the ArcelorMittal tip),
# `depuradora` (EDAR La Reguerona), `cantera` (Aboño, Perecil) and `butano`
# (Factoría Repsol Butano).
#
# The entries that must *not* fire are the point of the list: nothing here
# matches `Fábrica de Hielo`, `Talleres Prendes`, `Alskin Cosmetics`,
# `Neoalgae`, `Centro de Transportes de Gijón`, `Moeve`, `Astilleros Armón`,
# `INDRA (El Tallerón)`, `Esmena / Mecalux`, `Industrias Metalicas Ruiz`,
# `EBHISA`, or any of the eight `Polígono Industrial ...` estates.
# Words that say a *depuradora* purifies shellfish rather than sewage.
# Words that say a chimney is kept rather than used.
_PRESERVED_STACK = ("antigua", "antiga", "antiguo", "vella", "vieja")

_SHELLFISH = ("marisco", "mariscos", "cetarea", "cetaria", "moluscos", "mejillon")

# ...and words that say a *central térmica* is solar. A concentrated-solar
# plant is a *central térmica solar* or a *central termosolar* in Spanish, and
# it burns nothing.
_SOLAR = ("solar", "termosolar", "fotovoltaica", "fotovoltaico")

# `(folded needle, kind, severity)` or `(..., words that disqualify it)`.
_NAME_EVIDENCE: Tuple[Tuple, ...] = (
    # (folded substring, kind, severity)
    ("cemento", "cement_works", SEVERITY_HIGH),
    ("cement works", "cement_works", SEVERITY_HIGH),
    ("aceria", "steelworks", SEVERITY_HIGH),
    ("siderur", "steelworks", SEVERITY_HIGH),
    ("altos hornos", "steelworks", SEVERITY_HIGH),
    ("refineria", "refinery", SEVERITY_HIGH),
    ("refinery", "refinery", SEVERITY_HIGH),
    ("petroquim", "chemical_works", SEVERITY_HIGH),
    ("quimica", "chemical_works", SEVERITY_HIGH),
    ("central termica", "power_plant", SEVERITY_HIGH, _SOLAR),
    ("power station", "power_plant", SEVERITY_HIGH),
    ("regasificadora", "lng_terminal", SEVERITY_HIGH),
    ("butano", "lpg_storage", SEVERITY_HIGH),
    ("propano", "lpg_storage", SEVERITY_HIGH),
    # `cantera` is deliberately absent. It is the ordinary Spanish word for a
    # club's youth academy as well as for a quarry, and on the measured data it
    # earns nothing: both canteras at property 793 are tagged `landuse=quarry`
    # and qualify on the tag alone. A name entry that catches nothing the tags
    # miss and misfires on an everyday word is a cost with no measurement
    # behind it (review, 2026-08-20).
    # `carbonera` is deliberately absent for the reason `cantera` is: it
    # matches *Carboneras*, an Almerían municipality whose name is on every
    # industrial estate in it, and it caught nothing the entry below misses --
    # both coal yards in the fixture are *Parque de Carbones*.
    ("carbones", "coal_yard", SEVERITY_HIGH),
    ("coal yard", "coal_yard", SEVERITY_HIGH),
    ("incinerad", "incinerator", SEVERITY_HIGH),
    ("papelera", "paper_mill", SEVERITY_HIGH),
    ("fundicion", "foundry", SEVERITY_HIGH),
    ("coquer", "coking_plant", SEVERITY_HIGH),
    ("vertedero", "landfill", SEVERITY_HIGH),
    ("escombrera", "landfill", SEVERITY_HIGH),
    # *Depuradora* is a sewage works in Asturias and a **shellfish purification
    # plant** on the Galician coast, and both are mapped `landuse=industrial`
    # with nothing but a name. Codex found two real ones -- `way/407548492`
    # *Depuradora e Cetaria de Mariscos* and `way/498273059* *Depuradora de
    # Marisco* -- so the entry carries the words that say which sense is meant.
    # Narrow and measured beats dropping the entry: a sewage works mapped by
    # name alone is exactly what it is here to catch.
    ("depuradora", "wastewater_plant", SEVERITY_MODERATE, _SHELLFISH),
    ("edar ", "wastewater_plant", SEVERITY_MODERATE, ()),
)

# `plant:source` values whose plant burns something. Everything absent from
# this map -- wind, solar, hydro, and a `power=plant` that declares no source
# at all -- is refused, because a wind farm is not what this block is for and
# an undeclared plant has told us nothing.
_COMBUSTION_SOURCES = frozenset(
    {
        "coal",
        "gas",
        "oil",
        "diesel",
        "waste",
        "biomass",
        "biofuel",
        "biogas",
        "coke",
        "peat",
        "oil_shale",
        "blast furnace gas",
        "blast_furnace_gas",
    }
)

# Not combustion, and not something to walk past either. Kept apart from the
# set above because the kind and the reason differ: nothing here burns.
_NON_COMBUSTION_PLANT_SOURCES: Dict[str, Tuple[str, str]] = {
    "nuclear": ("nuclear_plant", SEVERITY_HIGH),
}

# `industrial=*` values that name a hazardous industry. The refusals matter as
# much as the accepts: `laboratory` (Alskin Cosmetics), `warehouse` (Moeve),
# `metal_fabrication` (INDRA) and `shipyard` (Astilleros Armón) are all
# present in the measured data and none of them is here.
# Words that say an `industrial=oil` works presses olives rather than storing
# petroleum.
_EDIBLE_OIL = ("almazara", "aceite", "aceitera", "oliva", "olivar", "orujo")

_INDUSTRIAL_EVIDENCE: Dict[str, Tuple] = {
    "steelmaking": ("steelworks", SEVERITY_HIGH),
    "steel": ("steelworks", SEVERITY_HIGH),
    "smelting": ("foundry", SEVERITY_HIGH),
    "foundry": ("foundry", SEVERITY_HIGH),
    "refinery": ("refinery", SEVERITY_HIGH),
    # In Spanish, *aceite* is oil too. `way/591673652` is *Almazara Molino de
    # las Torres*, an olive-oil mill tagged `industrial=oil`, and it came back
    # a high-severity fuel depot (codex review, 2026-08-20) -- while the same
    # tag on El Musel is Exolum's petroleum terminals, which is why the entry
    # stays and carries the words that say which sense is meant. The same
    # ambiguity took `product=oil` out of the table entirely; here the tag has
    # two measured true positives to lose.
    "oil": ("fuel_depot", SEVERITY_HIGH, _EDIBLE_OIL),
    "oil_tank_farm": ("fuel_depot", SEVERITY_HIGH),
    # `way/885803865` carries it, beside `content=gas` (codex review).
    "gas_storage": ("fuel_depot", SEVERITY_HIGH),
    "gas": ("fuel_depot", SEVERITY_HIGH),
    "chemical": ("chemical_works", SEVERITY_HIGH),
    "petrochemical": ("chemical_works", SEVERITY_HIGH),
    "cement": ("cement_works", SEVERITY_HIGH),
    "coking": ("coking_plant", SEVERITY_HIGH),
    "paper_mill": ("paper_mill", SEVERITY_HIGH),
    "tannery": ("tannery", SEVERITY_HIGH),
    "slaughterhouse": ("slaughterhouse", SEVERITY_MODERATE),
    "asphalt": ("asphalt_plant", SEVERITY_HIGH),
    "concrete": ("concrete_plant", SEVERITY_MODERATE),
    # The tag CEMEX's own objects carry (`way/1221635493`), refused because
    # only the bare word was listed (codex review, 2026-08-20).
    "concrete_plant": ("concrete_plant", SEVERITY_MODERATE),
    "cement_plant": ("cement_works", SEVERITY_HIGH),
    "mine": ("mine", SEVERITY_MODERATE),
    "quarry": ("quarry", SEVERITY_MODERATE),
}

# `product=*` is the most direct claim an industrial plant can make about
# itself, and the issue named it first: *"tags that are claims about what the
# thing is -- `plant:source=coal`, `product=cement`"*. It was missing, and
# codex found the object that proves the cost: `relation/11519713`,
# *Asturiana de Zinc* in San Juan de Nieva -- a zinc smelter and sulfuric-acid
# plant inside the owner's own search area, tagged `man_made=works`,
# `product=zinc`, `operator=Glencore`, with a name that says nothing an
# industry vocabulary can read. Without this table it classified as nothing at
# all.
_PRODUCT_EVIDENCE: Dict[str, Tuple[str, str]] = {
    "cement": ("cement_works", SEVERITY_HIGH),
    "concrete": ("concrete_plant", SEVERITY_MODERATE),
    "lime": ("cement_works", SEVERITY_HIGH),
    # `steel` and `iron` are as broad as `metal` was: a fabricator that cuts
    # and welds steel sections tags `product=steel` exactly as a mill does,
    # and a real steelworks says so in `industrial=steelmaking` or in its own
    # name. Out for the same reason and by the same measurement.
    # **No bare metal is here, and that costs a real facility.** `product=X`
    # says what comes *out*, never what process made it: `way/1068457365` is
    # *Balumco*, `man_made=works` + `product=aluminum`, and the Catalan
    # environmental register describes extrusion and anodising -- not primary
    # smelting (codex review, 2026-08-20). Nothing structural separates it
    # from `relation/11519713`, *Asturiana de Zinc*, which really is a
    # smelter: same `man_made=works`, same bare product tag. OSM cannot answer
    # "is this a smelter" from `product` alone, so this table does not
    # pretend to -- a smelter has to say so in `industrial=smelting`, in
    # `industrial=steelmaking`, or in its own name. The price is that AZSA
    # classifies as nothing until somebody tags it with a process, and saying
    # that out loud is the point: the alternative is every aluminium
    # extruder in Spain reported as a smelter.
    # `metal` and `oil` are deliberately absent, and both were measured.
    # `node/13016693457` is *Alcyon*, `man_made=works` + `product=metal` -- a
    # metal-parts manufacturer on a Basque industrial estate, not a smelter.
    # `way/485376150` is *Molino aceitero*, `man_made=works` + `product=oil` --
    # an olive-oil mill, because in Spanish OSM *aceite* is oil too. Only the
    # products that name a process nobody runs by accident are here; the
    # specific metals stay, `petroleum` carries the refinery sense.
    "coke": ("coking_plant", SEVERITY_HIGH),
    "paper": ("paper_mill", SEVERITY_HIGH),
    "pulp": ("paper_mill", SEVERITY_HIGH),
    "chemicals": ("chemical_works", SEVERITY_HIGH),
    "chemical": ("chemical_works", SEVERITY_HIGH),
    "fertilizer": ("chemical_works", SEVERITY_HIGH),
    "fertiliser": ("chemical_works", SEVERITY_HIGH),
    "sulfuric_acid": ("chemical_works", SEVERITY_HIGH),
    "glass": ("glassworks", SEVERITY_HIGH),
    "asphalt": ("asphalt_plant", SEVERITY_HIGH),
    "bitumen": ("asphalt_plant", SEVERITY_HIGH),
    "gypsum": ("cement_works", SEVERITY_MODERATE),
    "petroleum": ("refinery", SEVERITY_HIGH),
}

# What a storage tank may be holding for it to count. A tank with
# `building=yes` and nothing else -- 55 of the 82 at property 793 -- has made
# no claim, and inventing one for it is the STATUS-002 mistake in a new column.
_TANK_CONTENTS: Dict[str, Tuple[str, str]] = {
    "gas": ("lpg_storage", SEVERITY_HIGH),
    "lpg": ("lpg_storage", SEVERITY_HIGH),
    "propane": ("lpg_storage", SEVERITY_HIGH),
    "butane": ("lpg_storage", SEVERITY_HIGH),
    "lng": ("lng_terminal", SEVERITY_HIGH),
    "cng": ("lpg_storage", SEVERITY_HIGH),
    # Not `oil` on its own: `way/550880773` and `way/550880775` are
    # olive-oil tanks inside SCA San Antonio (`industrial=olive_oil`), and the
    # word means both in Spain -- the same ambiguity that took `product=oil`
    # out of the table and put a guard on `industrial=oil` (codex review,
    # 2026-08-20). A tank that says `fuel`, `diesel` or `petroleum` has said
    # which one it holds.
    "fuel": ("fuel_depot", SEVERITY_HIGH),
    "diesel": ("fuel_depot", SEVERITY_HIGH),
    "gasoline": ("fuel_depot", SEVERITY_HIGH),
    "petroleum": ("fuel_depot", SEVERITY_HIGH),
    "kerosene": ("fuel_depot", SEVERITY_HIGH),
    "chemical": ("chemical_works", SEVERITY_HIGH),
    "ammonia": ("chemical_works", SEVERITY_HIGH),
    "hydrogen": ("chemical_works", SEVERITY_HIGH),
    "slurry": ("wastewater_plant", SEVERITY_MODERATE),
}

# Tags that qualify on their own, because the tag *is* the claim about what
# the thing is. There is no way to map a landfill as a landfill and mean a
# business park.
_TAG_EVIDENCE: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("landuse", "landfill"): ("landfill", SEVERITY_HIGH),
    ("landuse", "quarry"): ("quarry", SEVERITY_MODERATE),
    ("landuse", "port"): ("port_industry", SEVERITY_MODERATE),
    ("man_made", "wastewater_plant"): ("wastewater_plant", SEVERITY_MODERATE),
    ("amenity", "waste_transfer_station"): ("waste_transfer", SEVERITY_MODERATE),
}

# Keys that mean the thing is not there any more.
#
# A lifecycle *prefix* means "gone" only when it prefixes a key that would
# itself have been evidence. That distinction is the whole rule, and both
# halves of it cost a review round. `was:name=Ensidesa` on
# *Acería de Veriña - ArcelorMittal* is the plant's renaming history through
# Ensidesa -> Aceralia -> Arcelor -> ArcelorMittal, and `disused:railway=rail`
# on a live works is a siding -- reading either as "gone" erased a live
# hazard. But `disused:power=plant` **is** the claim, and codex found two real
# ones: `way/16851312` *Central térmica del Narcea* and `way/88799255`
# *Central Térmica de Meirama*, both carrying `disused:power=plant`,
# `disused:plant:source=coal` and `end_date=2020-06-30`, still tagged
# `landuse=industrial` and still named *Central térmica*. Spain closed them in
# June 2020; the name rule reported them as emitting power stations.
# `proposed` and `construction` are here for the other direction: a plant that
# does not exist yet is not a neighbour, and its name is already on the map.
_LIFECYCLE_PREFIXES = (
    "disused",
    "abandoned",
    "ruins",
    "demolished",
    "razed",
    "removed",
    "destroyed",
    "was",
    "proposed",
    "construction",
    "planned",
)

# The keys a prefix has to be attached to for the prefix to mean the hazard is
# gone. `name` is deliberately absent -- that is the `was:name` case above.
# Only the keys a *hazard* is established by. `amenity` and `content` are
# gone: `disused:amenity=fuel` is a closed petrol station, and on a refinery
# whose only evidence is its name it suppressed that name and dropped the
# refinery entirely (found in review, 2026-08-20). A retired thing silences
# the name when the retired thing is the kind of thing the name is about.
_EVIDENCE_KEYS = frozenset(
    {"power", "man_made", "landuse", "industrial", "product", "plant"}
)

_DISUSED_KEYS = ("disused", "abandoned", "ruins", "demolished", "razed")

# `historic` is a claim about significance, not about status, so it refuses
# only on the values that say the thing is preserved rather than running.
# `Antigua chimenea de Cristasa` carries `historic=monument` and is a listed
# brick stack in the middle of Gijón; `historic=archaeological_site` sits on
# land whose *significance* is underground and says nothing about the landfill
# on top of it.
_HISTORIC_DISUSED_VALUES = frozenset(
    {"monument", "memorial", "ruins", "heritage", "industrial", "mine"}
)

# Values that read as "no" on a bare lifecycle key.
_NEGATIVE_VALUES = frozenset({"no", "false", "0"})


def fold(text: Any) -> str:
    """Case- and accent-folded text, for matching a Spanish name.

    `Acería` and `Aceria`, `Fábrica` and `Fabrica` are the same word to a
    reader and two different strings to `in`. This is the same folding
    `utils/municipality_codes.normalize()` applies to both sides of the INE
    join; it is repeated here rather than imported because this module is a
    leaf by design (see the module docstring) and the two do different jobs
    with it -- that one builds a key, this one matches a substring.
    """
    if text is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold().strip()


def _range_end(raw: str) -> str:
    """The last endpoint of an OSM date range, or the value unchanged.

    OSM's date specification allows `1990..2020`, `1990-2020`, an open start
    (`-1964`), and full dates on both sides (`2020-01-01--2022-06-30`). All of
    them failed to parse as a single date and therefore read as *active* -- a
    plant closed years ago, back on the map (codex review, 2026-08-20).

    Only forms whose tail is itself a date are split, which is what keeps
    `2020-06` a month rather than a range from 2020 to June.
    """
    for separator in ("--", ".."):
        if separator in raw:
            return raw.rsplit(separator, 1)[-1].strip()
    if raw.startswith("-"):
        return raw[1:].strip()
    # `A-B` where B is a whole date of its own: `1990-2020`, and
    # `2020-01-01-2022-06-30`, whose tail is the last three groups.
    groups = raw.split("-")
    if len(groups) in (2, 4, 6):
        tail = "-".join(groups[len(groups) // 2 :]).strip()
        if _looks_like_a_date(tail):
            return tail
    return raw


def _looks_like_a_date(text: str) -> bool:
    parts = text.split("-")
    if not 1 <= len(parts) <= 3:
        return False
    if len(parts[0]) != 4 or not parts[0].isdigit():
        return False
    return all(part.isdigit() for part in parts[1:])


def _closed_before_today(tags: Dict[str, Any]) -> bool:
    """Does `end_date` name a day that has already passed?

    A plain year or year-month counts as its last day, so a plant that closed
    in 2020 is closed whether the mapper wrote `2020`, `2020-06` or
    `2020-06-30`. Anything this cannot parse -- an OSM date range, a `~2020`
    -- is left alone: a date nobody can read is not evidence of a closure.
    """
    raw = str(tags.get("end_date") or "").strip()
    if not raw:
        return False
    # OSM's date specification allows a range, and both spellings turn up:
    # `2020..2022` and `2020-2022`. The end of the range is the date that
    # matters, and reading the whole string as one date failed to parse and
    # therefore read as *active* -- a retired plant back on the map (codex
    # review, 2026-08-20). The second form is only a range when the tail is a
    # bare year, or `2020-06` would be split into `2020` and `06`.
    raw = _range_end(raw)
    for pattern, roll in (("%Y-%m-%d", 0), ("%Y-%m", 1), ("%Y", 2)):
        try:
            parsed = datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
        if roll == 2:
            parsed = date(parsed.year, 12, 31)
        elif roll == 1:
            month_end = date(parsed.year + parsed.month // 12, parsed.month % 12 + 1, 1)
            parsed = month_end - timedelta(days=1)
        return parsed < date.today()
    return False


def _is_disused(tags: Dict[str, Any]) -> bool:
    """Is the whole element gone?

    Only a claim about the element itself counts here: a bare `disused=yes`,
    a `historic` value that means preserved rather than running, or an
    `end_date` already past. A lifecycle *prefix* is not that -- see
    `_retired_evidence` for what it really says.
    """
    if _closed_before_today(tags):
        return True
    for key, value in tags.items():
        name = str(key).strip().casefold()
        if name in _DISUSED_KEYS and fold(value) not in _NEGATIVE_VALUES:
            return True
        if name == "historic" and fold(value) in _HISTORIC_DISUSED_VALUES:
            return True
    return False


def _retired_evidence(tags: Dict[str, Any]) -> bool:
    """Has a key that *would* have been evidence been moved under a prefix?

    `disused:power=plant` says the power plant is not one any more, and that
    is what makes *Central térmica del Narcea* -- still `landuse=industrial`,
    still named *Central térmica* -- not a hazard. But it says nothing about
    the rest of the element, and reading it as if it did was wrong twice on
    real data (codex review, 2026-08-20): `way/485376150`-style
    `{'industrial': 'chemical', 'name': 'Química Activa',
    'disused:power': 'plant'}` is a live chemical works with a dead power
    plant on site, and `{'product': 'zinc', 'was:product': 'lead'}` is a
    smelter that changed what it makes.

    So this refuses the **name** only -- the name describes what the thing was
    called, and a retired process is exactly what makes the name stale -- and
    a bare tag saying what the element *is* now always outranks it. A prefixed
    key whose bare form is still present says nothing at all: the bare one is
    the current state.
    """
    for key in tags:
        name = str(key).strip().casefold()
        prefix, _, rest = name.partition(":")
        if prefix not in _LIFECYCLE_PREFIXES or not rest:
            continue
        # `disused:plant:source` counts as `plant`, so the first segment after
        # the prefix is what decides.
        bare = rest.partition(":")[0]
        if bare in _EVIDENCE_KEYS and bare not in tags:
            return True
    return False


def _name_tokens(name: str) -> Tuple[str, ...]:
    return tuple(
        token
        for token in "".join(ch if ch.isalnum() else " " for ch in name).split()
        if token
    )


def _name_says(tokens: Tuple[str, ...], needle: str) -> bool:
    """Does this name contain the needle as whole words?

    Substring matching was the first version and it is not safe on a single
    ordinary word. Measured against the entries this table actually carries:
    `cantera` is the standard Spanish word for a club's youth academy, so
    *Escuela de Fútbol La Cantera* read as a quarry; and `quimica` matched
    inside *Bioquímica*, so a laboratory the table deliberately refuses came
    back as a chemical works (review, 2026-08-20). Worse than either, a name
    match wins over tag evidence, so an LPG tank on *Polígono La Cantera* was
    reported as a moderate quarry rather than the high-severity `content=gas`
    the tag on it stated.

    The last token may be a prefix of the name's own, which is what keeps the
    deliberate stems working -- `incinerad` inside *incineradora*, `siderur`
    inside *siderúrgica*, `cemento` inside *Cementos* -- while `quimica` no
    longer reaches inside *bioquímica*, because that is a prefix in the other
    direction.
    """
    parts = _name_tokens(needle)
    if not parts:
        return False
    span = len(parts)
    for start in range(len(tokens) - span + 1):
        window = tokens[start : start + span]
        if window[:-1] == parts[:-1] and window[-1].startswith(parts[-1]):
            return True
    return False


def classify(tags: Optional[Dict[str, Any]]) -> Optional[HazardVerdict]:
    """What this OSM element is, or None when it has not said.

    Both kinds of evidence are read and the **more severe** wins. The name has
    to be read at all because OSM's tagging is sometimes coarser than its
    labelling: at property 793 the coal yard of El Musel is mapped
    `landuse=quarry` and named *Parque de carbones*, and the cement works is
    `man_made=works` with no `product` tag -- reading tags alone would report a
    quarry and an unclassified works. But letting the name win *outright* is
    how an LPG tank at *Polígono La Cantera* came back as a moderate quarry
    over its own `content=gas` (review, 2026-08-20), and understating a real
    hazard is strictly worse than reporting a spurious one.
    """
    if not isinstance(tags, dict) or not tags:
        return None
    if _is_disused(tags):
        return None

    by_name = None if _retired_evidence(tags) else _name_verdict(tags)
    by_tag = _tag_verdict(tags)
    # A declared source contradicts a name that claims combustion. Spain's
    # concentrated-solar plants are *centrales térmicas solares*, and the
    # name rule would otherwise report one as a coal station -- the tag says
    # what it burns, and for solar, wind and hydro the answer is nothing.
    if by_name is not None and by_name.kind == "power_plant":
        source = fold(tags.get("plant:source"))
        if source and source not in _COMBUSTION_SOURCES:
            by_name = None
    # The **more severe** of the two wins, and a tie goes to the name. That
    # keeps every reason the name is read at all -- the cement works carries no
    # `product` tag, and El Musel's coal yard is mapped `landuse=quarry` -- and
    # removes the way that ordering used to bite back: an incidental
    # place-name fragment must never *downgrade* a hazard whose own tag says
    # what it holds.
    if by_name is None:
        return by_tag
    if by_tag is None:
        return by_name
    # A tie goes to the **tag**, because a tag is a claim about the thing and
    # a name is a claim about what somebody called it. `way/459067378` is the
    # measured case: `landuse=landfill` named *Escombrera central térmica* --
    # the power station's own spoil tip. Both readings are high, and letting
    # the name win reported a coal-fired power station where there is a heap
    # of ash (codex review, 2026-08-20). Nothing in the fixture changes: every
    # tie there agrees with itself, and every case the name exists for -- the
    # cement works with no `product`, the coal yard mapped as a quarry -- is
    # decided on severity before it reaches here.
    return (
        by_name
        if severity_rank(by_name.severity) < severity_rank(by_tag.severity)
        else by_tag
    )


def _name_verdict(tags: Dict[str, Any]) -> Optional[HazardVerdict]:
    tokens = _name_tokens(fold(tags.get("name")))
    if not tokens:
        return None
    for entry in _NAME_EVIDENCE:
        needle, kind, severity = entry[0], entry[1], entry[2]
        unless = entry[3] if len(entry) > 3 else ()
        if not _name_says(tokens, needle):
            continue
        if any(_name_says(tokens, word) for word in unless):
            continue
        return HazardVerdict(
            kind=kind, severity=severity, evidence=f"name:{needle.strip()}"
        )
    return None


def _tag_verdict(tags: Dict[str, Any]) -> Optional[HazardVerdict]:
    product = fold(tags.get("product"))
    if product in _PRODUCT_EVIDENCE:
        kind, severity = _PRODUCT_EVIDENCE[product]
        return HazardVerdict(
            kind=kind, severity=severity, evidence=f"product={product}"
        )

    industrial = fold(tags.get("industrial"))
    if industrial in _INDUSTRIAL_EVIDENCE:
        entry = _INDUSTRIAL_EVIDENCE[industrial]
        kind, severity = entry[0], entry[1]
        unless = entry[2] if len(entry) > 2 else ()
        tokens = _name_tokens(fold(tags.get("name")))
        if any(_name_says(tokens, word) for word in unless):
            return None
        return HazardVerdict(
            kind=kind, severity=severity, evidence=f"industrial={industrial}"
        )

    if fold(tags.get("power")) == "plant":
        source = fold(tags.get("plant:source"))
        method = fold(tags.get("plant:method"))
        if source in _NON_COMBUSTION_PLANT_SOURCES:
            kind, severity = _NON_COMBUSTION_PLANT_SOURCES[source]
            return HazardVerdict(
                kind=kind, severity=severity, evidence=f"plant:source={source}"
            )
        if source in _COMBUSTION_SOURCES:
            return HazardVerdict(
                kind="power_plant",
                severity=SEVERITY_HIGH,
                evidence=f"plant:source={source}",
            )
        if method == "combustion":
            return HazardVerdict(
                kind="power_plant",
                severity=SEVERITY_HIGH,
                evidence="plant:method=combustion",
            )
        return None

    if fold(tags.get("man_made")) == "storage_tank":
        content = fold(tags.get("content"))
        if content in _TANK_CONTENTS:
            kind, severity = _TANK_CONTENTS[content]
            return HazardVerdict(
                kind=kind, severity=severity, evidence=f"content={content}"
            )
        return None

    if fold(tags.get("man_made")) == "chimney":
        # A stack is evidence of combustion, but only when it belongs to
        # something: an unnamed, unattributed chimney has said nothing about
        # what burns under it, and three of the nine at property 793 are
        # exactly that. The two that do qualify carry `operator=ArcelorMittal`
        # and collapse into that facility rather than standing as hazards of
        # their own.
        # `antigua`/`antiga` names a preserved stack, and Spain and Catalonia
        # are full of them -- `node/12460849210` is *Xemeneia de l'antiga
        # Inpacsa*, which carries no `historic` tag to refuse it by (codex
        # review, 2026-08-20).
        name_tokens = _name_tokens(fold(tags.get("name")))
        if any(_name_says(name_tokens, word) for word in _PRESERVED_STACK):
            return None
        if tags.get("operator") or tags.get("name"):
            return HazardVerdict(
                kind="combustion_stack",
                severity=SEVERITY_MODERATE,
                evidence="man_made=chimney",
            )
        return None

    for (key, value), (kind, severity) in _TAG_EVIDENCE.items():
        if fold(tags.get(key)) == value:
            return HazardVerdict(
                kind=kind, severity=severity, evidence=f"{key}={value}"
            )

    # `landuse=industrial` and `man_made=works` reach here with nothing to
    # show, and that is the whole point of the table: zoning is not a hazard
    # and a building is not an industry.
    return None


# ---------------------------------------------------------------------------
# One facility, however many elements OSM maps it as
# ---------------------------------------------------------------------------

# Below this many characters a key is not distinctive enough to absorb another
# one by containment: `edp` inside `edpuerto` would be a merge nobody asked
# for. Operators short enough to fall under it still group with themselves --
# an exact key match needs no containment at all.
_MIN_CONTAINMENT_KEY_CHARS = 5


def operator_key(tags: Optional[Dict[str, Any]]) -> Optional[str]:
    """The folded operator, or None. What a facility key may be *absorbed* by."""
    if not isinstance(tags, dict):
        return None
    return fold(tags.get("operator")) or None


def facility_key(tags: Optional[Dict[str, Any]]) -> Optional[str]:
    """Which facility this element belongs to, as a folded string.

    The operator first, because it is the claim about ownership; the name only
    when there is no operator. Measured at property 793, that is what collapses
    fourteen `content=gas` spheres and the polygon around them into
    *Repsol Butano*, and the two `power=plant` turbines #437's acceptance
    criteria name -- *Turbina A* and *Turbina B*, both `operator=ArcelorMittal`
    -- into the steelworks rather than leaving them as two extra hazards. It is
    the same defect as #325's hospital campus indexed room by room.

    An element with neither is not keyed here at all: the service clusters
    those by position, since an unnamed tank has told us nothing but where it
    is.
    """
    if not isinstance(tags, dict):
        return None
    for key in ("operator", "name"):
        folded = fold(tags.get(key))
        if folded:
            return folded
    return None


def _tokens(key: str) -> List[str]:
    return [
        token
        for token in "".join(ch if ch.isalnum() else " " for ch in key).split()
        if token
    ]


def _contains(haystack: List[str], needle: List[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[i : i + len(needle)] == needle
        for i in range(len(haystack) - len(needle) + 1)
    )


def merge_keys(keys: Iterable[str], absorbing: Iterable[str]) -> Dict[str, str]:
    """`{key: canonical key}`, folding a facility name into its operator.

    OSM does not tag one facility consistently. At property 793 the steelworks
    is `landuse=industrial` named *Acería de Veriña - ArcelorMittal* with **no**
    operator, its tip is named *Vertedero ArcelorMittal*, and its two turbines
    and two stacks carry `operator=ArcelorMittal` and nothing else. Grouping by
    the key alone would report that one plant four times.

    So a key whose tokens *contain* another key's tokens contiguously is folded
    into the shorter one -- `arcelormittal` absorbs both names above, and
    `exolum` absorbs `exolum - musel i`, `- ii` and `- iii`.

    **Only an operator may absorb**, and that is the guard that matters. The
    first version let any short key do it, and codex found what that costs on
    real data: `way/231335217` is a quarry whose entire name is *Cantera*, and
    `way/169318445` is a different quarry named *Cantera Blokdegal S.A.* -- the
    generic name swallowed the specific one and reported two workings as one.
    An operator is a claim about who runs a thing; a name is just what somebody
    typed. The other two guards stay: the absorbing key must be at least
    `_MIN_CONTAINMENT_KEY_CHARS` long, and the match is over whole tokens in
    order, so `mar` never absorbs `marisma`. Anything not absorbed stays its
    own facility, which is the safe direction: two rows for one plant
    over-reports, one row for two plants hides one.
    """
    unique = sorted({key for key in keys if key}, key=lambda k: (len(k), k))
    # Required rather than defaulted, and deliberately so: the default used to
    # be "anything may absorb", which is the defect this argument exists to
    # remove, sitting one forgotten keyword away from coming back.
    may_absorb = {key for key in absorbing if key}
    canonical: Dict[str, str] = {key: key for key in unique}
    token_cache = {key: _tokens(key) for key in unique}
    for index, longer in enumerate(unique):
        if longer in may_absorb:
            # An operator is somebody's own claim about who runs a thing, so
            # it never disappears into another operator. `operator=Norte
            # Ambiental` and `operator=Servicios Norte Ambiental` are two
            # companies, and folding the second into the first produced one
            # item wearing the far facility's identity and the near one's
            # distance (codex review, 2026-08-20). Only a *name* may fold.
            continue
        for shorter in unique[:index]:
            if shorter not in may_absorb:
                continue
            if len(shorter) < _MIN_CONTAINMENT_KEY_CHARS:
                continue
            if _contains(token_cache[longer], token_cache[shorter]):
                canonical[longer] = canonical[shorter]
                break
    return canonical
