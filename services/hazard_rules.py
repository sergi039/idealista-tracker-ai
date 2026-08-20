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
    But the presets only run on the *paid* path -- `enrich_property` reaches
    travel after `enrich_free_sources` -- while this scan runs on every
    ingested listing, free. Sharing the spec set would drag a 100 km aerodrome
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
_NAME_EVIDENCE: Tuple[Tuple[str, str, str], ...] = (
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
    ("central termica", "power_plant", SEVERITY_HIGH),
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
    ("carbones", "coal_yard", SEVERITY_HIGH),
    ("carbonera", "coal_yard", SEVERITY_HIGH),
    ("coal yard", "coal_yard", SEVERITY_HIGH),
    ("incinerad", "incinerator", SEVERITY_HIGH),
    ("papelera", "paper_mill", SEVERITY_HIGH),
    ("fundicion", "foundry", SEVERITY_HIGH),
    ("coquer", "coking_plant", SEVERITY_HIGH),
    ("vertedero", "landfill", SEVERITY_HIGH),
    ("escombrera", "landfill", SEVERITY_HIGH),
    ("depuradora", "wastewater_plant", SEVERITY_MODERATE),
    ("edar ", "wastewater_plant", SEVERITY_MODERATE),
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

# `industrial=*` values that name a hazardous industry. The refusals matter as
# much as the accepts: `laboratory` (Alskin Cosmetics), `warehouse` (Moeve),
# `metal_fabrication` (INDRA) and `shipyard` (Astilleros Armón) are all
# present in the measured data and none of them is here.
_INDUSTRIAL_EVIDENCE: Dict[str, Tuple[str, str]] = {
    "steelmaking": ("steelworks", SEVERITY_HIGH),
    "steel": ("steelworks", SEVERITY_HIGH),
    "smelting": ("foundry", SEVERITY_HIGH),
    "foundry": ("foundry", SEVERITY_HIGH),
    "refinery": ("refinery", SEVERITY_HIGH),
    "oil": ("fuel_depot", SEVERITY_HIGH),
    "oil_tank_farm": ("fuel_depot", SEVERITY_HIGH),
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
    "mine": ("mine", SEVERITY_MODERATE),
    "quarry": ("quarry", SEVERITY_MODERATE),
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
    "oil": ("fuel_depot", SEVERITY_HIGH),
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

# Keys that mean the thing is not there any more. **Bare keys only**, and that
# is the whole of the rule: OSM's lifecycle *prefixes* (`disused:`,
# `abandoned:`, `was:`) apply to the tag they prefix, and an element that
# reached this function carries the bare candidate tag -- `man_made=works`,
# not `disused:man_made=works` -- so a prefixed key on it is about something
# else entirely. `disused:railway=rail` on a live steelworks is a siding, and
# `was:name=Ensidesa` on `Acería de Veriña - ArcelorMittal` is the plant's own
# renaming history through Ensidesa -> Aceralia -> Arcelor -> ArcelorMittal.
# Reading either as "gone" erased the one hazard this feature was written to
# catch (review, 2026-08-20).
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


def _is_disused(tags: Dict[str, Any]) -> bool:
    for key, value in tags.items():
        name = str(key).strip().casefold()
        if name in _DISUSED_KEYS and fold(value) not in _NEGATIVE_VALUES:
            return True
        if name == "historic" and fold(value) in _HISTORIC_DISUSED_VALUES:
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

    by_name = _name_verdict(tags)
    by_tag = _tag_verdict(tags)
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
    return (
        by_tag
        if severity_rank(by_tag.severity) < severity_rank(by_name.severity)
        else by_name
    )


def _name_verdict(tags: Dict[str, Any]) -> Optional[HazardVerdict]:
    tokens = _name_tokens(fold(tags.get("name")))
    if not tokens:
        return None
    for needle, kind, severity in _NAME_EVIDENCE:
        if _name_says(tokens, needle):
            return HazardVerdict(
                kind=kind, severity=severity, evidence=f"name:{needle.strip()}"
            )
    return None


def _tag_verdict(tags: Dict[str, Any]) -> Optional[HazardVerdict]:
    industrial = fold(tags.get("industrial"))
    if industrial in _INDUSTRIAL_EVIDENCE:
        kind, severity = _INDUSTRIAL_EVIDENCE[industrial]
        return HazardVerdict(
            kind=kind, severity=severity, evidence=f"industrial={industrial}"
        )

    if fold(tags.get("power")) == "plant":
        source = fold(tags.get("plant:source"))
        method = fold(tags.get("plant:method"))
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


def merge_keys(keys: Iterable[str]) -> Dict[str, str]:
    """`{key: canonical key}`, folding a facility name into its operator.

    OSM does not tag one facility consistently. At property 793 the steelworks
    is `landuse=industrial` named *Acería de Veriña - ArcelorMittal* with **no**
    operator, its tip is named *Vertedero ArcelorMittal*, and its two turbines
    and two stacks carry `operator=ArcelorMittal` and nothing else. Grouping by
    the key alone would report that one plant four times.

    So a key whose tokens *contain* another key's tokens contiguously is folded
    into the shorter one -- `arcelormittal` absorbs both names above, and
    `exolum` absorbs `exolum - musel i`, `- ii` and `- iii`. The guards are
    what keep that from becoming a wildcard: the absorbing key must be at least
    `_MIN_CONTAINMENT_KEY_CHARS` long, and the match is over whole tokens in
    order, so `mar` never absorbs `marisma` and `de` absorbs nothing at all.
    Anything not absorbed stays its own facility, which is the safe direction:
    two rows for one plant over-reports, one row for two plants hides one.
    """
    unique = sorted({key for key in keys if key}, key=lambda k: (len(k), k))
    canonical: Dict[str, str] = {key: key for key in unique}
    token_cache = {key: _tokens(key) for key in unique}
    for index, longer in enumerate(unique):
        for shorter in unique[:index]:
            if len(shorter) < _MIN_CONTAINMENT_KEY_CHARS:
                continue
            if _contains(token_cache[longer], token_cache[shorter]):
                canonical[longer] = canonical[shorter]
                break
    return canonical
