from datetime import datetime, timezone
import json

from app import db
from services import owner_review
from services.advertiser import read_verdict as advertiser_verdict
from services.dossier import read_dossier
from services.listing_verification import read_verdict as listing_verdict
from services.search_subscription_identity import SEARCH_KEY_LENGTH
from sqlalchemy import CheckConstraint, func
from sqlalchemy.types import JSON


def utcnow():
    return datetime.now(timezone.utc)


def investment_rating_badge_class(rating):
    """Bootstrap badge class for an AI investment rating.

    Shared by `Property` and `Land`: both render the same "Inv. Metr." column
    on the one listing page, and two copies of this mapping would eventually
    colour the same rating differently depending on which table a row is in.
    """
    text = (rating or "").upper()
    if not text:
        return None
    if "EXCELLENT" in text or text == "HIGH":
        return "bg-success"
    if "GOOD" in text:
        return "bg-primary"
    if "MODERATE" in text or "MEDIUM" in text:
        return "bg-warning text-dark"
    if "BELOW" in text or "POOR" in text or "LOW" in text:
        return "bg-danger"
    return "bg-secondary"


class SearchProfile(db.Model):
    """Represents a saved search / client profile.

    Each profile can have its own classification rules, travel targets, and UI/scoring config.
    """

    __tablename__ = "search_profiles"
    __table_args__ = (
        db.Index("ix_search_profiles_name", "name"),
        db.Index("ix_search_profiles_is_active", "is_active"),
        db.Index("ix_search_profiles_is_default", "is_default"),
        db.Index(
            "ux_search_profiles_source_search_key", "source_search_key", unique=True
        ),
        # Dropping the UNIQUE on `name` (migration 013) also removed what used
        # to protect the check-then-insert in get_or_create_profile_by_name()
        # and get_default_profile() from two overlapping ingestions. Identified
        # subscriptions may share a label; unidentified ones may not, which
        # restores exactly the old invariant without blocking the new case.
        db.Index(
            "ux_search_profiles_name_without_key",
            "name",
            unique=True,
            postgresql_where=db.text("source_search_key IS NULL"),
            sqlite_where=db.text("source_search_key IS NULL"),
        ),
        # The catch-all must not be anybody's saved search. It receives every
        # email that carries no search URL and matches no rule, so a default
        # profile holding a search key would quietly make one subscription the
        # recipient of all unrouted mail.
        #
        # Enforced here rather than in each reader on purpose: `is_default` is
        # written from the profile editor, the create form, the merge and
        # `get_default_profile()`, and five separate read-side filters were
        # needed before this constraint existed. A route written next year
        # cannot get around it.
        CheckConstraint(
            "source_search_key IS NULL OR is_default IS NOT TRUE",
            name="ck_search_profiles_default_has_no_search_key",
        ),
        # The catch-all cannot be hidden either (migration 028, #533): it
        # receives every email that matches nothing else, so a hidden
        # catch-all takes listings off the page as they arrive.
        # `set_profile_hidden` and `edit_profile` refuse it first; this is
        # what refuses it for the writers that never reach a route --
        # curation SQL through `docker exec` is a supported workflow. The
        # hiding half of 025's `ck_search_profiles_catch_all_never_routes`,
        # on the same shape: `IS NOT TRUE` on both sides, because a NULL
        # `is_default` is nobody's catch-all.
        CheckConstraint(
            "is_hidden IS NOT TRUE OR is_default IS NOT TRUE",
            name="ck_search_profiles_catch_all_never_hidden",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    # Deliberately NOT unique (migration 013 drops the constraint): two saved
    # searches may carry the same human label with a different `shape`, and
    # before #102 that was impossible to represent.
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    is_default = db.Column(db.Boolean, default=False)
    # Taken off the screen by the owner (2026-08-17), which is not the same
    # question as `is_active`. An inactive subscription is *archived*: it is
    # still offered, under `Archive`, because a saved search that stopped
    # still holds listings worth reaching. A hidden one is not offered at all
    # -- not as a chip, not in the menu, not in the archive -- and its
    # listings are out of `profile_id=all` too, so the page it is hidden from
    # does not show its rows either.
    #
    # It says nothing about ingestion: a hidden subscription keeps receiving
    # its own alert emails, keeps its `email_matchers`, and keeps every
    # listing it already holds. See services/search_profile_service.py for
    # the one clause every surface reads. Never TRUE on the catch-all: the
    # CHECK in `__table_args__` refuses the pair from either side.
    is_hidden = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.text("FALSE")
    )

    # Saved-search identity (#102). The key is the fingerprint of the search
    # URL the alert email carries -- see services/search_subscription_identity
    # -- and is what actually identifies a subscription; the name is a label.
    # NULL until an email for this subscription arrives: nothing is
    # backfilled, because no stored row records which search it came from.
    source_search_key = db.Column(db.String(SEARCH_KEY_LENGTH))
    # Diagnostics only: the last search URL seen for this profile. Never
    # unique -- cosmetic variants of one link differ here but share the key.
    source_search_url = db.Column(db.Text)
    # Machine-readable "the ingester invented this label", so relabelling can
    # never touch a profile the owner named. Existing rows stay False: the
    # only evidence for them is a description string, which is exactly the
    # signal #102 refuses to trust.
    is_auto_created = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.text("FALSE")
    )

    # Email routing for ingestion: list of regex patterns (configurable).
    email_matchers = db.Column(JSON)  # [{"pattern": "...", "priority": 10}, ...]

    # This subscription's listings live on ANOTHER subscription (migration
    # 025). The stub keeps its #102 saved-search identity; its rows land on
    # the target — enforced by a BEFORE trigger on `properties` in
    # PostgreSQL, so no writer (ORM, curation SQL, COPY) can land a row on a
    # routed stub. Exactly one hop; `route_profile()` in
    # services/search_profile_service.py is the one writer and refuses
    # chains in both directions, self-routes and the catch-all. On SQLite
    # (the test suite) only the Python boundary applies; the PostgreSQL
    # trigger is pinned by tests/test_postgres_migrations.py.
    routed_to = db.Column(db.Integer, db.ForeignKey("search_profiles.id"))
    # On the TARGET: a profile auto-created by ingestion whose name matches
    # this regex is born routed here and hidden — what keeps each of the six
    # Galicia alerts from putting a chip back on screen at its first email.
    auto_route_from_pattern = db.Column(db.String(120))
    # The owner's app-side requirements the portals cannot encode, e.g.
    # {"min_house_m2": 150, "min_plot_m2": 700}. Read by
    # services/subscription_criteria.py; NULL means no criteria.
    criteria = db.Column(JSON)

    # Per-profile configuration (all optional; fall back to global defaults).
    classification_rules = db.Column(
        JSON
    )  # [{"category": "...", "subtype": "...", "pattern": "...", "priority": 50}, ...]
    travel_targets = db.Column(
        JSON
    )  # list of targets (schema in SettingsService/SearchProfileService)
    ui_config = db.Column(JSON)  # which columns/sections to show
    scoring_config = db.Column(JSON)  # future: per-profile scoring weights, etc.
    ai_config = db.Column(
        JSON
    )  # optional AI prompt/context overrides (e.g., market_context)

    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def __repr__(self):
        return f"<SearchProfile {self.id} {self.name}>"


class Property(db.Model):
    """Universal property model (venta-first, Spain-wide).

    Introduced alongside the legacy Land model to enable an incremental migration.
    """

    __tablename__ = "properties"
    __table_args__ = (
        db.Index("ix_properties_idealista_property_id", "idealista_property_id"),
        db.Index("ix_properties_search_profile_id", "search_profile_id"),
        db.Index("ix_properties_property_category", "property_category"),
        db.Index("ix_properties_property_subtype", "property_subtype"),
        db.Index("ix_properties_municipality", "municipality"),
        db.Index("ix_properties_listing_status", "listing_status"),
        db.Index("ix_properties_is_favorite", "is_favorite"),
        db.Index("ix_properties_created_at", "created_at"),
        db.Index("ix_properties_score_total", "score_total"),
        db.Index("ix_properties_score_investment", "score_investment"),
        db.Index("ix_properties_score_lifestyle", "score_lifestyle"),
        db.Index("ix_properties_owner_verdict", "owner_verdict"),
        db.Index("ix_properties_next_action_due_on", "next_action_due_on"),
        db.Index("ix_properties_cadastral_reference", "cadastral_reference"),
        # Data integrity constraints
        CheckConstraint(
            "price IS NULL OR price >= 0", name="ck_properties_price_non_negative"
        ),
        CheckConstraint(
            "area IS NULL OR area >= 0", name="ck_properties_area_non_negative"
        ),
        CheckConstraint(
            "location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)",
            name="ck_properties_lat_range",
        ),
        CheckConstraint(
            "location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180)",
            name="ck_properties_lon_range",
        ),
        CheckConstraint(
            "score_total IS NULL OR (score_total >= 0 AND score_total <= 100)",
            name="ck_properties_score_total_range",
        ),
        CheckConstraint(
            "score_investment IS NULL OR (score_investment >= 0 AND score_investment <= 100)",
            name="ck_properties_score_investment_range",
        ),
        CheckConstraint(
            "score_lifestyle IS NULL OR (score_lifestyle >= 0 AND score_lifestyle <= 100)",
            name="ck_properties_score_lifestyle_range",
        ),
        CheckConstraint(
            "listing_status IN ('active', 'removed', 'sold', 'unknown')",
            name="ck_properties_listing_status_enum",
        ),
        # The two review CHECKs -- `ck_properties_owner_verdict_enum` and
        # `ck_properties_due_needs_action` -- live in migration 021 and
        # deliberately NOT here, which is migration 015's precedent for the
        # same situation. `tests/test_deployment_bootstrap.py` rebuilds the
        # pre-ledger schema by running `create_all()` and dropping what later
        # migrations added, and SQLite refuses to drop a column any CHECK
        # mentions -- so declaring them on the model makes that baseline
        # impossible to construct. They are exercised where they matter, on a
        # real server, by `tests/test_postgres_migrations.py`; the reading in
        # `services/owner_review.py` refuses an unknown verdict on every
        # engine, towards `undecided` rather than towards a decision nobody
        # made.
    )

    id = db.Column(db.Integer, primary_key=True)
    source_email_id = db.Column(db.String(255), unique=True, nullable=False)
    # Stable Idealista listing id extracted from URL (/inmueble/<id>/). Used for dedup/updates.
    idealista_property_id = db.Column(db.BigInteger, nullable=True)
    email_subject = db.Column(db.Text)
    email_sender = db.Column(db.String(255))

    search_profile_id = db.Column(
        db.Integer,
        db.ForeignKey("search_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    search_profile = db.relationship(
        "SearchProfile", backref=db.backref("properties", lazy="dynamic")
    )

    title = db.Column(db.Text)
    url = db.Column(db.Text)

    # Deal & type classification (config-driven; no hard constraints here by design).
    deal_type = db.Column(
        db.String(16), default="sale"
    )  # 'sale' | 'rent' (rent not used initially)
    property_category = db.Column(
        db.String(32)
    )  # e.g. 'housing', 'land', 'garage', 'commercial', 'building'
    property_subtype = db.Column(
        db.String(64)
    )  # e.g. 'apartment', 'house', 'penthouse', 'plot', ...

    price = db.Column(db.Numeric(10, 2))
    currency = db.Column(db.String(3), default="EUR")
    area = db.Column(db.Numeric(10, 2))
    area_type = db.Column(
        db.String(16), default="unknown"
    )  # 'built' | 'plot' | 'unknown'

    municipality = db.Column(db.String(255))
    location_lat = db.Column(db.Numeric(10, 7))
    location_lon = db.Column(db.Numeric(10, 7))
    location_accuracy = db.Column(
        db.String(20), default="unknown"
    )  # 'precise', 'approximate', 'unknown'

    description = db.Column(db.Text)

    # Flexible JSON fields for extracted + enriched data.
    attributes = db.Column(JSON)  # rooms, bathrooms, floor, etc.
    property_details = db.Column(
        JSON
    )  # Raw/detail blocks (kept for compatibility patterns)
    enrichment = db.Column(JSON)  # Google/OSM derived info
    travel = db.Column(JSON)  # Dynamic travel times/distances per configured targets
    scoring = db.Column(JSON)  # Full scoring breakdown (per profile)
    ai_analysis = db.Column(JSON)
    enhanced_description = db.Column(JSON)

    score_total = db.Column(db.Numeric(5, 2))
    score_investment = db.Column(db.Numeric(5, 2))
    score_lifestyle = db.Column(db.Numeric(5, 2))

    # Price history (generic)
    previous_price = db.Column(db.Numeric(10, 2))
    price_change_amount = db.Column(db.Numeric(10, 2))
    price_change_percentage = db.Column(db.Numeric(5, 2))
    price_changed_date = db.Column(db.DateTime)

    is_favorite = db.Column(db.Boolean, default=False)

    listing_status = db.Column(
        db.String(20), default="active"
    )  # 'active', 'removed', 'sold', 'unknown'
    listing_removed_date = db.Column(db.DateTime)
    listing_last_checked = db.Column(db.DateTime)
    # How the status above was decided: 'ingest' (the default a new row carries,
    # never verified), 'email' (idealista's own removal notice), 'check' (the
    # scraper read the listing page) or 'manual' (the owner). NULL on rows that
    # predate the column -- nothing was backfilled, because nothing recorded it.
    listing_status_source = db.Column(db.String(16), default="ingest")

    # What the OWNER concluded, which is a different question from whether the
    # advert is still live -- writing one into `listing_status` is STATUS-002
    # again (issue #430). NULL is not a fourth value: it means nobody has
    # decided, and `services/owner_review.py` presents it as `undecided`,
    # never as a rejection.
    owner_verdict = db.Column(db.String(16))
    owner_verdict_reason = db.Column(db.Text)
    owner_verdict_at = db.Column(db.DateTime)
    # What is still outstanding on this listing, and when it is due. Legal
    # under any verdict, because "interested; call the architect on Friday" is
    # an ordinary state. `overdue` is derived from the date and never stored.
    next_action = db.Column(db.Text)
    next_action_due_on = db.Column(db.Date)

    # The parcel this listing sits on, as the cadastre names it (issue #430).
    # Typed by a human off a document, so it is a column rather than a key
    # inside `enrichment`: it is what every later check keys on, and it is
    # looked up. The measurement it unlocks lives in `enrichment["cadastre"]`,
    # written under the same lock (services/cadastre_service.py).
    cadastral_reference = db.Column(db.String(20))

    # How well this listing matches the owner's taste, scored by AI against
    # the profile distilled from their own review comments (issue #498).
    # NUMERIC like the three score columns above, because the list sorts on
    # it; NULL means nobody scored the row — a bridge refusal writes nothing
    # (services/taste_service.py), so the row stays in the backfill's scope.
    # `taste` is the evidence beside the number: reasons, matched traits, the
    # profile_version it was scored against, and a fingerprint of the facts.
    taste_score = db.Column(db.Numeric(5, 2))
    taste = db.Column(JSON)

    # The parcel's surface in m², where the source portal states it
    # (migration 025). A real column because the criteria verdict filters on
    # it in SQL. NULL means nobody measured it (#98) — fotocasa's payload
    # carries it (`surfaceLand`/`groundSurface`, 0-as-blank convention),
    # yaencontre and idealista mostly cannot answer from this machine.
    # Distinct from `area`, which is the BUILT surface for habitable
    # listings and the plot only for bare land (`area_type` says which).
    plot_area = db.Column(db.Numeric(10, 2))

    created_at = db.Column(db.DateTime, default=utcnow)
    email_date = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        title = (self.title or "").strip()
        snippet = (title[:50] + "...") if len(title) > 50 else title
        return f"<Property {self.id}: {snippet}>"

    def to_dict(self, review_today=None):
        """Serialize the row. `review_today` is the request's one Madrid date.

        A collection endpoint MUST pass it: `overdue` is the comparison of a
        due date against today, and a filter that selected rows against one
        date while the payload describes them against another disagrees with
        itself once a day, at the hour nobody is watching. `None` means
        "compute it", which is right for a single-row caller and wrong for a
        list -- `tests/test_owner_review_propagation.py` freezes the clock at
        23:59 Madrid and asserts the filter and both serializers agree.
        """
        review_action = owner_review.read_action(self, review_today)
        return {
            "id": self.id,
            "source_email_id": self.source_email_id,
            "idealista_property_id": self.idealista_property_id,
            "email_subject": self.email_subject,
            "email_sender": self.email_sender,
            "search_profile_id": self.search_profile_id,
            "title": self.title,
            "url": self.url,
            "deal_type": self.deal_type,
            "property_category": self.property_category,
            "property_subtype": self.property_subtype,
            "price": float(self.price) if self.price else None,
            "currency": self.currency,
            "area": float(self.area) if self.area else None,
            "area_type": self.area_type,
            "municipality": self.municipality,
            "location_lat": float(self.location_lat) if self.location_lat else None,
            "location_lon": float(self.location_lon) if self.location_lon else None,
            "location_accuracy": self.location_accuracy,
            "description": self.description,
            "attributes": self.attributes or {},
            "property_details": self.property_details or {},
            "enrichment": self.enrichment or {},
            "travel": self.travel or {},
            "scoring": self.scoring or {},
            "ai_analysis": self.ai_analysis or {},
            "enhanced_description": self.enhanced_description or {},
            "score_total": float(self.score_total) if self.score_total else None,
            "score_investment": float(self.score_investment)
            if self.score_investment
            else None,
            "score_lifestyle": float(self.score_lifestyle)
            if self.score_lifestyle
            else None,
            # `is not None`, not truthiness: a taste score of 0 is a measured
            # answer ("nothing the owner values"), not an absence.
            "taste_score": float(self.taste_score)
            if self.taste_score is not None
            else None,
            "plot_area": float(self.plot_area) if self.plot_area is not None else None,
            "taste": self.taste or {},
            "previous_price": float(self.previous_price)
            if self.previous_price
            else None,
            "price_change_amount": float(self.price_change_amount)
            if self.price_change_amount
            else None,
            "price_change_percentage": float(self.price_change_percentage)
            if self.price_change_percentage
            else None,
            "price_changed_date": self.price_changed_date.isoformat()
            if self.price_changed_date
            else None,
            "is_favorite": bool(self.is_favorite),
            "listing_status": self.listing_status or "active",
            "listing_removed_date": self.listing_removed_date.isoformat()
            if self.listing_removed_date
            else None,
            "listing_last_checked": self.listing_last_checked.isoformat()
            if self.listing_last_checked
            else None,
            "listing_status_source": self.listing_status_source,
            # The raw column above is 'active' by default and nobody verified
            # that default, so a consumer reading it alone cannot tell a live
            # listing from a never-checked one. This is the same verdict the
            # pages render: 'active' only when a check or the owner established
            # it, 'unchecked' otherwise (services/listing_verification.py).
            "listing_status_verdict": listing_verdict(self)["state"],
            # Who is selling. Most of the answer is derived from the listing
            # URL rather than stored, so a consumer reading `enrichment` alone
            # would see nothing for the 408 rows that answer for free
            # (services/advertiser.py). 'unchecked' where nobody established
            # it -- never 'agency' by default.
            "advertiser_verdict": advertiser_verdict(self)["state"],
            # The dossier written about this listing, if there is one. A
            # pointer, not a measurement -- `None` means nobody wrote one, and
            # the URL is the one the page would render, validated by the same
            # function (services/dossier.py).
            "dossier": read_dossier(self),
            # What the owner decided, and what is still outstanding. Absent is
            # `undecided`, never `rejected`: a report built off the raw column
            # alone cannot tell "nobody looked" from "looked and said no"
            # (services/owner_review.py).
            "owner_verdict": owner_review.read_decision(self)["state"],
            "owner_verdict_reason": self.owner_verdict_reason,
            "owner_verdict_at": self.owner_verdict_at.isoformat()
            if self.owner_verdict_at
            else None,
            "next_action": self.next_action,
            "next_action_due_on": self.next_action_due_on.isoformat()
            if self.next_action_due_on
            else None,
            "next_action_state": review_action["state"],
            "cadastral_reference": self.cadastral_reference,
            "email_date": self.email_date.isoformat() if self.email_date else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def _get_enrichment_dict(self):
        return self.enrichment if isinstance(self.enrichment, dict) else {}

    def _get_legacy_land(self):
        enrichment = self._get_enrichment_dict()
        legacy = enrichment.get("legacy_land")
        return legacy if isinstance(legacy, dict) else {}

    def _get_enrichment_section(self, key):
        enrichment = self._get_enrichment_dict()
        section = enrichment.get(key)
        if isinstance(section, dict):
            return section
        legacy = self._get_legacy_land()
        section = legacy.get(key)
        return section if isinstance(section, dict) else {}

    def _get_enrichment_value(self, key):
        enrichment = self._get_enrichment_dict()
        if key in enrichment and enrichment.get(key) is not None:
            return enrichment.get(key)
        legacy = self._get_legacy_land()
        if key in legacy and legacy.get(key) is not None:
            return legacy.get(key)
        return None

    def _get_travel_targets(self):
        travel = self.travel if isinstance(self.travel, dict) else {}
        targets = travel.get("targets")
        return targets if isinstance(targets, dict) else {}

    def _travel_target_duration(self, key):
        target = self._get_travel_targets().get(key)
        if not isinstance(target, dict):
            return None
        duration_min = target.get("duration_min")
        if duration_min is not None:
            return duration_min
        duration_s = target.get("duration_s")
        if duration_s is None:
            return None
        return int(round(duration_s / 60.0))

    def _travel_target_distance_km(self, key):
        target = self._get_travel_targets().get(key)
        if not isinstance(target, dict):
            return None
        distance_km = target.get("distance_km")
        if distance_km is None:
            distance_m = target.get("distance_m")
            if distance_m is None:
                return None
            distance_km = float(distance_m) / 1000.0
        return round(float(distance_km), 1)

    @property
    def geocoded_address(self):
        """The address the geocoder resolved, or `None` if it never resolved one.

        A listing carries no street of its own -- the columns stop at
        `municipality` -- so `enrichment.geocoding.formatted_address` is the
        only human-readable location this app ever holds. It was written by
        the enrichment pass and then read by nothing, which is why the page
        could show a coordinate and no address at all. Absent means the
        geocoder did not run or refused; nothing is reconstructed from the
        coordinate to fill the gap.
        """
        address = self._get_enrichment_section("geocoding").get("formatted_address")
        if not isinstance(address, str):
            return None
        return address.strip() or None

    def _measured_duration(self, target_key: str, legacy_key: str):
        """Minutes to a target, preferring the source that can be re-measured.

        `travel["targets"]` is rewritten by every enrichment run;
        `enrichment.legacy_land` is a frozen snapshot from the old `Land`
        model that no recalculation touches. Reading legacy first made the
        Transport card contradict Travel Times on all 168 mirrored rows --
        41 minutes to Asturias Airport in one card, 35 to something unnamed
        in the other. A target present in `travel` is authoritative even when
        its value is `None`: "we looked and found nothing" outranks a
        measurement nobody can reproduce.
        """
        if target_key in self._get_travel_targets():
            return self._travel_target_duration(target_key)
        return self._get_enrichment_value(legacy_key)

    def _measured_distance_km(self, target_key: str, legacy_key: str):
        """Kilometres to a target, with the same precedence as the duration."""
        if target_key in self._get_travel_targets():
            return self._travel_target_distance_km(target_key)
        return self._get_enrichment_value(legacy_key)

    @property
    def infrastructure_basic(self):
        return self._get_enrichment_section("infrastructure_basic")

    @property
    def infrastructure_extended(self):
        return self._get_enrichment_section("infrastructure_extended")

    @property
    def transport(self):
        return self._get_enrichment_section("transport")

    @property
    def environment(self):
        return self._get_enrichment_section("environment")

    @property
    def services_quality(self):
        return self._get_enrichment_section("services_quality")

    @property
    def travel_time_airport(self):
        return self._measured_duration("airport", "travel_time_airport")

    @property
    def travel_time_train_station(self):
        return self._measured_duration("train_station", "travel_time_train_station")

    @property
    def travel_time_hospital(self):
        return self._measured_duration("hospital", "travel_time_hospital")

    @property
    def travel_time_police(self):
        return self._measured_duration("police", "travel_time_police")

    @property
    def distance_airport(self):
        return self._measured_distance_km("airport", "distance_airport")

    @property
    def distance_train_station(self):
        return self._measured_distance_km("train_station", "distance_train_station")

    @property
    def distance_hospital(self):
        return self._measured_distance_km("hospital", "distance_hospital")

    @property
    def distance_police(self):
        return self._measured_distance_km("police", "distance_police")

    def _ai_analysis_dict(self):
        """Return ai_analysis as a dict regardless of storage type."""
        data = self.ai_analysis
        if not data:
            return {}
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _humanize_rating_text(value):
        text = str(value or "").strip()
        if not text:
            return None

        text = text.replace("_", " ")
        text = " ".join(text.split())

        head, sep, tail = text.partition("-")
        head = head.strip()
        tail = tail.strip() if sep else ""

        if head and head.upper() == head:
            head = head.title()

        if not sep:
            return head

        if not tail:
            return head

        return f"{head} - {tail}"

    @property
    def investment_metrics_rating_full(self):
        """Full investment rating text from structured rental market analysis (if present)."""
        analysis = self._ai_analysis_dict()
        rental = (
            analysis.get("rental_market_analysis")
            if isinstance(analysis, dict)
            else None
        )
        if not isinstance(rental, dict):
            return None
        rating = rental.get("investment_rating")
        if not rating:
            return None
        return self._humanize_rating_text(rating)

    @property
    def investment_metrics_rating(self):
        """Short investment rating label (e.g., GOOD/MODERATE/EXCELLENT)."""
        full = self.investment_metrics_rating_full
        if not full:
            return None
        short = full.split("-", 1)[0].strip()
        return short if short else None

    @property
    def investment_metrics_badge_class(self):
        """Bootstrap badge class for the investment rating."""
        return investment_rating_badge_class(
            self.investment_metrics_rating_full or self.investment_metrics_rating
        )


class Land(db.Model):
    __tablename__ = "lands"
    __table_args__ = (
        db.Index("ix_lands_idealista_property_id", "idealista_property_id"),
        db.Index("ix_lands_land_type", "land_type"),
        db.Index("ix_lands_municipality", "municipality"),
        db.Index("ix_lands_listing_status", "listing_status"),
        db.Index("ix_lands_is_favorite", "is_favorite"),
        db.Index("ix_lands_created_at", "created_at"),
        db.Index("ix_lands_score_total", "score_total"),
        db.Index("ix_lands_score_investment", "score_investment"),
        db.Index("ix_lands_score_lifestyle", "score_lifestyle"),
        # Data integrity constraints (matching Legacy)
        CheckConstraint(
            "price IS NULL OR price >= 0", name="ck_lands_price_non_negative"
        ),
        CheckConstraint("area IS NULL OR area >= 0", name="ck_lands_area_non_negative"),
        CheckConstraint(
            "location_lat IS NULL OR (location_lat >= -90 AND location_lat <= 90)",
            name="ck_lands_lat_range",
        ),
        CheckConstraint(
            "location_lon IS NULL OR (location_lon >= -180 AND location_lon <= 180)",
            name="ck_lands_lon_range",
        ),
        CheckConstraint(
            "score_total IS NULL OR (score_total >= 0 AND score_total <= 100)",
            name="ck_lands_score_total_range",
        ),
        CheckConstraint(
            "score_investment IS NULL OR (score_investment >= 0 AND score_investment <= 100)",
            name="ck_lands_score_investment_range",
        ),
        CheckConstraint(
            "score_lifestyle IS NULL OR (score_lifestyle >= 0 AND score_lifestyle <= 100)",
            name="ck_lands_score_lifestyle_range",
        ),
        CheckConstraint(
            "travel_time_oviedo IS NULL OR travel_time_oviedo >= 0",
            name="ck_lands_tt_oviedo",
        ),
        CheckConstraint(
            "travel_time_gijon IS NULL OR travel_time_gijon >= 0",
            name="ck_lands_tt_gijon",
        ),
        CheckConstraint(
            "travel_time_nearest_beach IS NULL OR travel_time_nearest_beach >= 0",
            name="ck_lands_tt_beach",
        ),
        CheckConstraint(
            "travel_time_airport IS NULL OR travel_time_airport >= 0",
            name="ck_lands_tt_airport",
        ),
        CheckConstraint(
            "travel_time_train_station IS NULL OR travel_time_train_station >= 0",
            name="ck_lands_tt_train",
        ),
        CheckConstraint(
            "travel_time_hospital IS NULL OR travel_time_hospital >= 0",
            name="ck_lands_tt_hospital",
        ),
        CheckConstraint(
            "travel_time_police IS NULL OR travel_time_police >= 0",
            name="ck_lands_tt_police",
        ),
        CheckConstraint(
            "listing_status IN ('active', 'removed', 'sold', 'unknown')",
            name="ck_lands_listing_status_enum",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    source_email_id = db.Column(db.String(255), unique=True, nullable=False)
    # Stable Idealista listing id extracted from URL (/inmueble/<id>/). Used for dedup/updates.
    idealista_property_id = db.Column(db.BigInteger, nullable=True)
    email_subject = db.Column(db.Text)  # Original email subject line
    email_sender = db.Column(db.String(255))  # Email sender
    title = db.Column(db.Text)
    url = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2))
    area = db.Column(db.Numeric(10, 2))
    municipality = db.Column(db.String(255))
    location_lat = db.Column(db.Numeric(10, 7))
    location_lon = db.Column(db.Numeric(10, 7))
    location_accuracy = db.Column(
        db.String(20), default="unknown"
    )  # 'precise', 'approximate', 'unknown'
    land_type = db.Column(
        db.String(20), CheckConstraint("land_type IN ('developed', 'buildable')")
    )
    description = db.Column(db.Text)

    # JSON fields for complex data (works with both PostgreSQL and SQLite)
    infrastructure_basic = db.Column(JSON)  # electricity, water, internet, gas
    infrastructure_extended = db.Column(
        JSON
    )  # supermarket, school, restaurants, hospital
    transport = db.Column(JSON)  # train, airport, highway, bus
    environment = db.Column(JSON)  # sea_view, mountain_view, forest, orientation
    neighborhood = db.Column(JSON)  # new_houses, area_price_level, noise
    services_quality = db.Column(
        JSON
    )  # schools rating, restaurants rating, cafes rating

    legal_status = db.Column(db.String(50))
    property_details = db.Column(
        JSON
    )  # AI analysis and property details in JSON format
    ai_analysis = db.Column(JSON)  # Structured AI analysis with 5 blocks
    enhanced_description = db.Column(JSON)  # AI-enhanced professional description data
    # How the last travel run went, in the `Property.travel` shape: api_status
    # plus a per-target `google` / `estimate` / `unavailable` (#225). The
    # travel_time_* columns above hold measurements only; an estimate lives
    # here, labelled, instead of impersonating one.
    travel = db.Column(JSON)
    score_total = db.Column(db.Numeric(5, 2))
    score_investment = db.Column(db.Numeric(5, 2))  # Investment-focused score (0-100)
    score_lifestyle = db.Column(db.Numeric(5, 2))  # Lifestyle-focused score (0-100)

    # Travel times by car (in minutes)
    # Reference city A/B are configured via SettingsService.get_reference_cities().
    travel_time_oviedo = db.Column(db.Integer)  # Travel time to reference city A
    travel_time_gijon = db.Column(db.Integer)  # Travel time to reference city B
    travel_time_nearest_beach = db.Column(
        db.Integer
    )  # Time to nearest beach in minutes
    nearest_beach_name = db.Column(db.String(255))  # Name of nearest beach

    # Priority infrastructure travel times (in minutes)
    travel_time_airport = db.Column(db.Integer)  # Time to nearest airport
    travel_time_train_station = db.Column(db.Integer)  # Time to nearest train station
    travel_time_hospital = db.Column(db.Integer)  # Time to nearest hospital
    travel_time_police = db.Column(db.Integer)  # Time to nearest police station

    # Priority infrastructure distances (in kilometers)
    distance_airport = db.Column(db.Integer)  # Distance to nearest airport in km
    distance_train_station = db.Column(
        db.Integer
    )  # Distance to nearest train station in km
    distance_hospital = db.Column(db.Integer)  # Distance to nearest hospital in km
    distance_police = db.Column(db.Integer)  # Distance to nearest police station in km

    # Price history tracking
    previous_price = db.Column(db.Numeric(10, 2))  # Previous price before update
    price_change_amount = db.Column(
        db.Numeric(10, 2)
    )  # Amount of price change (negative for decrease)
    price_change_percentage = db.Column(db.Numeric(5, 2))  # Percentage change
    price_changed_date = db.Column(db.DateTime)  # When price was last changed

    # Favorites
    is_favorite = db.Column(db.Boolean, default=False)  # Mark property as favorite

    # Listing status tracking
    listing_status = db.Column(
        db.String(20), default="active"
    )  # 'active', 'removed', 'sold', 'unknown'
    listing_removed_date = db.Column(
        db.DateTime
    )  # When listing was removed from Idealista
    listing_last_checked = db.Column(
        db.DateTime
    )  # Last time we checked the listing status
    # Same four values as Property.listing_status_source, same NULL meaning.
    listing_status_source = db.Column(db.String(16), default="ingest")

    created_at = db.Column(db.DateTime, default=utcnow)
    email_date = db.Column(db.DateTime)  # Date when the email was received
    updated_at = db.Column(
        db.DateTime, default=utcnow, onupdate=utcnow
    )  # Last update time

    def __repr__(self):
        return f"<Land {self.id}: {self.title[:50]}...>"

    def to_dict(self):
        """Convert land to dictionary for API responses"""
        return {
            "id": self.id,
            "source_email_id": self.source_email_id,
            "idealista_property_id": self.idealista_property_id,
            "title": self.title,
            "url": self.url,
            "price": float(self.price) if self.price else None,
            "area": float(self.area) if self.area else None,
            "municipality": self.municipality,
            "location_lat": float(self.location_lat) if self.location_lat else None,
            "location_lon": float(self.location_lon) if self.location_lon else None,
            "location_accuracy": self.location_accuracy,
            "land_type": self.land_type,
            "description": self.description,
            "infrastructure_basic": self.infrastructure_basic or {},
            "infrastructure_extended": self.infrastructure_extended or {},
            "transport": self.transport or {},
            "environment": self.environment or {},
            "neighborhood": self.neighborhood or {},
            "services_quality": self.services_quality or {},
            "legal_status": self.legal_status,
            "score_total": float(self.score_total) if self.score_total else None,
            "score_investment": float(self.score_investment)
            if self.score_investment
            else None,
            "score_lifestyle": float(self.score_lifestyle)
            if self.score_lifestyle
            else None,
            "travel_time_oviedo": self.travel_time_oviedo,
            "travel_time_gijon": self.travel_time_gijon,
            "travel_time_nearest_beach": self.travel_time_nearest_beach,
            "nearest_beach_name": self.nearest_beach_name,
            "is_favorite": self.is_favorite or False,
            "listing_status": self.listing_status or "active",
            "listing_removed_date": self.listing_removed_date.isoformat()
            if self.listing_removed_date
            else None,
            "listing_last_checked": self.listing_last_checked.isoformat()
            if self.listing_last_checked
            else None,
            "listing_status_source": self.listing_status_source,
            # The raw column above is 'active' by default and nobody verified
            # that default, so a consumer reading it alone cannot tell a live
            # listing from a never-checked one. This is the same verdict the
            # pages render: 'active' only when a check or the owner established
            # it, 'unchecked' otherwise (services/listing_verification.py).
            "listing_status_verdict": listing_verdict(self)["state"],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def _ai_analysis_dict(self):
        """Return ai_analysis as a dict regardless of storage type."""
        data = self.ai_analysis
        if not data:
            return {}
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @property
    def investment_metrics_rating_full(self):
        """Full investment rating text from structured rental market analysis (if present)."""
        analysis = self._ai_analysis_dict()
        rental = (
            analysis.get("rental_market_analysis")
            if isinstance(analysis, dict)
            else None
        )
        if not isinstance(rental, dict):
            return None
        rating = rental.get("investment_rating")
        if not rating:
            return None
        return self._humanize_rating_text(rating)

    @property
    def investment_metrics_rating(self):
        """Short investment rating label (e.g., GOOD/MODERATE/EXCELLENT)."""
        full = self.investment_metrics_rating_full
        if not full:
            return None
        short = full.split("-", 1)[0].strip()
        return short if short else None

    @staticmethod
    def _humanize_rating_text(value):
        text = str(value or "").strip()
        if not text:
            return None

        text = text.replace("_", " ")
        text = " ".join(text.split())

        head, sep, tail = text.partition("-")
        head = head.strip()
        tail = tail.strip() if sep else ""

        if head and head.upper() == head:
            head = head.title()

        if not sep:
            return head

        if not tail:
            return head

        return f"{head} - {tail}"

    @property
    def investment_metrics_badge_class(self):
        """Bootstrap badge class for investment rating."""
        return investment_rating_badge_class(
            self.investment_metrics_rating_full or self.investment_metrics_rating
        )


class ScoringCriteria(db.Model):
    __tablename__ = "scoring_criteria"

    id = db.Column(db.Integer, primary_key=True)
    criteria_name = db.Column(db.String(100), nullable=False)
    profile = db.Column(
        db.String(20), nullable=True, default="combined"
    )  # 'investment', 'lifestyle', 'combined'
    weight = db.Column(db.Numeric(3, 2), default=1.0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    # Unique constraint for criteria_name + profile combination
    __table_args__ = (
        db.UniqueConstraint("criteria_name", "profile", name="uq_criteria_profile"),
    )

    def __repr__(self):
        return f"<ScoringCriteria {self.criteria_name}[{self.profile}]: {self.weight}>"


class LandHistory(db.Model):
    """Tracks changes to favorite properties over time"""

    __tablename__ = "land_history"

    id = db.Column(db.Integer, primary_key=True)
    land_id = db.Column(
        db.Integer, db.ForeignKey("lands.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_date = db.Column(db.DateTime, default=utcnow, nullable=False)

    # Tracked fields
    price = db.Column(db.Numeric(10, 2))
    title = db.Column(db.Text)
    description = db.Column(db.Text)
    area = db.Column(db.Numeric(10, 2))
    land_type = db.Column(db.String(20))
    url = db.Column(db.Text)

    # Change metadata
    change_type = db.Column(db.String(50), nullable=False)
    # Types: 'added_to_favorites', 'price_change', 'description_change', 'title_change', 'removed_from_listing', 'periodic_snapshot'

    # Price change details (for price_change type)
    price_previous = db.Column(db.Numeric(10, 2))
    price_change_amount = db.Column(db.Numeric(10, 2))
    price_change_percentage = db.Column(db.Numeric(5, 2))

    # Relationship
    land = db.relationship(
        "Land",
        backref=db.backref(
            "history", lazy="dynamic", order_by="LandHistory.snapshot_date.desc()"
        ),
    )

    def __repr__(self):
        return (
            f"<LandHistory {self.land_id} - {self.change_type} @ {self.snapshot_date}>"
        )

    def to_dict(self):
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "land_id": self.land_id,
            "snapshot_date": self.snapshot_date.isoformat()
            if self.snapshot_date
            else None,
            "price": float(self.price) if self.price else None,
            "title": self.title,
            "description": self.description,
            "area": float(self.area) if self.area else None,
            "land_type": self.land_type,
            "url": self.url,
            "change_type": self.change_type,
            "price_previous": float(self.price_previous)
            if self.price_previous
            else None,
            "price_change_amount": float(self.price_change_amount)
            if self.price_change_amount
            else None,
            "price_change_percentage": float(self.price_change_percentage)
            if self.price_change_percentage
            else None,
        }

    @classmethod
    def create_snapshot(cls, land, change_type, price_previous=None):
        """Create a new history snapshot for a land"""
        snapshot = cls(
            land_id=land.id,
            price=land.price,
            title=land.title,
            description=land.description,
            area=land.area,
            land_type=land.land_type,
            url=land.url,
            change_type=change_type,
            price_previous=price_previous,
        )

        # Calculate price change if previous price provided
        if price_previous and land.price:
            snapshot.price_change_amount = float(land.price) - float(price_previous)
            if float(price_previous) > 0:
                snapshot.price_change_percentage = (
                    snapshot.price_change_amount / float(price_previous)
                ) * 100

        return snapshot


class SyncHistory(db.Model):
    __tablename__ = "sync_history"

    id = db.Column(db.Integer, primary_key=True)
    sync_type = db.Column(db.String(20), nullable=False)  # 'full', 'incremental'
    backend = db.Column(db.String(20), nullable=False)  # 'imap', 'gmail'
    total_emails_found = db.Column(db.Integer, default=0)
    new_properties_added = db.Column(db.Integer, default=0)
    price_updated_count = db.Column(
        db.Integer, default=0
    )  # Properties with price changes
    expired_count = db.Column(
        db.Integer, default=0
    )  # Properties marked as expired/removed
    sync_duration = db.Column(db.Integer)  # Duration in seconds
    status = db.Column(
        db.String(20), default="completed"
    )  # 'completed', 'failed', 'partial'
    error_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime, default=utcnow)
    completed_at = db.Column(db.DateTime)

    def __repr__(self):
        return (
            f"<SyncHistory {self.sync_type} - {self.new_properties_added} properties>"
        )


class AppSetting(db.Model):
    __tablename__ = "app_settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(120), unique=True, nullable=False, index=True)
    value = db.Column(JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def __repr__(self):
        return f"<AppSetting {self.key}>"


class MarketSettings(db.Model):
    """Configurable market analysis parameters for AI enrichment"""

    __tablename__ = "market_settings"

    id = db.Column(db.Integer, primary_key=True)

    # Construction costs (€/m²)
    construction_basic_min = db.Column(db.Integer, default=1100)
    construction_basic_avg = db.Column(db.Integer, default=1300)
    construction_basic_max = db.Column(db.Integer, default=1500)
    construction_premium_min = db.Column(db.Integer, default=1500)
    construction_premium_avg = db.Column(db.Integer, default=1800)
    construction_premium_max = db.Column(db.Integer, default=2200)

    # Purchase costs ratio (transfer tax/VAT + notary/registry/legal); varies by region and deal.
    purchase_costs_ratio = db.Column(db.Numeric(4, 3), default=0.10)

    # Rental adjustments - Urban
    urban_vacancy_rate = db.Column(db.Numeric(4, 3), default=0.05)
    urban_operating_expenses = db.Column(db.Numeric(4, 3), default=0.15)
    urban_management_fee = db.Column(db.Numeric(4, 3), default=0.00)

    # Rental adjustments - Suburban
    suburban_vacancy_rate = db.Column(db.Numeric(4, 3), default=0.08)
    suburban_operating_expenses = db.Column(db.Numeric(4, 3), default=0.15)
    suburban_management_fee = db.Column(db.Numeric(4, 3), default=0.00)

    # Rental adjustments - Rural
    rural_vacancy_rate = db.Column(db.Numeric(4, 3), default=0.20)
    rural_operating_expenses = db.Column(db.Numeric(4, 3), default=0.18)
    rural_management_fee = db.Column(db.Numeric(4, 3), default=0.10)

    # Rental prices per m²/month (configurable)
    urban_rental_min = db.Column(db.Integer, default=9)
    urban_rental_avg = db.Column(db.Integer, default=11)
    urban_rental_max = db.Column(db.Integer, default=13)
    suburban_rental_min = db.Column(db.Integer, default=7)
    suburban_rental_avg = db.Column(db.Integer, default=9)
    suburban_rental_max = db.Column(db.Integer, default=11)
    rural_rental_min = db.Column(db.Integer, default=5)
    rural_rental_avg = db.Column(db.Integer, default=7)
    rural_rental_max = db.Column(db.Integer, default=9)

    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        return f"<MarketSettings id={self.id}>"

    def to_dict(self):
        """Convert to dictionary for API/template use"""
        return {
            "construction_costs": {
                "basic": {
                    "min": self.construction_basic_min,
                    "avg": self.construction_basic_avg,
                    "max": self.construction_basic_max,
                },
                "premium": {
                    "min": self.construction_premium_min,
                    "avg": self.construction_premium_avg,
                    "max": self.construction_premium_max,
                },
            },
            "purchase_costs_ratio": float(self.purchase_costs_ratio)
            if self.purchase_costs_ratio
            else 0.11,
            "rental_adjustments": {
                "urban": {
                    "vacancy_rate": float(self.urban_vacancy_rate)
                    if self.urban_vacancy_rate
                    else 0.05,
                    "operating_expenses": float(self.urban_operating_expenses)
                    if self.urban_operating_expenses
                    else 0.15,
                    "management_fee": float(self.urban_management_fee)
                    if self.urban_management_fee
                    else 0.00,
                },
                "suburban": {
                    "vacancy_rate": float(self.suburban_vacancy_rate)
                    if self.suburban_vacancy_rate
                    else 0.08,
                    "operating_expenses": float(self.suburban_operating_expenses)
                    if self.suburban_operating_expenses
                    else 0.15,
                    "management_fee": float(self.suburban_management_fee)
                    if self.suburban_management_fee
                    else 0.00,
                },
                "rural": {
                    "vacancy_rate": float(self.rural_vacancy_rate)
                    if self.rural_vacancy_rate
                    else 0.20,
                    "operating_expenses": float(self.rural_operating_expenses)
                    if self.rural_operating_expenses
                    else 0.18,
                    "management_fee": float(self.rural_management_fee)
                    if self.rural_management_fee
                    else 0.10,
                },
            },
            "rental_prices": {
                "urban": {
                    "min": self.urban_rental_min,
                    "avg": self.urban_rental_avg,
                    "max": self.urban_rental_max,
                },
                "suburban": {
                    "min": self.suburban_rental_min,
                    "avg": self.suburban_rental_avg,
                    "max": self.suburban_rental_max,
                },
                "rural": {
                    "min": self.rural_rental_min,
                    "avg": self.rural_rental_avg,
                    "max": self.rural_rental_max,
                },
            },
        }

    @classmethod
    def get_settings(cls):
        """Get current settings or create default if none exist"""
        settings = cls.query.first()
        if not settings:
            from app import db

            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings


class AiAnalysisVariant(db.Model):
    """Stores alternative AI analyses per land (e.g., Claude vs ChatGPT).

    At most one row per (land_id, provider) -- enforced by the database, not
    just by the writer's own logic (#190 review round 3, finding 4;
    migration 017). Before that migration this was a plain (non-unique)
    `db.Index`, and both land-side writers in routes/api_routes.py
    (`analyze_property_structured` for Claude, `generate_openai_structured`
    for OpenAI) were query-then-insert: `?sync=1` racing an interrupted
    job's async retry -- `?sync=1` bypasses `background_jobs`' dedupe_key
    entirely -- could both see "no row for this pair" and both insert,
    leaving two variants racing for the same land/provider. See
    `routes.api_routes._upsert_land_ai_variant`, the update-or-insert writer
    this constraint lets recover from a lost race instead of preventing it
    silently.
    """

    __tablename__ = "ai_analysis_variants"

    id = db.Column(db.Integer, primary_key=True)
    land_id = db.Column(
        db.Integer,
        db.ForeignKey("lands.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = db.Column(
        db.String(32), nullable=False, index=True
    )  # 'claude', 'openai'
    model = db.Column(db.String(128), nullable=True)
    analysis = db.Column(JSON, nullable=False, default=dict)
    # The price the prompt carried. NULL means "not recorded" -- every variant
    # written before #235 -- and the page compares nothing rather than
    # inventing a comparison.
    price_at_analysis = db.Column(db.Numeric(10, 2))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    land = db.relationship(
        "Land", backref=db.backref("ai_analysis_variants", lazy="dynamic")
    )

    __table_args__ = (
        db.UniqueConstraint(
            "land_id", "provider", name="ux_ai_analysis_variants_land_provider"
        ),
    )

    def __repr__(self):
        return f"<AiAnalysisVariant land={self.land_id} provider={self.provider}>"


class PropertyAiAnalysisVariant(db.Model):
    """Stores alternative AI analyses per Property (e.g., Claude vs ChatGPT).

    At most one row per (property_id, provider) -- enforced by the database,
    not just by the writer's own logic (#190 review, blocker 3; migration
    017). Before that migration the composite index here was a plain
    (non-unique) `db.Index`, and routes/api_routes.py's writer was
    query-then-insert: an interrupted job's async retry racing a `?sync=1`
    request (which bypasses background_jobs' dedupe_key entirely) could both
    see "no row for this pair" and both insert, leaving two variants racing
    for the same property/provider. See
    `routes.api_routes._upsert_property_ai_variant`, the update-or-insert
    writer this constraint lets recover from a lost race instead of just
    preventing it silently.
    """

    __tablename__ = "property_ai_analysis_variants"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(
        db.Integer,
        db.ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = db.Column(
        db.String(32), nullable=False, index=True
    )  # 'claude', 'openai'
    model = db.Column(db.String(128), nullable=True)
    analysis = db.Column(JSON, nullable=False, default=dict)
    # The price the prompt carried. NULL means "not recorded" -- every variant
    # written before #235 -- and the page compares nothing rather than
    # inventing a comparison.
    price_at_analysis = db.Column(db.Numeric(10, 2))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    property = db.relationship(
        "Property", backref=db.backref("ai_analysis_variants", lazy="dynamic")
    )

    __table_args__ = (
        db.UniqueConstraint(
            "property_id",
            "provider",
            name="ux_property_ai_analysis_variants_property_provider",
        ),
    )

    def __repr__(self):
        return f"<PropertyAiAnalysisVariant property={self.property_id} provider={self.provider}>"


class PropertyActivity(db.Model):
    """The conversation and the decisions behind one listing (issue #430).

    Three kinds of entry share this table because they share one screen: the
    timeline on `/properties/<id>` is a single reverse-chronological feed, and
    splitting notes from contacts from verdict changes would make the page
    re-interleave by date what the reader is trying to read in order.

    * `note`    -- free text the owner typed.
    * `contact` -- one exchange: `channel`, `counterpart`, what was `asked`,
                   what came back in `body`.
    * `verdict` -- appended by `services.owner_review.set_review`, carrying the
                   whole review state in `snapshot` (plus the previous one).
                   It is the history, so the UI renders it read-only.

    `happened_at` is when the exchange happened; `created_at` is when the row
    was typed. An answer given on the phone yesterday is recorded today, and a
    timeline ordered by the wrong one of those tells the story wrong.

    Deletion is soft. Everything else in this application can be recomputed --
    a drive time, a score, a sea-view verdict -- and a sentence the owner typed
    cannot, so a mis-tap must not be the end of it.

    The CHECK constraints are declared here in a form SQLite can execute too;
    migration 021 states the same rules in PostgreSQL, where the blank tests
    are `~ '[^[:space:]]'` rather than `TRIM(...) <> ''` (measured: `BTRIM`
    strips spaces but not tabs or newlines, so a note holding one newline
    passed the trim form). The two agree on everything the suite can reach; the
    stricter PostgreSQL wording is pinned by tests/test_postgres_migrations.py
    against a real server.
    """

    __tablename__ = "property_activity"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(
        db.Integer,
        db.ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind = db.Column(db.String(16), nullable=False)
    happened_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    channel = db.Column(db.String(24))
    counterpart = db.Column(db.String(160))
    asked = db.Column(db.Text)
    body = db.Column(db.Text)
    snapshot = db.Column(JSON)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at = db.Column(db.DateTime)

    property = db.relationship(
        "Property", backref=db.backref("activity", lazy="dynamic")
    )

    __table_args__ = (
        # ix_property_activity_property_id comes from `index=True` on the
        # column itself, the way PropertyAiAnalysisVariant declares its own.
        db.Index(
            "ix_property_activity_property_happened", "property_id", "happened_at"
        ),
        db.Index("ix_property_activity_kind", "kind"),
        # An attachment (PR3) points at a property and, optionally, at the
        # exchange it arrived in. This pair is what lets that table carry a
        # composite foreign key, so an attachment on one property can never
        # reference another property's exchange.
        db.UniqueConstraint(
            "id", "property_id", name="uq_property_activity_id_property"
        ),
        CheckConstraint(
            "kind IN ('note', 'contact', 'verdict')",
            name="ck_property_activity_kind",
        ),
        CheckConstraint(
            "kind <> 'verdict' OR snapshot IS NOT NULL",
            name="ck_property_activity_verdict_snapshot",
        ),
        CheckConstraint(
            "kind = 'verdict' OR snapshot IS NULL",
            name="ck_property_activity_snapshot_is_verdict",
        ),
        CheckConstraint(
            "kind = 'contact' OR "
            "(channel IS NULL AND counterpart IS NULL AND asked IS NULL)",
            name="ck_property_activity_contact_columns",
        ),
        CheckConstraint(
            "kind <> 'contact' OR ("
            "channel IS NOT NULL AND channel IN "
            "('whatsapp', 'email', 'portal', 'phone', 'visit', 'other') AND ("
            "(asked IS NOT NULL AND TRIM(asked) <> '') OR "
            "(body IS NOT NULL AND TRIM(body) <> '') OR "
            "(counterpart IS NOT NULL AND TRIM(counterpart) <> '')))",
            name="ck_property_activity_contact_content",
        ),
        CheckConstraint(
            "kind <> 'note' OR (body IS NOT NULL AND TRIM(body) <> '')",
            name="ck_property_activity_note_body",
        ),
    )

    def __repr__(self):
        return f"<PropertyActivity {self.id} property={self.property_id} {self.kind}>"


class PropertyAttachment(db.Model):
    """A document or photo attached to a listing (issue #430).

    The bytes live under `DATA_DIR`, named by their own sha256; this row is
    what is known about them. `services/attachments.py` owns the writing and
    the type policy, and its docstring explains why the bytes are not in the
    database.

    It points at a property and, optionally, at the exchange it arrived in --
    through a **composite** foreign key, so an attachment on one property can
    never reference another property's exchange. That is enforced by the
    database (migration 023) rather than by whichever writer remembers to
    check, and it is why `property_activity` carries `UNIQUE (id,
    property_id)`.

    There is deliberately no unique constraint on (property_id,
    content_sha256): the same document may be attached to two exchanges, and a
    soft-deleted row would otherwise hold the key against re-uploading the
    file it refers to. Deduplication is on disk, one file per hash; a row here
    is a link.
    """

    __tablename__ = "property_attachment"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(
        db.Integer,
        db.ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    activity_id = db.Column(db.Integer, index=True)
    content_sha256 = db.Column(db.String(64), nullable=False, index=True)
    storage_path = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255))
    # The sniffed type, never the client's claim.
    content_type = db.Column(db.String(64), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(16), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    deleted_at = db.Column(db.DateTime)

    property = db.relationship(
        "Property", backref=db.backref("attachments", lazy="dynamic")
    )

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["activity_id", "property_id"],
            ["property_activity.id", "property_activity.property_id"],
            name="fk_property_attachment_activity",
            ondelete="SET NULL",
        ),
        # All three indexes come from `index=True` on the columns themselves,
        # the way PropertyAiAnalysisVariant declares its own; repeating them
        # here is a duplicate CREATE INDEX.
        CheckConstraint(
            "kind IN ('document', 'photo')", name="ck_property_attachment_kind"
        ),
        CheckConstraint("size_bytes > 0", name="ck_property_attachment_size"),
        # The PostgreSQL form of this is a regex (migration 023); written here
        # without one so SQLite executes the same rule, the way migration 021's
        # blank checks are.
        CheckConstraint(
            "LENGTH(content_sha256) = 64", name="ck_property_attachment_sha256"
        ),
    )

    def __repr__(self):
        return f"<PropertyAttachment {self.id} property={self.property_id}>"


class BackgroundJob(db.Model):
    """Persists the state `services/background_jobs.py` used to keep only in a
    process-local dict (issue #176).

    `tools/autopilot/deploy_watcher.sh` recreates the app container on every
    new `main` -- as often as every 300 s -- so a job that happened to be
    `queued` or `running` in memory was abandoned mid-flight with no record it
    was ever attempted, and `/api/jobs/<id>` answered 404 for it. This table is
    written on enqueue, on start and on completion, so the row outlives the
    process that wrote it. `services.background_jobs.reconcile_orphaned_jobs`
    is what turns a row still `queued`/`running` at the next startup into
    `interrupted` -- the process that owned it no longer exists to finish it.

    `dedupe_key` plus the partial unique index below is the idempotency
    guard for issue #176's acceptance criterion 4: re-running an interrupted
    AI analysis must not leave two `PropertyAiAnalysisVariant` writers racing
    for the same (property, provider). The index only covers `queued`/
    `running` rows, so a terminal (`success`/`error`/`interrupted`) row never
    blocks a legitimate retry -- only a second *concurrently active* job for
    the same key does, and the database is what refuses it, not a Python
    check-then-insert that a second thread could still slip past.

    `lease_expires_at` is what decides whether an active row is still owned
    by a live process -- always written as `now() + TTL` *in SQL*, by
    `services.background_jobs`, never as a Python-computed value. A round-2
    review of #176/PR #190 rejected the first version of this table, which
    judged staleness by comparing the reading process's own clock against
    `started_at`/`created_at`: a skewed process clock could then declare a
    live job dead, and reconciliation ran unconditionally on every
    `create_app()`, so a one-shot utility script sharing the database with
    the running web app would interrupt that app's genuinely in-flight job.
    See `services/background_jobs.py`'s module docstring for the full model,
    including the round-4 review's transactional advisory lock (a Python
    `created_at` still shadowing the database's own default could let two
    enqueuers order the same race differently) and the single-transaction
    domain-write/terminal-CAS commit.
    """

    __tablename__ = "background_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'success', 'error', 'interrupted')",
            name="ck_background_jobs_status_enum",
        ),
        db.Index("ix_background_jobs_job_type", "job_type"),
        db.Index("ix_background_jobs_status", "status"),
        db.Index("ix_background_jobs_status_lease", "status", "lease_expires_at"),
        db.Index(
            "ux_background_jobs_active_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=db.text(
                "dedupe_key IS NOT NULL AND status IN ('queued', 'running')"
            ),
            sqlite_where=db.text(
                "dedupe_key IS NOT NULL AND status IN ('queued', 'running')"
            ),
        ),
    )

    # uuid.uuid4().hex, generated by services.background_jobs.enqueue_job --
    # not an autoincrement id, so the caller knows the job's id before the
    # row is ever committed.
    id = db.Column(db.String(32), primary_key=True)
    job_type = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="queued")
    # A caller-chosen key identifying "this unit of work", e.g.
    # "property_ai_analysis:355:claude". NULL for job types that never need
    # deduplication; never unique on its own, only while status is active
    # (see the partial index above).
    dedupe_key = db.Column(db.String(255), nullable=True)
    meta = db.Column(JSON, nullable=False, default=dict)
    result = db.Column(JSON, nullable=True)
    error = db.Column(db.Text, nullable=True)
    # server_default, not default=utcnow: a Python-side `default=` computes
    # its value in this process and sends it explicitly with the INSERT,
    # which would silently shadow the database's own DEFAULT NOW() (already
    # in migrations/016_create_background_jobs_table.sql) and reintroduce a
    # process clock into a decision the rest of this table deliberately
    # keeps in SQL (#190 review round 4, finding 2 -- two enqueuers whose
    # process clocks disagree, even by a little, could each believe their
    # own row is the newest one). `server_default=func.now()` compiles to
    # `now()` on PostgreSQL and `CURRENT_TIMESTAMP` on SQLite (verified),
    # matching the migration's own DDL exactly, so a row's timestamp always
    # comes from whichever database actually wrote it.
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    # Set to the *database's* now() + TTL at enqueue and renewed on every
    # status transition (services.background_jobs._write_status). Never had
    # a Python-side default of its own to begin with -- every write site in
    # services.background_jobs sets it explicitly via `_lease_expiry_expr`,
    # a SQL expression -- so there was nothing to shadow here; listed for
    # completeness alongside created_at above. A row is only ever treated as
    # dead when this has passed the database's own now() -- never compared
    # against anything Python computed.
    lease_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<BackgroundJob {self.id} type={self.job_type} status={self.status}>"


class TasteProfile(db.Model):
    """One distilled version of the owner's taste (issue #498).

    INSERT-ONLY: the primary key IS the version, assigned transactionally by
    the sequence, so two concurrent builds cannot mint the same version — the
    property a data/-file design could not have. "Current" is the greatest
    id; a failed rebuild inserts nothing and therefore changes nothing, and
    prior versions are retained by construction, which is what keeps a stored
    score's `profile_version` readable after the profile moves on.

    `source` holds the signals the profile was built from — property ids,
    verdicts, the owner's reason texts and the facts fed to the model — so a
    stored profile explains itself without re-querying rows that may since
    have changed. `signals_fingerprint` is the sha256 of that basis; an
    unchanged fingerprint means a rebuild would answer the same question.
    """

    __tablename__ = "taste_profile"

    id = db.Column(db.Integer, primary_key=True)
    built_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    provider = db.Column(db.String(16), nullable=False)
    model = db.Column(db.String(120))
    signals_fingerprint = db.Column(db.String(64), nullable=False)
    source = db.Column(JSON, nullable=False)
    profile = db.Column(JSON, nullable=False)

    def __repr__(self):
        return f"<TasteProfile v{self.id} built_at={self.built_at}>"
