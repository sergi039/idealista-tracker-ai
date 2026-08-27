import os


def _first_env(*names, default=None):
    """Return the first non-empty environment variable among names."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _compose_database_url():
    """Compose DATABASE_URL from DB_* parts when provided."""
    user = os.environ.get("DB_USER")
    password = os.environ.get("DB_PASSWORD")
    name = os.environ.get("DB_NAME")
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    if user and password and name:
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"
    return None


class Config:
    DEV_MODE = os.environ.get("DEV_MODE", "").lower() == "true"

    # Browser/session isolation (important when running legacy + universal on localhost).
    # Cookies do not isolate by port, so use a different cookie name than the legacy app.
    SESSION_COOKIE_NAME = (
        os.environ.get("SESSION_COOKIE_NAME") or "idealista_universal_session"
    )

    # Email backend selection
    EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "imap").lower()  # 'imap' or 'gmail'

    # AI transport: the owner's Claude and ChatGPT *subscriptions*, reached
    # through the host-side bridge (tools/ai_bridge.py) which shells out to the
    # authenticated Claude Code and Codex CLIs. No ANTHROPIC_API_KEY /
    # OPENAI_API_KEY anywhere: a key would bill per token instead of using the
    # subscription, so this path is deliberately the only one.
    AI_BRIDGE_URL = (
        os.environ.get("AI_BRIDGE_URL") or "http://host.docker.internal:5061"
    )
    AI_BRIDGE_TOKEN = os.environ.get("AI_BRIDGE_TOKEN")

    # The single definition of the AI analysis timeout (#206 item 3). Used as
    # the `timeout` handed to services/subscription_transport.py's
    # `complete()` by both services/property_ai_service.py and
    # services/openai_service.py, which used to each spell out `timeout=600`
    # independently. 600 s was sized for the #201 defect -- an inherited
    # "ultra" reasoning effort spawning research sub-agents, 4m50s per
    # listing -- that tools/ai_bridge.py's cold-start isolation already
    # fixed. Measured since: the first two real analyses through production
    # took 41.1 s (codex) and 19.4 s (claude), and a later run took 26.0 s.
    # 180 s keeps roughly 4-9x headroom over every measured real run while
    # cutting the worst case a stuck request holds a run slot for from ten
    # minutes to three.
    AI_ANALYSIS_TIMEOUT_SECONDS = int(
        os.environ.get("AI_ANALYSIS_TIMEOUT_SECONDS") or "180"
    )

    # tools/ai_bridge.py's own KILL_GRACE_SECONDS, read here under the same
    # name so both sides of the timeout math see the same number: the
    # bridge's LaunchAgent and this app's Docker container source the same
    # .env file. Do not hardcode a copy of "5" here -- an operator raising
    # AI_BRIDGE_KILL_GRACE on the host must raise this side's margin too, or
    # a slow kill sequence turns into a generic "bridge unreachable" instead
    # of the 504 the bridge worked to produce.
    AI_BRIDGE_KILL_GRACE_SECONDS = float(os.environ.get("AI_BRIDGE_KILL_GRACE") or "5")

    # Real slack for TCP connect/handshake, thread scheduling and JSON
    # (de)serialization on both ends. The old `timeout + 15` in
    # subscription_transport.py was exactly `3 * KILL_GRACE_SECONDS` at the
    # default grace and nothing else -- zero margin for anything but the
    # bridge's own kill sequence.
    AI_BRIDGE_REQUEST_MARGIN_SECONDS = 10.0

    # services/subscription_transport.py adds this on top of whatever
    # `timeout` a caller asks the bridge for, so the HTTP socket always
    # outlives the bridge's own worst case for that same request: up to
    # 3 * AI_BRIDGE_KILL_GRACE_SECONDS (SIGTERM wait, post-SIGKILL wait, pipe
    # drain -- see tools/ai_bridge.py's `_kill_process_group`), plus the real
    # margin above.
    AI_BRIDGE_SOCKET_MARGIN_SECONDS = (
        3 * AI_BRIDGE_KILL_GRACE_SECONDS + AI_BRIDGE_REQUEST_MARGIN_SECONDS
    )

    # Claude (via the claude CLI)
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5"

    # OpenAI (via the codex CLI). The id has to be one the codex CLI itself
    # knows: anything outside its own catalogue is dropped by the bridge and
    # the call silently runs on the CLI default instead.
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL") or "gpt-5.6-terra"

    # IMAP settings (for Gmail with App Password)
    IMAP_HOST = os.environ.get("IMAP_HOST") or "imap.gmail.com"
    IMAP_PORT = int(os.environ.get("IMAP_PORT") or "993")
    IMAP_SSL = (os.environ.get("IMAP_SSL") or "true").lower() == "true"
    IMAP_USER = os.environ.get("IMAP_USER")  # Required when using IMAP
    IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD")  # Required when using IMAP
    # Default to a dedicated Gmail label to avoid clobbering the legacy build.
    IMAP_FOLDER = os.environ.get("IMAP_FOLDER") or "IdealistaProperties"
    # Socket timeout for every IMAP connection (issue #15): a hung socket must
    # fail that run loudly instead of stalling all future ingestions.
    IMAP_TIMEOUT_SECONDS = float(os.environ.get("IMAP_TIMEOUT_SECONDS") or "30")
    IMAP_SEARCH_QUERY = os.environ.get("IMAP_SEARCH_QUERY") or "ALL"
    MAX_EMAILS_PER_RUN = int(os.environ.get("MAX_EMAILS_PER_RUN") or "200")

    # Gmail API (legacy, kept for compatibility)
    GMAIL_API_KEY = os.environ.get("GMAIL_API_KEY")
    GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
    GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
    GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
    GMAIL_LABEL = os.environ.get("GMAIL_LABEL") or "IdealistaProperties"

    # Google APIs - Required for production
    GOOGLE_MAPS_API_KEY = _first_env(
        "GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_API", "Google_api"
    )
    GOOGLE_PLACES_API_KEY = _first_env(
        "GOOGLE_PLACES_API_KEY", "GOOGLE_PLACES_API", "Google_api"
    )

    # Database - Required
    DATABASE_URL = os.environ.get("DATABASE_URL") or _compose_database_url()

    # App settings - Required
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SESSION_SECRET = os.environ.get("SESSION_SECRET")

    # Feature flags
    AUTO_START_SCHEDULER = (
        os.environ.get("AUTO_START_SCHEDULER", "true" if DEV_MODE else "false").lower()
        == "true"
    )
    # Google Places + Distance Matrix at ingestion. **Off by default since
    # 2026-08-17** (owner decision: "будем пересчитывать по запросу"), where it
    # used to default to on.
    #
    # What that default cost, measured from both databases on 2026-08-17: one
    # automatic run is 6 preset Places Nearby lookups + 1 for the beaches +
    # one Distance Matrix request of ~26 elements, about $0.36 per listing.
    # The scheduler was running on the mini *and* on the laptop against the
    # same mailbox, so every listing was paid for twice, and the laptop's half
    # landed in a throwaway dev database. On 2026-08-16 four new saved
    # searches delivered 306 listings to the laptop between 07:00 and 10:00 —
    # roughly $110 of Google credit in one morning that nobody asked for and
    # nobody read.
    #
    # Travel is the only *automatic* paid caller in this repository; every
    # other Google call is behind a button press or a CLI backfill. So turning
    # this off is what makes the automatic path free, and the per-listing
    # Enrich button plus `utils/recalc_property_travel.py` are how travel is
    # measured now — on request, for the rows the owner actually cares about.
    AUTO_TRAVEL_ENRICHMENT = (
        os.environ.get("AUTO_TRAVEL_ENRICHMENT", "false").lower() == "true"
    )
    # Geocoding at ingestion, on the other hand, stays on. It is the one paid
    # call the free pass cannot do without: `ensure_coordinates` is what puts
    # a coordinate on a new row, and with no coordinate the sea distance, the
    # sea-view verdict, the OSM amenities and the quality-of-life block all
    # record an honest "no coordinates" and the score is computed from almost
    # nothing. It is also 1.4% of the bill above — $0.005 a listing, about $1
    # a month at the ~7 listings a day this mailbox delivers, against ~$75 for
    # the travel step it used to ride in on.
    #
    # It is a separate flag rather than a branch of the one above because the
    # two now answer different questions: "may ingestion spend money on
    # routing" (no) and "may ingestion spend a cent on placing the listing at
    # all" (yes). Setting this to false makes ingestion reach no Google API
    # whatsoever.
    AUTO_GEOCODING = os.environ.get("AUTO_GEOCODING", "true").lower() == "true"
    # May this machine reach a billed Google API *at all*?
    #
    # The second of two locks, and the outer one. The inner lock is
    # `utils/google_spend`: no billed request is made outside an authorization
    # somebody opened on purpose, and the default is no authorization. That is
    # what closed the paths the two flags above never covered -- three HTTP
    # endpoints, six CLI tools and the background executor, of which
    # `POST /api/lands/enrich-all` is the one that loops the table
    # unauthenticated on a single unbounded request.
    #
    # This flag exists for a machine that must never spend whatever its code
    # does: a dev checkout, a `git worktree`, a throwaway clone. Set it false
    # *there*. It defaults to **true**, which is a decision rather than an
    # oversight -- defaulting it false would stop the deployment's own Enrich
    # button on the deploy that shipped it, which is precisely the mistake
    # already on record for `AUTO_START_SCHEDULER` ("that mistake was made here
    # and nearly stopped production ingestion on deploy"). A cost control that
    # breaks production on arrival gets switched off, and then there is no
    # control.
    GOOGLE_SPEND_ENABLED = (
        os.environ.get("GOOGLE_SPEND_ENABLED", "true").lower() == "true"
    )
    AUTO_PROPERTY_SCORING = (
        os.environ.get("AUTO_PROPERTY_SCORING", "true").lower() == "true"
    )
    # Distance to the sea is measured against OSM coastline geometry via
    # Overpass, which is free -- unlike the Google APIs above, which are
    # billed and are the reason those two flags exist. The flag exists so
    # offline runs and tests never reach for the network.
    SEA_DISTANCE_ENABLED = (
        os.environ.get("SEA_DISTANCE_ENABLED", "true").lower() == "true"
    )
    # The free pass at ingestion (issue #299): OSM amenities (#152), the
    # quality-of-life block (#275) and the sea-view verdict. What "free"
    # means here, precisely: Overpass (OpenStreetMap) for amenities,
    # supermarkets and the coastline, OpenTopoData for elevation, and the
    # local INE/CNH reference files -- no Google API, and no AI bridge,
    # because ingestion calls the sea-view step with `use_ai=False` (see
    # PropertyEnrichmentService.enrich_free_sources: the bridge is a cold CLI
    # on the owner's subscription, #201, and this loop is unattended).
    # So the default is on; like SEA_DISTANCE_ENABLED the flag exists so
    # offline runs never reach for the network, not to save money.
    FREE_ENRICHMENT_ENABLED = (
        os.environ.get("FREE_ENRICHMENT_ENABLED", "true").lower() == "true"
    )

    # How long a drive still counts as "at the beach" for the beaches block on
    # the property page. A beach further than this is not shown at all, so the
    # block disappears entirely for inland listings.
    BEACH_MAX_DRIVE_MIN = os.environ.get("BEACH_MAX_DRIVE_MIN", "20")

    # Universal build is sale-first; rentals are excluded unless explicitly enabled.
    SALE_ONLY = os.environ.get("SALE_ONLY", "true").lower() == "true"

    # Optional: skip categories entirely during ingestion (comma-separated).
    # Useful to keep legacy land tracker and universal properties tracker isolated without Gmail labels.
    EXCLUDED_PROPERTY_CATEGORIES = {
        part.strip().lower()
        for part in (os.environ.get("EXCLUDED_PROPERTY_CATEGORIES") or "").split(",")
        if part.strip()
    }

    # Scheduler settings
    SCHEDULER_TIMEZONE = "Europe/Madrid"  # CET timezone
    INGESTION_TIMES = ["07:00", "19:00"]  # 7 AM and 7 PM CET
    LISTING_STATUS_CHECK_TIME = os.environ.get("LISTING_STATUS_CHECK_TIME", "10:00")

    # A routing engine on this machine, replacing the Distance Matrix leg --
    # the last billed call an enrichment makes (~$0.13 a listing). Empty means
    # Google answers, exactly as before: this is opt-in because OSRM's car
    # profile is measurably slower than Google on motorway runs (+26% to +34%
    # over five airport measurements against the durations already stored),
    # so turning it on decides what the numbers in the table mean, not only
    # what they cost. See services/osrm_routing.py.
    OSRM_URL = os.environ.get("OSRM_URL", "").strip()

    # OSM Overpass API
    OSM_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
    # And where to go when that one will not talk to this machine. Measured
    # from the Mac mini at 00:52 on 2026-08-19, while overpass-api.de was
    # timing out on connect for this IP and answering the laptop in 0.27 s:
    # kumi.systems 200 in 3.5 s, private.coffee 200 in 2.1 s. Every public
    # instance rate-limits per IP, and the travel presets became a load-bearing
    # Overpass consumer on 2026-08-18 -- one endpoint is a single point of
    # failure for a feature that now has no paid path behind it.
    #
    # **Two fallbacks were not enough, measured 2026-08-20 at 22:30 CEST**:
    # from the mini, all three of the above were unusable *at once* --
    # overpass-api.de timed out on connect, kumi.systems answered 502,
    # private.coffee timed out -- while the laptop got overpass-api.de in
    # 0.6 s. A list whose every entry can be down together is the single point
    # of failure it was written to remove, just with more names on it. The
    # openstreetmap.fr instance answered the mini in 0.24 s throughout that
    # window and the laptop in 4.3 s, and it goes *first* among the fallbacks
    # because it is the only one that answered either machine that day. The
    # primary above still leads the walk: it is the reference instance, and
    # these are tried only after it refuses.
    #
    # It was added on evidence rather than on availability, because an
    # instance that merely answers is the more dangerous kind of failure. A
    # thin or regional mirror returns `200` with an empty `elements` list, and
    # this project would write that down as a *measured* absence -- "nothing
    # hazardous nearby" for a plot beside a cement works, which is #98's
    # defect arriving through a spare tyre. One was caught doing exactly that
    # the same afternoon (overpass.osm.ch, in another session's hand-run
    # override). So openstreetmap.fr was checked against a known answer before
    # it was written down: the hazard query for property 793's own coordinate
    # returned the **same 144 elements with the same tags** as overpass-api.de
    # and as the committed fixture in `tests/data/`, zero differences, and a
    # dense unrelated query (`shop=supermarket` over central Gijon) returned
    # the same national chains in the same counts. Do not add an instance to
    # this list on a `200` alone.
    #
    # Comma-separated in the environment to add or reorder without a deploy --
    # which is the escape hatch when the next one goes down, and is how the
    # mini was kept working while this landed.
    OSM_OVERPASS_FALLBACK_URLS = [
        url.strip()
        for url in os.environ.get(
            "OSM_OVERPASS_FALLBACK_URLS",
            "https://overpass.openstreetmap.fr/api/interpreter,"
            "https://overpass.kumi.systems/api/interpreter,"
            "https://overpass.private.coffee/api/interpreter",
        ).split(",")
        if url.strip()
    ]

    # How long one Overpass lookup may wait, and on what.
    #
    # The 3 s is #438's, measured, and moved into config here rather than
    # rewritten: pure TCP connect to the reachable instances is 0.06-0.08 s
    # (three samples each, 2026-08-20), and an independent measurement the same
    # afternoon puts the *complete TLS handshake* at 0.128-0.132 s -- so 3 s is
    # a twentyfold margin over the whole thing, not just the SYN, while
    # overpass-api.de answered `No route to host` instantly. It is separate
    # from the read allowance because `requests` expands a scalar timeout to
    # `connect=read=value`, so the 60 s an Overpass query genuinely needs to
    # *compute* was also being granted to learn that a host does not answer.
    # `tests/test_enrich_does_not_hold_a_slot.py` pins the pair as shipped.
    OSM_OVERPASS_CONNECT_TIMEOUT_S = float(
        os.environ.get("OSM_OVERPASS_CONNECT_TIMEOUT_S") or "3"
    )
    # Unchanged, and deliberately: this is Overpass's own query-computation
    # time, and shortening it would turn a slow answer into a manufactured
    # absence.
    OSM_OVERPASS_READ_TIMEOUT_S = float(
        os.environ.get("OSM_OVERPASS_READ_TIMEOUT_S") or "60"
    )
    # The ceiling on one lookup's walk across every instance above.
    #
    # Derived from what a *prompt* refusal costs, which is what a `504` is:
    #
    #   #144's patient budget on the first instance -- both per-IP slots are
    #   busy and one frees up in about a minute, so 8+16+32 s of backoff plus
    #   four 5 s gate waits, ~76 s
    # + one full attempt on each of the three fallbacks, 5 s gate + 60 s read
    # = ~271 s, rounded to 275.
    #
    # It was 210 for two fallbacks. A third instance was added on 2026-08-20
    # and this number moves with the count -- deliberately, and it is the one
    # place a new instance is not free. Leaving it at 210 would have bought
    # the shorter walk by clamping the *last* fallback's read leg, and that
    # afternoon the last fallback was the only one answering the mini at all.
    # `tests/test_one_press_is_bounded.py` derives this from
    # `len(OSM_OVERPASS_FALLBACK_URLS)` and goes red if the two drift.
    #
    # What that does **not** guarantee, because review reproduced it: a
    # primary whose four `504`s each take 30 s of the read allowance spends
    # ~177 s legally, and the first fallback then gets a clamped 28 s read
    # while the second is never dialled. The guarantee is therefore "a prompt
    # refusal on the primary leaves a complete attempt for each fallback", not
    # "every path does". Making it unconditional would mean 76 + 4x60 + 2x65
    # -- seven and a half minutes for one lookup -- which is the cost this
    # ticket exists to remove. A clamped attempt is reported as
    # `budget_exhausted` and never held against the host it was cut short on
    # (`utils/http.py`), so the price of the gap is a retry, not a wrong
    # answer.
    #
    # A tighter number would clamp a real query's read leg, and the presets
    # query asks Overpass for up to `[timeout:90]` of computation. That would
    # not break #98 -- a spent budget is recorded as `budget_exhausted`, a
    # refusal and never an absence -- but it would buy latency with retries
    # nobody asked for. What actually bounds one Enrich press is
    # ENRICH_LOOKUP_BUDGET_S below; this bounds the callers that have no run
    # around them, which is every backfill.
    OSM_OVERPASS_WALK_BUDGET_S = float(
        os.environ.get("OSM_OVERPASS_WALK_BUDGET_S") or "275"
    )
    # The ceiling on *all* the free lookups one Enrich press may wait for --
    # every Overpass walk and every elevation query in the run, together.
    #
    # The walk budget alone bounds one lookup; an enrichment run makes up to
    # eleven, so without this the press is still bounded only by their sum. The
    # decisive steps run first (services/property_enrichment_service.py), so
    # what an outage costs the advisory ones is whatever is left -- which is
    # the point: an advisory, score-neutral step must not hold a paid one
    # hostage, and when the clock is gone it records `unavailable` and the run
    # goes on.
    #
    # 305 s is a little over one full walk, so a total outage costs the press
    # about five minutes: one lookup that learns the instances are down, a
    # second that spends what is left, and every one after that refusing
    # before it opens a socket. Measured on the mini 2026-08-20, one lookup
    # alone cost 888 s and the run made eleven.
    #
    # It was 240 against a 210 s walk, and it moved with the walk rather than
    # for a reason of its own: this has to stay *above* the walk ceiling or
    # the second lookup of a run is starved on a merely-degraded day, which is
    # the outage's symptom appearing when there is no outage. The minute it
    # costs an unlucky press is paid only when every instance is down, and
    # `enrich_budget` hands the number to both clients so nothing has to be
    # told the ceiling moved.
    ENRICH_LOOKUP_BUDGET_S = float(os.environ.get("ENRICH_LOOKUP_BUDGET_S") or "305")
    # What the rest of one run may take: the paid Google steps plus the one
    # free HTTP fetch that is neither Google nor an OSM lookup. Not a deadline
    # -- nothing enforces it, and nothing should, since abandoning a billed
    # request is how a press pays for a measurement nobody receives (#178). It
    # exists so `services/enrich_budget.py` can state the run's worst case
    # instead of a client guessing at it.
    #
    # Added up from the transports rather than picked, and rounded up because
    # the harmful direction is being *short*: a client that stops polling
    # while the server is still working reports a running job as failed, and
    # the obvious next move pays for it again.
    #
    #   geocoding      2 queries x 3 attempts x 10 s + backoff   ~70 s
    #   Places         wide search, 3 attempts x 12 s + backoff  ~40 s
    #   DistanceMatrix 3 attempts x 15 s + backoff                ~50 s
    #   advertiser     3 attempts x 20 s behind a 3 s gate       ~70 s
    #
    # Those worst cases do not co-occur in any run anyone has observed; 240 s
    # is roughly the sum of the two largest plus room, and being generous here
    # costs a spinner that waits, not money.
    ENRICH_PAID_ALLOWANCE_S = float(os.environ.get("ENRICH_PAID_ALLOWANCE_S") or "240")

    # Sea-view estimation. Both sources are free and keyless -- Google billing
    # is off (#98) and is not needed here: the coastline comes from
    # OpenStreetMap and the terrain from Copernicus EU-DEM (25 m).
    SEA_VIEW_ELEVATION_URL = os.environ.get(
        "SEA_VIEW_ELEVATION_URL", "https://api.opentopodata.org/v1/eudem25m"
    )
    # OpenTopoData's public instance asks for one call per second and caps a
    # request at 100 locations. Staying under that is why a backfill is slow
    # rather than parallel.
    SEA_VIEW_ELEVATION_MIN_INTERVAL_S = float(
        os.environ.get("SEA_VIEW_ELEVATION_MIN_INTERVAL_S", "1.1")
    )
    SEA_VIEW_ELEVATION_MAX_LOCATIONS = 100

    # Paths
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
    # Attached documents and photos (#430). Under DATA_DIR because that is the
    # one directory docker-compose bind-mounts, so a file written here survives
    # the `COPY . .` rebuild that takes the image -- and it is already covered
    # by the `data/*` line in .gitignore, so nothing here is ever committed.
    ATTACHMENTS_DIR = os.environ.get(
        "ATTACHMENTS_DIR", os.path.join(DATA_DIR, "attachments")
    )
    # The whole request body. Werkzeug refuses past this with 413 rather than
    # buffering it, which is the only place a limit can be applied before the
    # bytes arrive; the per-file cap in services/attachments.py is counted as
    # they stream, because this one cannot bound one part of a multipart body.
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 32 * 1024 * 1024))
    LAST_SEEN_UID_PATH = os.environ.get(
        "LAST_SEEN_UID_PATH", os.path.join(DATA_DIR, ".last_seen_uid")
    )
    LAST_SEEN_UID_PROPERTIES_PATH = os.environ.get(
        "LAST_SEEN_UID_PROPERTIES_PATH",
        os.path.join(DATA_DIR, ".last_seen_uid_properties"),
    )

    # Ingestion target selector (legacy lands vs new universal properties)
    INGESTION_TARGET = os.environ.get("INGESTION_TARGET", "properties").lower()

    # Professional scoring weights based on Spanish/European standards
    # Total must equal 1.0 (100%) - Updated to include Investment Yield
    DEFAULT_SCORING_WEIGHTS = {
        # Investment & Financial Returns (20%) - NEW CRITERION
        "investment_yield": 0.20,  # Rental yield, cap rate, investment metrics
        # Location & Accessibility (28%)
        "location_quality": 0.16,  # Proximity to urban centers, neighborhood
        "transport": 0.12,  # Public transport, road access
        # Infrastructure & Utilities (24%)
        "infrastructure_basic": 0.16,  # Water, electricity, sewerage, internet
        "infrastructure_extended": 0.08,  # Gas, telecommunications, public services
        # Physical & Environmental (12%)
        "environment": 0.08,  # Environmental quality, natural features
        "physical_characteristics": 0.04,  # Topography, size, shape
        # Services & Amenities (8%)
        "services_quality": 0.08,  # Schools, hospitals, shopping
        # Legal & Development (8%)
        "legal_status": 0.04,  # Zoning status, building permissions
        "development_potential": 0.04,  # Future development possibilities
    }

    # Dual Scoring System - MCDM Profiles for Investment vs Lifestyle analysis
    SCORING_PROFILES = {
        # Investment Profile - Focus on rental yield, location value, and development potential
        "investment": {
            "investment_yield": 0.35,  # Primary factor for investment returns
            "location_quality": 0.20,  # Location drives property values
            "legal_status": 0.10,  # Legal clarity essential for investment
            "transport": 0.10,  # Accessibility affects rental demand
            "infrastructure_basic": 0.10,  # Basic utilities needed for rentals
            "development_potential": 0.08,  # Future value appreciation
            "physical_characteristics": 0.05,  # Size/shape for development
            "infrastructure_extended": 0.02,  # Nice-to-have for investments
            "services_quality": 0.00,  # Minimal impact on investment returns
            "environment": 0.00,  # Minimal impact on investment returns
        },
        # Lifestyle Profile - Focus on quality of life, environment, and daily amenities
        "lifestyle": {
            "environment": 0.22,  # Views, nature, air quality for living
            "services_quality": 0.18,  # Schools, healthcare, shopping for family
            "location_quality": 0.20,  # Neighborhood quality for living
            "transport": 0.12,  # Daily commute and accessibility
            "infrastructure_extended": 0.10,  # Gas, telecommunications for comfort
            "infrastructure_basic": 0.08,  # Essential utilities
            "physical_characteristics": 0.05,  # Land shape/size for personal use
            "legal_status": 0.03,  # Less critical for personal residence
            "development_potential": 0.02,  # Future changes can be disruptive
            "investment_yield": 0.00,  # Not relevant for personal residence
        },
    }

    # Combined Score Mix - Weighted combination of Investment and Lifestyle scores
    # Based on user preferences: Investment (32%) + Lifestyle (68%)
    COMBINED_MIX = {
        "investment": 0.32,  # Weight for investment score in combined calculation
        "lifestyle": 0.68,  # Weight for lifestyle score in combined calculation
    }
