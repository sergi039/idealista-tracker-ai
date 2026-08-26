"""
Internationalization utilities for the application
"""

from flask import session, request

TRANSLATIONS = {
    "en": {
        # Navigation
        "app_title": "Idealista Tracker AI",
        "properties": "Properties",
        "map": "Map",
        "scoring_criteria": "Scoring Criteria",
        "settings": "Settings",
        "manual_sync": "Manual Sync",
        # Saved searches ("subscriptions" in the owner's words)
        "subscription": "Subscription",
        "subscriptions": "Subscriptions",
        "all_subscriptions": "All subscriptions",
        "archive": "Archive",
        # Subscription copy shared by /properties, /map and /profiles.
        #
        # The counted ones come in `_one`/`_other` pairs read by `tn()`: both
        # languages here inflect the noun (and, in Spanish, the adjective with
        # it -- "suscripciÃ³n oculta" against "suscripciones ocultas"), so a
        # single string with a number dropped into it cannot be written
        # correctly in either. What is deliberately *not* pluralised is the
        # sentence around them: "Not shown:" leads the line instead of
        # trailing it, because a trailing "no se muestra(n)" would have to
        # agree with a subject that is itself a count.
        "not_shown_prefix": "Not shown",
        "manage": "manage",
        "no_subscription": "No subscription",
        "no_active_subscriptions": "No active subscriptions yet.",
        "hidden_badge": "Hidden",
        "hidden_subscriptions": "Hidden",
        "hidden_subscriptions_count_one": "%s hidden subscription",
        "hidden_subscriptions_count_other": "%s hidden subscriptions",
        "listings_count_one": "%s listing",
        "listings_count_other": "%s listings",
        "holding": "holding",
        "subscriptions_count_one": "%s subscription",
        "subscriptions_count_other": "%s subscriptions",
        "unassigned_not_shown_one": "%s listing with no subscription not shown",
        "unassigned_not_shown_other": "%s listings with no subscription not shown",
        "unassigned_included_one": "includes %s listing with no subscription",
        "unassigned_included_other": "includes %s listings with no subscription",
        "show_them": "show them",
        "selection_truncated_one": "Only the first %s subscription of that selection is being shown.",
        "selection_truncated_other": "Only the first %s subscriptions of that selection are being shown.",
        "unknown_profile": "Unknown profile #%s",
        # /profiles
        "search_profiles": "Search Profiles",
        "search_profiles_subtitle": "Separate configuration per saved search (no mixing)",
        "new_profile": "New profile",
        "column_id": "ID",
        "column_name": "Name",
        "column_properties": "Properties",
        "column_default": "Default",
        "column_active": "Active",
        "column_visible": "Visible",
        "column_updated": "Updated",
        "hide": "Hide",
        "show": "Show",
        "hide_title": "Take this subscription off the property views",
        "show_title": "Show this subscription on the property views again",
        "hide_default_title": "The default profile receives unmatched emails, so it cannot be hidden",
        "no_profiles_yet": "No profiles yet.",
        "unassigned_row_help": "Listings stored without a saved search — see the note below.",
        "hidden_panel_title": "Hidden is about the screen, not about the data.",
        "hidden_panel_body": "A hidden subscription keeps every listing it holds and keeps receiving its own alert emails — it is simply not offered on the property views: no chip, no entry in the subscription menu, not even under Archive, and its listings are out of “all subscriptions”, the map and the CSV export. They stay reachable through a direct link (/properties?profile_id=<id>) and through the count in this table, and Show above puts everything back. Active is the other question: an inactive subscription is archived, which still offers it one tick away.",
        "profiles_routing_note": "Idealista emails are routed by the saved-search link they carry (issue #102); an email with no recognizable link falls back to the saved-search name, the optional regex matchers, and finally the default profile.",
        "profiles_unassigned_title": "A listing can end up with no subscription at all.",
        "profiles_unassigned_body": "That happens when one email links to several different saved searches (guessing one would be silently wrong), when a link was read but its profile could not be resolved, or when a profile is deleted — its listings are kept and detached rather than removed. They are counted in the No subscription row above and listed under that entry on the properties list. There is no control to file one by hand: ingestion is the only writer of a listing’s subscription (issue #130), so a row sits here precisely because its email named no single saved search.",
        "profiles_unassigned_link": "that entry on the properties list",
        "profile_hidden_flash": "“%s” is hidden from the property views",
        "profile_shown_flash": "“%s” is shown again",
        "profile_default_cannot_hide": "The default profile receives every email that matches nothing else, so it cannot be hidden. Make another profile the default first.",
        # Property list
        "property_overview": "Property Overview",
        "total_properties": "Total Properties",
        "average_score": "Average Score",
        "price_range": "Price Range",
        "filters": "Filters",
        "filters_search": "Filters & Search",
        "search": "Search",
        "search_properties": "Search, or paste a listing URL...",
        "search_read_as_listing": "Read as Idealista listing %s — nothing here carries that id.",
        "search_read_as_link": "Read as a listing link (%s) — nothing here carries that link.",
        "land_type_all": "All Types",
        "developed": "Developed",
        "buildable": "Buildable",
        "all_municipalities": "All Municipalities",
        # Four states, not a flag -- see services/sea_view_service.py.
        "sea_view": "Sea view",
        "sea_view_any": "Sea view: any",
        "sea_view_confirmed": "Sea view: confirmed",
        "sea_view_yes_or_likely": "Sea view: confirmed or likely",
        # Straight-line metres to the coastline, labelled with the walk they
        # roughly buy at 5 km/h. The walk is over roads and never shorter than
        # the straight line, which is why the metres lead the label.
        "sea_distance_filter": "Distance to the sea",
        "sea_distance_any": "Sea distance: any",
        "sea_distance_400": "Sea ≤ 400 m (~5 min walk)",
        "sea_distance_800": "Sea ≤ 800 m (~10 min walk)",
        "sea_distance_1600": "Sea ≤ 1.6 km (~20 min walk)",
        "route_from_gijon": "Route from Gijón (Google Maps)",
        # Curated buildability -- attributes.land_classification, written by
        # hand-run curation, never by ingestion.
        "build_filter": "Buildability",
        "build_any": "Build: all",
        "build_solar": "Build: urban/solar (build now)",
        "build_urbanizable": "Build: urbanizable (after works)",
        "build_claimed": "Build: claimed by seller",
        "build_classified": "Build: any curated",
        "sea_view_state_yes": "Sea view",
        "sea_view_state_likely": "Sea view likely",
        # A `likely` that rests on bare-earth terrain alone. It says the ground
        # does not block the line, not that the listing has a sea view -- and
        # what the line reaches can be an estuary channel rather than open
        # water (#334). Named for what was computed; see state_label_key().
        "sea_view_state_likely_geometry": "Terrain allows a sea view",
        # The other geometry `likely`: the water's edge under the house is
        # hidden by nearer ground and open water is visible over it, which is
        # the ordinary hillside plot and used to be reported as "No sea view".
        "sea_view_state_likely_geometry_over_terrain": "Sea visible over nearer ground",
        "sea_view_state_no": "No sea view",
        "sea_view_state_unknown": "Sea view unknown",
        # The two facts the geometry keeps apart, for the detail card.
        "sea_view_shoreline_hidden": "the shore below is hidden from",
        "sea_view_open_water_at": "open water visible from",
        # A plot inside the 500 m coastal band cannot carry a dwelling at all
        # (POLA/PESC, which outranks the municipal PGOU), so the list states
        # the verdict rather than the raw metre count alone.
        "plot_coast_ban": "Coastal ban zone",
        "plot_coast_ok": "Outside coastal ban",
        "plot_coast_unmeasured": "Coast distance unmeasured",
        # The coordinate is a locality centroid (#358), and the 500 m band it
        # sits inside or outside cannot be told apart within the slack: the
        # bounds straddle 500 m, so the verdict is neither a robust ban nor a
        # robust clearance. Kept apart from `plot_coast_unmeasured`, which
        # means the lookup itself never ran -- this one ran and still could
        # not answer for the parcel.
        "plot_coast_unmeasured_approximate": (
            "Coast distance unmeasured (approximate location)"
        ),
        "plot_coast_none_near": "No coast within radius",
        "plot_class_check": "Classification unverified",
        "plot_pgou_warning": "Outside PGOU urban zones?",
        "plot_listing_conflict": "Listing contradicts itself",
        "plot_geocoding_failed": "Coordinate is wrong",
        "plot_price_outlier": "Price per m² far below the norm",
        # The coastline point the geometry was measured to, on a map: the one
        # thing that answers "what water is this?" without re-deriving it from
        # a distance and a bearing.
        "sea_view_target_link": "see the point measured to",
        # Shown instead of the literal source code "none": nobody computed a
        # verdict here, which is not the same as computing "no sea view".
        "sea_view_not_computed": "Not computed yet",
        # Listing status is a claim, and a claim needs a source: `active` is
        # the default a row is ingested with, so the surfaces say when nobody
        # ever verified it (services/listing_verification.py).
        "listing_state_active": "Live",
        "listing_state_unchecked": "Unverified",
        "listing_verified_tooltip": "Confirmed against Idealista",
        "listing_unchecked_tooltip": "Never verified on the source site — the default a new listing carries",
        "listing_checked_ago": "checked %s d ago",
        "listing_status_coverage": "Listing status: %s of %s verified on the source site",
        # The filter bar narrowed the result, and the count line says so --
        # first %s is the filtered total, second the same subscriptions
        # without the filter bar.
        "filter_bar_narrowing": "Filters: %s of %s shown",
        "clear_filters_link": "clear filters",
        # Importing listings by link (fotocasa).
        "import_listings": "Import listings by link",
        "back_to_listings": "Back to listings",
        "import_paste_links": "Fotocasa listing links",
        "import_links_help": "One per line. Only fotocasa.es listing pages — a search results page has no listing to read.",
        "import_destination": "Add to subscription",
        "import_choose_destination": "Choose a subscription…",
        "import_destination_help": "This decides where the listing appears and whose comparables it joins. A listing with no subscription is not shown on a bare /properties.",
        "import_no_destination": "Every subscription is hidden, and a hidden one is not offered here — an import would land where the page does not show it.",
        "import_no_destination_link": "Show one again",
        "import_read_pages": "Read the pages",
        "import_reading": "Reading the pages. Nothing is saved yet — this page refreshes when it finishes.",
        "import_nothing_read": "That import read nothing. Paste the links again.",
        "import_preview": "What the pages said",
        "import_preview_counts": "%s new of %s links",
        "import_row_new": "New",
        "import_row_duplicate": "Already here",
        "import_row_failed": "Not read",
        "import_add_n": "Add %s listing(s)",
        "import_approximate_help": "Fotocasa declares this coordinate inexact, so travel times are not measured from it and distances carry a margin.",
        "approximate": "approximate",
        "coordinates": "Coordinates",
        "status": "Status",
        "source": "Source",
        "source_all": "All sources",
        "listing_status_coverage_tooltip": "The rest carry the status they were ingested with, which nobody confirmed. Idealista blocks the automatic checker from this machine; fotocasa answers it.",
        # Who is selling: the owner, or an agency (services/advertiser.py).
        "advertiser": "Seller",
        "advertiser_all": "Seller: all",
        "advertiser_owner": "Private owner",
        "advertiser_agency": "Agency",
        "advertiser_unknown": "Source did not say",
        "advertiser_unchecked": "Not established",
        "advertiser_source_alert_campaign": "From the Idealista alert email that delivered this listing.",
        "advertiser_source_portal_payload": "Read from the listing page.",
        "advertiser_source_manual": "Set by hand.",
        "advertiser_set_header": "Record who is selling",
        "advertiser_clear": "Clear — use the computed reading",
        # What the owner decided, and what is still outstanding on the listing
        # (services/owner_review.py). `undecided` is the state of a listing
        # nobody has judged; it is never rendered as a rejection.
        "owner_verdict": "Verdict",
        "owner_verdict_all": "Verdict: all",
        "owner_verdict_interested": "Interested",
        "owner_verdict_waiting": "Waiting",
        "owner_verdict_rejected": "Rejected",
        "owner_verdict_undecided": "Not decided yet",
        "next_action": "Next action",
        "next_action_all": "Action: all",
        "next_action_none": "Nothing outstanding",
        "next_action_pending": "Outstanding",
        "next_action_overdue": "Overdue",
        "next_action_due": "due",
        "next_action_due_on": "Due date",
        "review_section_title": "Decision & next action",
        "review_reason": "Why",
        "review_reason_placeholder": "Why this verdict — the reason is what a later session reads instead of researching it again",
        "review_next_action_placeholder": "What is still outstanding — «condiciones de edificabilidad por escrito»",
        "review_save": "Save",
        "review_clear_verdict": "Not decided",
        "review_no_verdict": "Nobody has decided about this listing yet.",
        "review_decided_at": "Recorded",
        "review_history_out_of_sync": "The stored decision does not match the newest entry in the log — something wrote it outside this page.",
        "review_overdue_link_one": "%s overdue",
        "review_overdue_link_other": "%s overdue",
        # The parcel behind the listing (services/cadastre_service.py).
        "cadastre_section_title": "Cadastral parcel",
        "cadastre_reference": "Cadastral reference",
        "cadastre_reference_placeholder": "20 characters from the ficha catastral — 33016A003001530001HQ",
        "cadastre_fetch": "Fetch from Catastro",
        "cadastre_clear": "Clear",
        "cadastre_none": "No parcel recorded. Paste the reference from the ficha catastral and the outline is fetched from Catastro — free, no key.",
        "cadastre_not_measured": "The reference is recorded; the parcel has not been measured yet.",
        "cadastre_class": "Class",
        "cadastre_use": "Use",
        "cadastre_poligono": "Polígono / parcela",
        "cadastre_paraje": "Paraje",
        "cadastre_area": "Cadastral area",
        "cadastre_shape": "Shape",
        "cadastre_bbox": "Bounding box",
        "cadastre_fill_ratio": "Fills",
        "cadastre_compactness": "Compactness",
        "cadastre_compactness_hint": "Polsby-Popper: a circle is 1.00, a square 0.79. Below about 0.4 the plot is long, bent or has a neck.",
        "cadastre_subparcels": "Subparcels",
        "cadastre_viewer": "Open in the Catastro viewer",
        "cadastre_measured_in": "measured in EPSG:%s",
        "cadastre_crs_not_recorded": "the projection it was measured in was not recorded",
        "cadastre_source_metric_geometry": "Outline",
        "cadastre_source_map_geometry": "Map outline",
        "cadastre_source_attributes": "Parcel data",
        "cadastre_state_ok": "measured",
        "cadastre_state_not_found": "no such parcel",
        "cadastre_state_refused": "Catastro refused",
        "cadastre_state_unavailable": "could not be reached",
        "cadastre_state_malformed": "unreadable answer",
        "cadastre_state_not_applicable": "not applicable",
        "cadastre_state_unsupported_metric_crs": "outside the zones Catastro measures",
        "cadastre_kept_previous": "kept from an earlier fetch",
        # The conversation behind a listing: notes and contact entries.
        "timeline_title": "What happened",
        "timeline_empty": "Nothing recorded yet. A note, or an exchange with the agency, goes here.",
        "timeline_add_note": "Note",
        "timeline_add_contact": "Exchange",
        "timeline_when": "When",
        "timeline_channel": "Channel",
        "timeline_counterpart": "Who",
        "timeline_counterpart_placeholder": "David Villa, Sellmi",
        "timeline_asked": "Asked",
        "timeline_asked_placeholder": "What was asked — «¿tienen la ficha catastral?»",
        "timeline_answer": "Answer",
        "timeline_answer_placeholder": "What came back — hearsay is worth recording as hearsay",
        "timeline_note_placeholder": "Anything worth not researching twice",
        "timeline_add": "Add",
        "timeline_more_fields": "Exchange with the agency, date, file…",
        "timeline_save": "Save",
        "timeline_delete": "Remove",
        "timeline_edited": "edited",
        "timeline_verdict_entry": "Decision recorded",
        "timeline_verdict_readonly": "This is the record of a decision — change it in the block above.",
        # `_label` and not a bare `channel_other`: a key ending in `_other`
        # reads as the plural form of a counted phrase, and
        # tests/test_subscription_copy_is_translated.py then demands a
        # `_one` beside it. The suffix keeps the two vocabularies apart.
        "channel_whatsapp_label": "WhatsApp",
        "channel_email_label": "Email",
        "channel_portal_label": "Portal chat",
        "channel_phone_label": "Phone",
        "channel_visit_label": "Visit",
        "channel_other_label": "Other",
        # Attached documents and photos (services/attachments.py).
        "attachment_add": "Attach a file",
        "attachment_hint": "PDF or photo, up to 25 MB. The type is read from the file itself, not from its name.",
        "attachment_remove": "Remove this file",
        "attachment_refused": "The file was refused",
        # Quick scope switches on the properties toolbar
        "favorites": "Favorites",
        "hide_removed": "Hide removed",
        "price": "Price",
        "area": "Area",
        "beach_distance": "Beach Distance",
        "date_added": "Date Added",
        "sort_by": "Sort by",
        "apply": "Apply",
        "clear": "Clear",
        "export_csv": "Export CSV",
        "properties_found": "properties found",
        "last_sync": "Last sync",
        "loading": "Loading",
        # Table headers
        "title": "TITLE",
        "coords": "COORDS",
        "beach": "BEACH",
        "travel": "TRAVEL",
        "type": "TYPE",
        "actions": "ACTIONS",
        "municipality": "Municipality",
        "land_type": "Land Type",
        "legal_status": "Legal Status",
        "score": "Score",
        "view_details": "View Details",
        "clear_filters": "Clear Filters",
        # Property details
        "property_description": "Property Description",
        "key_highlights": "Key Highlights",
        "location": "Location",
        "view_on_maps": "Google Maps",
        "view_on_idealista": "Idealista",
        "view_on_app_map": "Our Maps",
        "travel_times_distances": "Travel Times & Distances",
        "distance_to_sea": "Distance to sea",
        "sea_distance_no_coastline": "No coastline within",
        "sea_distance_unavailable": "Coastline data unavailable",
        "sea_distance_not_measured": "Not measured yet",
        # An approximate coordinate is a locality centroid, so anything
        # measured from it belongs to the locality and not to this parcel.
        "sea_distance_approximate_origin": "Not measured (approximate location)",
        "approximate_origin_short": "Approximate location",
        "approximate_origin_tooltip": (
            "This listing's coordinate is a locality centroid, not its address. "
            "Distances and travel times measured from it describe that point, "
            "not this property."
        ),
        "approximate_origin_notice": (
            "The coordinate on this listing is a locality centroid, not its "
            "address, so the times below are routes from that point rather "
            "than from this property. They do not count towards its score."
        ),
        # No coordinate at all: nothing was asked of any API, so the empty
        # travel column is an absence of measurement and not a measured
        # absence (#98). The tooltip names the way out, because the row can
        # be fixed -- the Enrich button geocodes before it measures.
        "not_located_short": "Not located",
        "not_located_tooltip": (
            "This listing has no coordinate, so nothing has been measured "
            "for it -- no travel times, and no beaches. Press Enrich to "
            "geocode it first."
        ),
        "not_located_notice": (
            "This listing has no coordinate yet, so nothing below has been "
            "measured: travel times, beaches and distance to the sea all "
            "start from a point this row does not have. Enrich geocodes it."
        ),
        # Which engine measured the drive times on this row. Recorded
        # per run, never inferred from the configuration.
        "routing_engine_osrm": "Drive times from the local routing engine (OSRM)",
        "routing_engine_google_distance_matrix": "Drive times from Google Distance Matrix",
        "from_the_geocoded_point": "from the geocoded point",
        "shared_coordinate_with": "same coordinate as",
        "shared_coordinate_tooltip": (
            "Another listing is stored at exactly this point. Two homes in one "
            "building can share one legitimately; separate plots cannot."
        ),
        # The list is capped, and a capped list that does not name its own cap
        # reads as the whole cluster (UNIVERSE-001, #265).
        "shared_coordinate_more": "and %s more",
        "beaches_within": "Beaches ≤",
        "minutes_drive": "min by car",
        "nearest_beach": "Nearest beach",
        "beaches_not_measured": "Not measured",
        # List-row beach line: three states kept apart per #98.
        "beach_short": "BEACH",
        "next_beach": "next",
        "beach_none_within": "no beach ≤",
        "beach_none_within_tooltip": "Measured: no beach within the drive limit",
        "beach_not_measured_tooltip": "The beach lookup did not answer — not measured",
        "more_beaches_within": "more ≤",
        "nearest_airport": "Nearest Airport",
        "train_station": "Train Station",
        "hospital": "Hospital",
        "basic_infrastructure": "Basic Infrastructure",
        "extended_infrastructure": "Extended Infrastructure",
        "transport": "Transport",
        "environment": "Environment",
        "services_quality": "Services Quality",
        "information": "Information",
        "added": "Added",
        "email_received": "Email Received",
        "ai_analysis": "AI Analysis",
        "enhance_description": "Enhance Description",
        "enrich_google_api": "Enrich with Google API",
        # Scoring
        "dual_scoring_analysis": "Dual Scoring Analysis",
        # Detail-page infographic (proposal D6-D9, approved 2026-08-13).
        "score_band_strong": "Strong",
        "score_band_moderate": "Moderate",
        "score_band_weak": "Weak",
        "component_value": "Value",
        "component_size": "Size",
        "component_travel": "Travel",
        "component_sea": "Sea",
        "component_pool": "Pool",
        "component_hazard": "Industrial neighbours",
        "component_not_measured": "not measured — excluded",
        "weight_renormalized_tooltip": "Effective weight after excluding unmeasured components",
        "combined_score": "Combined",
        # #379: the combined score rests on the criteria that answered.
        "score_coverage_tooltip": "Criteria measured / enabled; share of the enabled weight the score rests on",
        "score_coverage_line": "The score rests on {share}% of the enabled weight ({measured} of {enabled} criteria measured)",
        "score_coverage_missing": "not measured",
        "measured_filter": "Measured criteria",
        "measured_any": "Measured: any",
        "measured_full": "Measured: fully",
        "show_original_email": "Show original email text",
        "no_photos": "No photos",
        "no_photos_tooltip": "The email alerts carry no images; photo sourcing is a separate decision",
        "route_from_property": "Route from this property",
        # Hazardous neighbours (#437). The card names what OSM cannot answer
        # rather than letting a list of four facilities read as a survey.
        "hazards_title": "Industrial neighbours",
        "hazards_badge": "Industry nearby",
        "hazards_badge_centroid": "Industry near the locality",
        "hazards_incomplete_badge": "Scan incomplete",
        "hazards_badge_tooltip": "OpenStreetMap records an industrial or waste facility near this listing",
        "hazards_none": "Nothing recognised within the radius scanned",
        "hazards_none_incomplete": "Nothing recognised among the elements this scan saw — and it did not see all of them",
        "hazards_unavailable": "Not measured — OpenStreetMap refused the lookup",
        "hazards_no_coordinates": "Not measured — this listing has no coordinate",
        "hazards_missing": "Not scanned yet",
        "hazards_stale_origin": "Not measured for this coordinate — the listing has been re-located since the scan",
        "hazards_not_measured": "Not measured",
        "hazards_coverage": "Industrial neighbours: %s of %s listings hold a complete scan",
        "hazards_coverage_tooltip": "The rest were never scanned, or their scan came back incomplete. A listing that holds a complete scan may still have been re-located since it was taken — the listing page says so. A listing with no scan is not a listing with nothing near it.",
        "hazards_searched": "Scanned %s km around the stored coordinate",
        "hazards_guaranteed": "guaranteed within %s km of the parcel",
        "hazards_approximate": "The coordinate is a locality centroid, so each distance is a range, not a point",
        "hazards_straight_line": "Straight-line distance from the stored coordinate; air and noise travel in straight lines, a road does not",
        "hazards_bearing": "bearing",
        "hazards_elements_one": "%s OpenStreetMap element",
        "hazards_elements_other": "%s OpenStreetMap elements",
        "hazards_more_one": "%s more facility not shown",
        "hazards_more_other": "%s more facilities not shown",
        "hazards_truncated": "The scan reached its element limit, so this list may be incomplete",
        "hazards_limits": "OpenStreetMap says a facility exists. It says nothing about its emissions (PRTR-España publishes those), nothing about measured air quality, and nothing about a plant that is approved but not yet built. Which way the wind blows is not recorded here either — only the bearing is.",
        # Names what the facility *is*, not what it emits. OSM establishes
        # a cement works; the block's own disclosure says in the next
        # breath that it establishes nothing about emissions, and a badge
        # reading "Emitting" beside that sentence was an inference wearing
        # a measurement's clothes -- the STATUS-002 shape (review, #453).
        "hazard_severity_high": "Heavy industry",
        "hazard_severity_moderate": "Nuisance",
        "hazard_kind_cement_works": "Cement works",
        "hazard_kind_steelworks": "Steelworks",
        "hazard_kind_refinery": "Refinery",
        "hazard_kind_chemical_works": "Chemical works",
        "hazard_kind_power_plant": "Combustion power plant",
        "hazard_kind_nuclear_plant": "Nuclear power plant",
        "hazard_kind_lng_terminal": "LNG terminal",
        "hazard_kind_lpg_storage": "LPG storage",
        "hazard_kind_fuel_depot": "Fuel depot",
        "hazard_kind_coal_yard": "Coal yard",
        "hazard_kind_incinerator": "Incinerator",
        "hazard_kind_paper_mill": "Paper mill",
        "hazard_kind_foundry": "Foundry",
        "hazard_kind_smelter": "Smelter",
        "hazard_kind_glassworks": "Glassworks",
        "hazard_kind_coking_plant": "Coking plant",
        "hazard_kind_tannery": "Tannery",
        "hazard_kind_asphalt_plant": "Asphalt plant",
        "hazard_kind_concrete_plant": "Concrete plant",
        "hazard_kind_slaughterhouse": "Slaughterhouse",
        "hazard_kind_landfill": "Landfill",
        "hazard_kind_quarry": "Quarry",
        "hazard_kind_mine": "Mine",
        "hazard_kind_port_industry": "Industrial port",
        "hazard_kind_wastewater_plant": "Wastewater plant",
        "hazard_kind_waste_transfer": "Waste transfer station",
        "hazard_kind_combustion_stack": "Industrial chimney",
        # Quality-of-life card (Phase 2 slice).
        "qol_title": "Quality of Life",
        "qol_municipality_context": "Municipality",
        "qol_renta": "Net income / person",
        "qol_renta_tooltip": "INE ADRH, renta neta media por persona",
        "qol_vs_province_tooltip": "Index vs province median = 100",
        "qol_population": "Population",
        "qol_population_tooltip": "INE Padrón",
        "qol_population_5y": "5-year change",
        "qol_municipality_not_matched": "Municipality not matched to INE data",
        "qol_no_reference_data": "Reference data not imported yet",
        "qol_supermarkets": "Supermarkets",
        "qol_straight_line": "straight-line",
        "qol_unnamed_shop": "Unnamed shop",
        "amenity_unnamed": "Unnamed",
        "qol_convenience_tooltip": "OSM shop=convenience — a small local shop",
        "qol_osm_empty": "None found in OSM within 12 km — coverage uncertain",
        "qol_not_measured": "Not measured",
        "qol_no_municipality": "No municipality recorded for this listing",
        "qol_no_coordinates": "No coordinates — not measured",
        "qol_hospitals": "Hospitals",
        # The vintage lives in the payload's own `source` field, not here —
        # a translation string pinning "2025" would outlive a re-import.
        "qol_hospital_grouping_note": (
            "Local display grouping derived from National Hospital Catalogue "
            "fields (beds / teaching accreditation / high-tech equipment) — "
            "not an official tier"
        ),
        "qol_hospital_outside_coverage": (
            "Outside the hospital catalogue's coverage (Asturias + Galicia)"
        ),
        "qol_hospital_teaching": "Teaching / high-tech",
        "qol_hospital_general": "General acute",
        "qol_hospital_fields": "beds / teaching / high-tech",
        # Pool criterion (proposal D17).
        "pool_title": "Swimming pool",
        "pool_unnamed": "Unnamed pool",
        "pool_indoor": "indoor",
        "pool_indoor_likely": "indoor?",
        "pool_unverified_absence": "No pool verified nearby — not scored",
        "pool_unverified_tooltip": (
            "OSM found none and one Places cross-check agreed; a single "
            "query proves nothing about completeness, so this is never a 0"
        ),
        # Municipality comparison page (proposal D22).
        "municipalities": "Municipalities",
        # /construccion — the building-rules reference. The chapter BODIES
        # are committed Russian content, not chrome; these keys are only the
        # page chrome and the state words the #98 contract depends on.
        "construccion": "Building rules",
        "agencies": "Agencies",
        "agencies_title": "Top agencies — detached houses ≤ €300k, Asturias & Cantabria",
        "agencies_measured_on": "Measured on",
        "agencies_definition": (
            "Detached house = idealista's chalet independiente + casa rústica "
            "(casa de pueblo / rural / casona); adosados and pareados are excluded. "
            "Ranked by the idealista count; the chalets-independientes-only figure "
            "is shown beside it. fotocasa counts use that portal's Casa o chalet + "
            "Finca rústica types with the same price cap. Every count links to the "
            "filtered page it was read from."
        ),
        "agencies_col_agency": "Agency",
        "agencies_col_website": "Website",
        "agencies_col_idealista": "idealista · detached ≤ €300k",
        "agencies_col_fotocasa": "fotocasa · casa/chalet + finca ≤ €300k",
        "agencies_col_reviews": "Reviews",
        "agencies_col_description": "Description",
        "agencies_founded": "Founded",
        "agencies_independientes": "of which chalets independientes",
        "agencies_all_houses": "all casas y chalets",
        "agencies_method": (
            "Method: every agency account in the idealista directories of both provinces "
            "was measured on its own microsite with the filters applied; portal figures "
            "change daily and a listing may appear on several portals, so counts are not "
            "summed across portals."
        ),
        "agencies_no_data": (
            "The agency table is not available: data/top_agencies.json is missing or "
            "unreadable."
        ),
        "concejo_select_label": "Concejo:",
        "concejo_none_selected_option": "— not selected —",
        "concejo_scope_group": "Search perimeter",
        "concejo_all_group": "All of Asturias",
        "concejo_snapshot_missing": (
            "The concejo identity snapshot is missing — the page refuses "
            "rather than serve identity from a fallback source."
        ),
        "concejo_code_rejected": "Concejo code not recognised — no overlay shown.",
        "concejo_coverage_line": (
            "Perimeter: {scope} concejos. Search performed in {searched}. "
            "All mandatory topics searched in {mandatory} "
            "(confirmed values: {confirmed}, of them stale: {stale})."
        ),
        "concejo_beyond_scope": "Also researched beyond the perimeter: {n}.",
        "concejo_not_researched_banner": (
            "nobody has researched this municipality. None of the fields "
            "below is checked for it."
        ),
        "concejo_regional_norm": "Regional norm",
        "concejo_pick_first": "concejo not selected — municipal terms not checked",
        "concejo_for_name": "For {name}",
        "concejo_not_researched": "not researched",
        "concejo_not_confirmed": "searched, not confirmed",
        "concejo_searched_in": "Searched in",
        "concejo_agent_unverified": "unverified",
        "concejo_stale": "recheck recommended",
        "concejo_chapter_callout": ("Regional frame. Not confirmed for {name}:"),
        "concejo_chapter_callout_none": (
            "Regional frame. No concejo selected — municipal terms are not "
            "checked for any place."
        ),
        "concejo_topic_pgo": "PGO adopted, and when",
        "concejo_topic_cedula": "Cédula or certificado only",
        "concejo_topic_coastal": "Coastal concejo (POLA/PESC)",
        "concejo_topic_silence": "Licence silence period",
        "concejo_topic_occupation": "First-occupation regime",
        "concejo_topic_icio": "ICIO rate",
        "construccion_full_title": "Full dossier (regional)",
        "construccion_full_link": "Full dossier",
        "construccion_full_back": "Back to the reference",
        "construccion_full_note": (
            "This is the regional document in full. It carries no concejo "
            "context: nothing on this page is checked for any particular "
            "municipality — municipal terms live on the reference page."
        ),
        "construccion_full_missing": (
            "The full dossier file is missing from this build."
        ),
        "listings_lower": "listings",
        "municipalities_listings": "Listings",
        "municipalities_score": "Score",
        "municipalities_index": "vs prov.",
        "municipalities_unemployment": "Unempl. proxy",
        "municipalities_unemployment_tooltip": (
            "SEPE registered unemployed / population — a comparable ratio, "
            "not the official unemployment rate"
        ),
        "municipalities_facts_label": "Municipality facts",
        "municipalities_facts_note": "INE and SEPE figures for the municipality itself",
        "municipalities_medians_label": "Listing medians",
        "municipalities_medians_note": (
            "median over this municipality's listings; the count in brackets "
            "is how many were measured"
        ),
        "municipalities_coverage": "Measured listings",
        "municipalities_none_measured": "Nothing measured yet",
        "municipalities_archived": "Include archived",
        "municipalities_archived_tooltip": "Also count removed and sold listings",
        "municipalities_pool_indoor_tooltip": "Median to the nearest pool with indoor evidence",
        "municipalities_unnamed_note": "listings carry no municipality and are not compared",
        "municipalities_unassigned_listings_one": "%s listing with no subscription",
        "municipalities_unassigned_listings_other": (
            "%s listings with no subscription"
        ),
        "municipalities_scope_suffix": (
            "each row opens exactly the listings behind its number"
        ),
        "municipalities_drilldown_truncated": (
            "More subscriptions carry this municipality than one link can name, "
            "so its list shows fewer listings than the count here"
        ),
        "municipalities_empty": "No listings to compare yet.",
        # What the whole table is a comparison *of* (UNIVERSE-001, #265).
        # The page spans every subscription, retired ones included, and said
        # so nowhere. Nouns rather than counted phrases on purpose: "live 461
        # · retired 311" needs no plural agreement in either language, while
        # a sentence around the numbers would need four of them.
        "municipalities_scope_label": "Scope",
        # MUNIC-002. The control's own name is `Subscriptions`, and its two
        # standing options are worded so that neither reads as the other:
        # "every" is this page's own population (the archive included) and
        # "live only" is `profile_id=all`, which means "active and not hidden"
        # here exactly as it does on /properties, /map, the CSV export and the
        # JSON API. This is the one surface where `all` is *narrower* than the
        # bare URL, so the labels carry the distinction the tokens cannot.
        "municipalities_scope_control": "Subscriptions",
        "municipalities_scope_every": "every",
        "municipalities_scope_live_only": "live only",
        "municipalities_scope_selected": "only the subscriptions selected above",
        "municipalities_scope_note": (
            "every stored listing, whatever subscription it is in"
        ),
        # Counted phrases, through `tn()`: each of these modifies an elided
        # "subscription", and Spanish inflects the adjective with it -- "1
        # suscripcion activa" against "2 suscripciones activas" -- so a single
        # string with a number dropped into it is wrong in one of the two
        # cases. English does not inflect them, and both forms are written out
        # anyway rather than sharing one key, because a pair that happens to
        # be identical in one language is not the same thing as a phrase that
        # does not need a pair.
        "municipalities_scope_live_one": "%s live",
        "municipalities_scope_live_other": "%s live",
        "municipalities_scope_retired_one": "%s retired",
        "municipalities_scope_retired_other": "%s retired",
        "municipalities_scope_hidden_one": "%s hidden",
        "municipalities_scope_hidden_other": "%s hidden",
        "municipalities_scope_unknown_one": "%s whose subscription is gone",
        "municipalities_scope_unknown_other": "%s whose subscriptions are gone",
        "municipalities_scope_delisted_excluded": "removed and sold excluded",
        "municipalities_scope_delisted_included": "removed and sold included",
        "municipalities_scope_favorites": "favorites only",
        "municipalities_basis_label": "Basis",
        "municipalities_basis_note": (
            "\u20ac/m\u00b2 is the unadjusted median of asking price \u00f7 area across "
            "every type, subtype and size \u2014 it describes the inventory mix, not a "
            "size-adjusted price for the municipality"
        ),
        "pool_unroutable": "no road route",
        "pool_unroutable_tooltip": "Google answered: no driving route to this pool",
        "pool_owner_absence": "Owner confirmed: no usable pool",
        "pool_owner_absence_set": "Confirm: no usable pool here",
        "pool_owner_absence_clear": "Clear the no-pool verdict",
        "pool_owner_absence_tooltip": (
            "The only way this property's pool score becomes 0 — "
            "computed absence is never trusted that far"
        ),
        "investment_score": "Investment Score",
        "lifestyle_score": "Lifestyle Score",
        "criteria_breakdown": "Criteria Breakdown",
        "score_composition": "Score Composition",
        "investment_analysis": "Investment Analysis",
        "rental_market": "Rental Market",
        "monthly_rent": "Monthly Rent",
        "rental_yield": "Rental Yield",
        "cap_rate": "Cap Rate",
        "investment_rating": "Investment Rating",
        "risk_level": "Risk Level",
        "market_position": "Market Position",
        "development_cost": "Development Cost",
        "total_investment": "Total Investment",
        "cost_per_m2": "Cost per m²",
        # Criteria page
        "investment_profile": "Investment Profile",
        "lifestyle_profile": "Lifestyle Profile",
        "save_changes": "Save Changes",
        "weight": "Weight",
        # Messages
        "running_gmail_ingestion": "Running Gmail ingestion...",
        "enrichment_in_progress": "Enrichment in progress...",
        "analysis_complete": "Analysis complete",
        # Common
        "yes": "Yes",
        "no": "No",
        "unknown": "Unknown",
        "save": "Save",
        "cancel": "Cancel",
        "edit": "Edit",
        "close": "Close",
        "back_to_properties": "Back to Properties",
    },
    "es": {
        # Navigation
        "app_title": "Idealista Tracker AI",
        "properties": "Propiedades",
        "map": "Mapa",
        "scoring_criteria": "Criterios de Puntuación",
        "settings": "Configuración",
        "manual_sync": "Sincronización Manual",
        "subscription": "Suscripción",
        "subscriptions": "Suscripciones",
        "all_subscriptions": "Todas las suscripciones",
        "archive": "Archivo",
        "not_shown_prefix": "Sin mostrar",
        "manage": "gestionar",
        "no_subscription": "Sin suscripción",
        "no_active_subscriptions": "Aún no hay suscripciones activas.",
        "hidden_badge": "Oculta",
        "hidden_subscriptions": "Ocultas",
        "hidden_subscriptions_count_one": "%s suscripción oculta",
        "hidden_subscriptions_count_other": "%s suscripciones ocultas",
        "listings_count_one": "%s anuncio",
        "listings_count_other": "%s anuncios",
        "holding": "con",
        "subscriptions_count_one": "%s suscripción",
        "subscriptions_count_other": "%s suscripciones",
        "unassigned_not_shown_one": "%s anuncio sin suscripción sin mostrar",
        "unassigned_not_shown_other": "%s anuncios sin suscripción sin mostrar",
        "unassigned_included_one": "incluye %s anuncio sin suscripción",
        "unassigned_included_other": "incluye %s anuncios sin suscripción",
        "show_them": "mostrarlos",
        "selection_truncated_one": "Solo se muestra la primera %s suscripción de esa selección.",
        "selection_truncated_other": "Solo se muestran las primeras %s suscripciones de esa selección.",
        "unknown_profile": "Perfil desconocido n.º %s",
        "search_profiles": "Perfiles de búsqueda",
        "search_profiles_subtitle": "Configuración propia para cada búsqueda guardada (sin mezclar)",
        "new_profile": "Nuevo perfil",
        "column_id": "ID",
        "column_name": "Nombre",
        "column_properties": "Anuncios",
        "column_default": "Predeterminado",
        "column_active": "Activa",
        "column_visible": "Visible",
        "column_updated": "Actualizado",
        "hide": "Ocultar",
        "show": "Mostrar",
        "hide_title": "Quitar esta suscripción de las vistas de anuncios",
        "show_title": "Volver a mostrar esta suscripción en las vistas de anuncios",
        "hide_default_title": "El perfil predeterminado recibe los correos sin coincidencia, por eso no se puede ocultar",
        "no_profiles_yet": "Aún no hay perfiles.",
        "unassigned_row_help": "Anuncios guardados sin búsqueda asociada — véase la nota de abajo.",
        "hidden_panel_title": "Ocultar afecta a la pantalla, no a los datos.",
        "hidden_panel_body": "Una suscripción oculta conserva todos sus anuncios y sigue recibiendo sus propios correos de alerta — simplemente no se ofrece en las vistas de anuncios: sin ficha, sin entrada en el menú de suscripciones, tampoco bajo Archivo, y sus anuncios quedan fuera de «todas las suscripciones», del mapa y de la exportación CSV. Siguen siendo accesibles por enlace directo (/properties?profile_id=<id>) y desde el recuento de esta tabla, y Mostrar lo devuelve todo. Activa es otra cuestión: una suscripción inactiva queda archivada, y así se sigue ofreciendo a un clic.",
        "profiles_routing_note": "Los correos de Idealista se enrutan por el enlace de búsqueda guardada que llevan (issue #102); un correo sin enlace reconocible recurre al nombre de la búsqueda, a los patrones regex opcionales y, por último, al perfil predeterminado.",
        "profiles_unassigned_title": "Un anuncio puede acabar sin ninguna suscripción.",
        "profiles_unassigned_body": "Ocurre cuando un correo enlaza a varias búsquedas guardadas distintas (elegir una sería un error silencioso), cuando se leyó un enlace pero no se pudo resolver su perfil, o cuando se elimina un perfil — sus anuncios se conservan y quedan desligados en vez de borrarse. Se cuentan en la fila Sin suscripción de arriba y se listan en esa entrada de la lista de anuncios. No hay ningún control para asignarlos a mano: la ingesta es la única que escribe la suscripción de un anuncio (issue #130), así que una fila está aquí precisamente porque su correo no nombraba una sola búsqueda guardada.",
        "profiles_unassigned_link": "esa entrada de la lista de anuncios",
        "profile_hidden_flash": "«%s» queda oculta en las vistas de anuncios",
        "profile_shown_flash": "«%s» vuelve a mostrarse",
        "profile_default_cannot_hide": "El perfil predeterminado recibe todos los correos que no coinciden con nada más, por eso no se puede ocultar. Haz predeterminado otro perfil primero.",
        # Property list
        "property_overview": "Resumen de Propiedades",
        "total_properties": "Total de Propiedades",
        "average_score": "Puntuación Promedio",
        "price_range": "Rango de Precios",
        "filters": "Filtros",
        "filters_search": "Filtros y Búsqueda",
        "search": "Buscar",
        "search_properties": "Buscar, o pegar la URL del anuncio...",
        "search_read_as_listing": "Interpretado como el anuncio %s de Idealista: aquí no hay ningún anuncio con ese id.",
        "search_read_as_link": "Interpretado como un enlace de anuncio (%s): aquí no hay ningún anuncio con ese enlace.",
        "land_type_all": "Todos los Tipos",
        "developed": "Desarrollado",
        "buildable": "Construible",
        "all_municipalities": "Todos los Municipios",
        "sea_view": "Vista al mar",
        "sea_view_any": "Vista al mar: cualquiera",
        "sea_view_confirmed": "Vista al mar: confirmada",
        "sea_view_yes_or_likely": "Vista al mar: confirmada o probable",
        "sea_distance_filter": "Distancia al mar",
        "sea_distance_any": "Distancia al mar: cualquiera",
        "sea_distance_400": "Mar ≤ 400 m (~5 min a pie)",
        "sea_distance_800": "Mar ≤ 800 m (~10 min a pie)",
        "sea_distance_1600": "Mar ≤ 1,6 km (~20 min a pie)",
        "route_from_gijon": "Ruta desde Gijón (Google Maps)",
        "build_filter": "Edificabilidad",
        "build_any": "Edificable: todos",
        "build_solar": "Edificable: urbano/solar (ya)",
        "build_urbanizable": "Edificable: urbanizable (tras obras)",
        "build_claimed": "Edificable: según el vendedor",
        "build_classified": "Edificable: cualquier curado",
        "sea_view_state_yes": "Vista al mar",
        "sea_view_state_likely": "Vista al mar probable",
        "sea_view_state_likely_geometry": "El terreno permite vista al mar",
        "sea_view_state_likely_geometry_over_terrain": "Mar visible por encima del terreno cercano",
        "sea_view_state_no": "Sin vista al mar",
        "sea_view_state_unknown": "Vista al mar sin determinar",
        "sea_view_shoreline_hidden": "la orilla de abajo queda oculta a",
        "sea_view_open_water_at": "mar abierto visible a partir de",
        "plot_coast_ban": "Zona de prohibición costera",
        "plot_coast_ok": "Fuera de la franja costera",
        "plot_coast_unmeasured": "Distancia a la costa sin medir",
        "plot_coast_unmeasured_approximate": (
            "Distancia a la costa sin medir (ubicación aproximada)"
        ),
        "plot_coast_none_near": "Sin costa en el radio",
        "plot_class_check": "Clasificación sin verificar",
        "plot_pgou_warning": "¿Fuera de zonas urbanas del PGOU?",
        "plot_listing_conflict": "El anuncio se contradice",
        "plot_geocoding_failed": "Coordenada errónea",
        "plot_price_outlier": "Precio por m² muy por debajo de lo normal",
        "sea_view_not_computed": "Sin calcular todavía",
        "sea_view_target_link": "ver el punto medido",
        # Ver services/listing_verification.py.
        "listing_state_active": "Activo",
        "listing_state_unchecked": "Sin verificar",
        "listing_verified_tooltip": "Confirmado en Idealista",
        "listing_unchecked_tooltip": "Nunca verificado en el sitio de origen — es el valor por defecto de un anuncio nuevo",
        "listing_checked_ago": "comprobado hace %s d",
        "listing_status_coverage": "Estado del anuncio: %s de %s verificados en el sitio de origen",
        # La barra de filtros ha acotado el resultado y la línea del total lo
        # dice -- el primer %s es el total filtrado, el segundo las mismas
        # suscripciones sin la barra de filtros.
        "filter_bar_narrowing": "Filtros: se muestran %s de %s",
        "clear_filters_link": "quitar filtros",
        # Importación de anuncios por enlace (fotocasa).
        "import_listings": "Importar anuncios por enlace",
        "back_to_listings": "Volver a los anuncios",
        "import_paste_links": "Enlaces de anuncios de Fotocasa",
        "import_links_help": "Uno por línea. Solo páginas de anuncio de fotocasa.es — una página de resultados no contiene ningún anuncio que leer.",
        "import_destination": "Añadir a la suscripción",
        "import_choose_destination": "Elige una suscripción…",
        "import_destination_help": "Decide dónde aparece el anuncio y a qué comparables se une. Un anuncio sin suscripción no se muestra en /properties.",
        "import_no_destination": "Todas las suscripciones están ocultas, y una oculta no se ofrece aquí — la importación acabaría donde la página no la muestra.",
        "import_no_destination_link": "Volver a mostrar una",
        "import_read_pages": "Leer las páginas",
        "import_reading": "Leyendo las páginas. Todavía no se guarda nada — esta página se actualiza al terminar.",
        "import_nothing_read": "Esa importación no leyó nada. Vuelve a pegar los enlaces.",
        "import_preview": "Lo que decían las páginas",
        "import_preview_counts": "%s nuevos de %s enlaces",
        "import_row_new": "Nuevo",
        "import_row_duplicate": "Ya está",
        "import_row_failed": "No leído",
        "import_add_n": "Añadir %s anuncio(s)",
        "import_approximate_help": "Fotocasa declara esta coordenada inexacta, por eso no se miden tiempos de viaje desde ella y las distancias llevan margen.",
        "approximate": "aproximada",
        "coordinates": "Coordenadas",
        "status": "Estado",
        "source": "Origen",
        "source_all": "Todos los orígenes",
        "listing_status_coverage_tooltip": "El resto conserva el estado con el que se importó, que nadie confirmó. Idealista bloquea el comprobador automático desde esta máquina; fotocasa sí responde.",
        "advertiser": "Vendedor",
        "advertiser_all": "Vendedor: todos",
        "advertiser_owner": "Particular",
        "advertiser_agency": "Inmobiliaria",
        "advertiser_unknown": "El origen no lo indica",
        "advertiser_unchecked": "Sin determinar",
        "advertiser_source_alert_campaign": "Del correo de alerta de Idealista que trajo este anuncio.",
        "advertiser_source_portal_payload": "Leído de la página del anuncio.",
        "advertiser_source_manual": "Establecido a mano.",
        "advertiser_set_header": "Registrar quién vende",
        "advertiser_clear": "Borrar — usar la lectura calculada",
        "owner_verdict": "Decisión",
        "owner_verdict_all": "Decisión: todas",
        "owner_verdict_interested": "Interesa",
        "owner_verdict_waiting": "En espera",
        "owner_verdict_rejected": "Descartado",
        "owner_verdict_undecided": "Sin decidir",
        "next_action": "Siguiente paso",
        "next_action_all": "Paso: todos",
        "next_action_none": "Nada pendiente",
        "next_action_pending": "Pendiente",
        "next_action_overdue": "Vencido",
        "next_action_due": "para el",
        "next_action_due_on": "Fecha límite",
        "review_section_title": "Decisión y siguiente paso",
        "review_reason": "Por qué",
        "review_reason_placeholder": "Por qué esta decisión — la razón es lo que se lee después en lugar de volver a investigarlo",
        "review_next_action_placeholder": "Qué queda pendiente — «condiciones de edificabilidad por escrito»",
        "review_save": "Guardar",
        "review_clear_verdict": "Sin decidir",
        "review_no_verdict": "Todavía nadie ha decidido sobre este anuncio.",
        "review_decided_at": "Registrado",
        "review_history_out_of_sync": "La decisión guardada no coincide con la última entrada del registro — algo la escribió fuera de esta página.",
        "review_overdue_link_one": "%s vencido",
        "review_overdue_link_other": "%s vencidos",
        "cadastre_section_title": "Parcela catastral",
        "cadastre_reference": "Referencia catastral",
        "cadastre_reference_placeholder": "20 caracteres de la ficha catastral — 33016A003001530001HQ",
        "cadastre_fetch": "Consultar el Catastro",
        "cadastre_clear": "Borrar",
        "cadastre_none": "Sin parcela registrada. Pega la referencia de la ficha catastral y se descarga el contorno del Catastro — gratis y sin clave.",
        "cadastre_not_measured": "La referencia está registrada; la parcela aún no se ha medido.",
        "cadastre_class": "Clase",
        "cadastre_use": "Uso",
        "cadastre_poligono": "Polígono / parcela",
        "cadastre_paraje": "Paraje",
        "cadastre_area": "Superficie catastral",
        "cadastre_shape": "Forma",
        "cadastre_bbox": "Rectángulo envolvente",
        "cadastre_fill_ratio": "Ocupa",
        "cadastre_compactness": "Compacidad",
        "cadastre_compactness_hint": "Polsby-Popper: un círculo es 1,00 y un cuadrado 0,79. Por debajo de 0,4 la parcela es alargada, quebrada o tiene cuello.",
        "cadastre_subparcels": "Subparcelas",
        "cadastre_viewer": "Abrir en el visor del Catastro",
        "cadastre_measured_in": "medido en EPSG:%s",
        "cadastre_crs_not_recorded": "no se registró la proyección en la que se midió",
        "cadastre_source_metric_geometry": "Contorno",
        "cadastre_source_map_geometry": "Contorno para el mapa",
        "cadastre_source_attributes": "Datos de la parcela",
        "cadastre_state_ok": "medido",
        "cadastre_state_not_found": "no existe esa parcela",
        "cadastre_state_refused": "el Catastro rechazó la consulta",
        "cadastre_state_unavailable": "no se pudo conectar",
        "cadastre_state_malformed": "respuesta ilegible",
        "cadastre_state_not_applicable": "no aplicable",
        "cadastre_state_unsupported_metric_crs": "fuera de las zonas que mide el Catastro",
        "cadastre_kept_previous": "conservado de una consulta anterior",
        "timeline_title": "Qué ha pasado",
        "timeline_empty": "Todavía no hay nada. Aquí van las notas y los contactos con la agencia.",
        "timeline_add_note": "Nota",
        "timeline_add_contact": "Contacto",
        "timeline_when": "Cuándo",
        "timeline_channel": "Canal",
        "timeline_counterpart": "Con quién",
        "timeline_counterpart_placeholder": "David Villa, Sellmi",
        "timeline_asked": "Se preguntó",
        "timeline_asked_placeholder": "Qué se preguntó — «¿tienen la ficha catastral?»",
        "timeline_answer": "Respuesta",
        "timeline_answer_placeholder": "Qué contestaron — lo que es de oídas se anota como tal",
        "timeline_note_placeholder": "Lo que no merezca la pena investigar dos veces",
        "timeline_add": "Añadir",
        "timeline_more_fields": "Contacto con la agencia, fecha, archivo…",
        "timeline_save": "Guardar",
        "timeline_delete": "Quitar",
        "timeline_edited": "editado",
        "timeline_verdict_entry": "Decisión registrada",
        "timeline_verdict_readonly": "Esto es el registro de una decisión — se cambia en el bloque de arriba.",
        "channel_whatsapp_label": "WhatsApp",
        "channel_email_label": "Correo",
        "channel_portal_label": "Chat del portal",
        "channel_phone_label": "Teléfono",
        "channel_visit_label": "Visita",
        "channel_other_label": "Otro",
        "attachment_add": "Adjuntar un archivo",
        "attachment_hint": "PDF o foto, hasta 25 MB. El tipo se lee del propio archivo, no de su nombre.",
        "attachment_remove": "Quitar este archivo",
        "attachment_refused": "El archivo fue rechazado",
        "favorites": "Favoritos",
        "hide_removed": "Ocultar retirados",
        "price": "Precio",
        "area": "Área",
        "beach_distance": "Distancia a Playa",
        "date_added": "Fecha Agregada",
        "sort_by": "Ordenar por",
        "apply": "Aplicar",
        "clear": "Limpiar",
        "export_csv": "Exportar CSV",
        "properties_found": "propiedades encontradas",
        "last_sync": "Última sincronización",
        "loading": "Cargando",
        # Table headers
        "title": "TÍTULO",
        "coords": "COORDENADAS",
        "beach": "PLAYA",
        "travel": "VIAJE",
        "type": "TIPO",
        "actions": "ACCIONES",
        "municipality": "Municipio",
        "land_type": "Tipo de Terreno",
        "legal_status": "Estado Legal",
        "score": "Puntuación",
        "view_details": "Ver Detalles",
        "clear_filters": "Limpiar Filtros",
        # Property details
        "property_description": "Descripción de la Propiedad",
        "key_highlights": "Aspectos Destacados",
        "location": "Ubicación",
        "view_on_maps": "Google Maps",
        "view_on_idealista": "Idealista",
        "view_on_app_map": "Nuestro mapa",
        "travel_times_distances": "Tiempos de Viaje y Distancias",
        "distance_to_sea": "Distancia al mar",
        "sea_distance_no_coastline": "Sin costa en",
        "sea_distance_unavailable": "Datos de costa no disponibles",
        "sea_distance_not_measured": "Aún no medido",
        "sea_distance_approximate_origin": "Sin medir (ubicación aproximada)",
        "approximate_origin_short": "Ubicación aproximada",
        "approximate_origin_tooltip": (
            "La coordenada de este anuncio es el centro de la localidad, no su "
            "dirección. Las distancias y los tiempos medidos desde ese punto "
            "describen la localidad, no esta propiedad."
        ),
        "approximate_origin_notice": (
            "La coordenada de este anuncio es el centro de la localidad, no su "
            "dirección: los tiempos de abajo son rutas desde ese punto, no "
            "desde esta propiedad. No cuentan para su puntuación."
        ),
        "not_located_short": "Sin ubicar",
        "not_located_tooltip": (
            "Este anuncio no tiene coordenada, así que no se ha medido nada: "
            "ni tiempos de viaje ni playas. Pulsa Enriquecer para "
            "geocodificarlo primero."
        ),
        "not_located_notice": (
            "Este anuncio todavía no tiene coordenada, así que nada de lo de "
            "abajo se ha medido: los tiempos de viaje, las playas y la "
            "distancia al mar parten de un punto que esta fila no tiene. "
            "Enriquecer lo geocodifica."
        ),
        # Which engine measured the drive times on this row. Recorded
        # per run, never inferred from the configuration.
        "routing_engine_osrm": "Tiempos de conducción del motor de rutas local (OSRM)",
        "routing_engine_google_distance_matrix": "Tiempos de conducción de Google Distance Matrix",
        "from_the_geocoded_point": "desde el punto geocodificado",
        "shared_coordinate_with": "misma coordenada que",
        "shared_coordinate_tooltip": (
            "Otro anuncio está guardado exactamente en este punto. Dos "
            "viviendas de un mismo edificio pueden compartirlo; dos parcelas "
            "distintas no."
        ),
        "shared_coordinate_more": "y %s más",
        "beaches_within": "Playas ≤",
        "minutes_drive": "min en coche",
        "nearest_beach": "Playa más cercana",
        "beaches_not_measured": "No medido",
        # List-row beach line: three states kept apart per #98.
        "beach_short": "PLAYA",
        "next_beach": "siguiente",
        "beach_none_within": "sin playa ≤",
        "beach_none_within_tooltip": "Medido: ninguna playa dentro del límite en coche",
        "beach_not_measured_tooltip": "La búsqueda de playas no respondió — no medido",
        "more_beaches_within": "más ≤",
        "nearest_airport": "Aeropuerto Más Cercano",
        "train_station": "Estación de Tren",
        "hospital": "Hospital",
        "basic_infrastructure": "Infraestructura Básica",
        "extended_infrastructure": "Infraestructura Extendida",
        "transport": "Transporte",
        "environment": "Entorno",
        "services_quality": "Calidad de Servicios",
        "information": "Información",
        "added": "Añadido",
        "email_received": "Correo Recibido",
        "ai_analysis": "Análisis IA",
        "enhance_description": "Mejorar Descripción",
        "enrich_google_api": "Enriquecer con Google API",
        # Scoring
        "dual_scoring_analysis": "Análisis de Puntuación Dual",
        # Detail-page infographic (proposal D6-D9, approved 2026-08-13).
        "score_band_strong": "Fuerte",
        "score_band_moderate": "Media",
        "score_band_weak": "Débil",
        "component_value": "Precio",
        "component_size": "Tamaño",
        "component_travel": "Viajes",
        "component_sea": "Mar",
        "component_pool": "Piscina",
        "component_hazard": "Vecindad industrial",
        "component_not_measured": "no medido — excluido",
        "weight_renormalized_tooltip": "Peso efectivo tras excluir componentes no medidos",
        "combined_score": "Combinada",
        "score_coverage_tooltip": "Criterios medidos / activos; parte del peso activo en la que se apoya la puntuación",
        "score_coverage_line": "La puntuación se apoya en el {share}% del peso activo ({measured} de {enabled} criterios medidos)",
        "score_coverage_missing": "sin medir",
        "measured_filter": "Criterios medidos",
        "measured_any": "Medidos: cualquiera",
        "measured_full": "Medidos: todos",
        "show_original_email": "Mostrar el texto original del correo",
        "no_photos": "Sin fotos",
        "no_photos_tooltip": "Las alertas de correo no llevan imágenes; obtener fotos es una decisión aparte",
        "route_from_property": "Ruta desde esta propiedad",
        # Vecindad industrial (#437).
        "hazards_title": "Vecindad industrial",
        "hazards_badge": "Industria cerca",
        "hazards_badge_centroid": "Industria cerca de la localidad",
        "hazards_incomplete_badge": "Exploración incompleta",
        "hazards_badge_tooltip": "OpenStreetMap registra una instalación industrial o de residuos cerca de este anuncio",
        "hazards_none": "Nada reconocido dentro del radio explorado",
        "hazards_none_incomplete": "Nada reconocido entre los elementos que vio esta exploración — y no los vio todos",
        "hazards_unavailable": "Sin medir — OpenStreetMap rechazó la consulta",
        "hazards_no_coordinates": "Sin medir — este anuncio no tiene coordenada",
        "hazards_missing": "Todavía sin explorar",
        "hazards_stale_origin": "Sin medir para esta coordenada — el anuncio se ha reubicado desde la exploración",
        "hazards_not_measured": "Sin medir",
        "hazards_coverage": "Vecindad industrial: %s de %s anuncios con exploración completa",
        "hazards_coverage_tooltip": "El resto nunca se exploró, o su exploración quedó incompleta. Un anuncio con exploración completa puede haberse reubicado desde entonces — la página del anuncio lo dice. Un anuncio sin exploración no es un anuncio sin nada cerca.",
        "hazards_searched": "Explorados %s km alrededor de la coordenada guardada",
        "hazards_guaranteed": "garantizado dentro de %s km de la parcela",
        "hazards_approximate": "La coordenada es el centroide de la localidad, así que cada distancia es un rango, no un punto",
        "hazards_straight_line": "Distancia en línea recta desde la coordenada guardada; el aire y el ruido van en línea recta, una carretera no",
        "hazards_bearing": "rumbo",
        "hazards_elements_one": "%s elemento de OpenStreetMap",
        "hazards_elements_other": "%s elementos de OpenStreetMap",
        "hazards_more_one": "%s instalación más sin mostrar",
        "hazards_more_other": "%s instalaciones más sin mostrar",
        "hazards_truncated": "La exploración alcanzó su límite de elementos, así que esta lista puede estar incompleta",
        "hazards_limits": "OpenStreetMap dice que la instalación existe. No dice nada sobre sus emisiones (PRTR-España las publica), nada sobre la calidad del aire medida y nada sobre una planta aprobada pero aún no construida. Tampoco se registra aquí de dónde viene el viento — solo el rumbo.",
        "hazard_severity_high": "Industria pesada",
        "hazard_severity_moderate": "Molestia",
        "hazard_kind_cement_works": "Fábrica de cemento",
        "hazard_kind_steelworks": "Acería",
        "hazard_kind_refinery": "Refinería",
        "hazard_kind_chemical_works": "Planta química",
        "hazard_kind_power_plant": "Central térmica",
        "hazard_kind_nuclear_plant": "Central nuclear",
        "hazard_kind_lng_terminal": "Terminal de GNL",
        "hazard_kind_lpg_storage": "Depósito de GLP",
        "hazard_kind_fuel_depot": "Depósito de combustible",
        "hazard_kind_coal_yard": "Parque de carbones",
        "hazard_kind_incinerator": "Incineradora",
        "hazard_kind_paper_mill": "Papelera",
        "hazard_kind_foundry": "Fundición",
        "hazard_kind_smelter": "Metalurgia de fundición",
        "hazard_kind_glassworks": "Vidriería",
        "hazard_kind_coking_plant": "Coquería",
        "hazard_kind_tannery": "Curtiduría",
        "hazard_kind_asphalt_plant": "Planta asfáltica",
        "hazard_kind_concrete_plant": "Planta de hormigón",
        "hazard_kind_slaughterhouse": "Matadero",
        "hazard_kind_landfill": "Vertedero",
        "hazard_kind_quarry": "Cantera",
        "hazard_kind_mine": "Mina",
        "hazard_kind_port_industry": "Puerto industrial",
        "hazard_kind_wastewater_plant": "Depuradora",
        "hazard_kind_waste_transfer": "Estación de transferencia de residuos",
        "hazard_kind_combustion_stack": "Chimenea industrial",
        # Quality-of-life card (Phase 2 slice).
        "qol_title": "Calidad de Vida",
        "qol_municipality_context": "Municipio",
        "qol_renta": "Renta neta / persona",
        "qol_renta_tooltip": "INE ADRH, renta neta media por persona",
        "qol_vs_province_tooltip": "Índice sobre la mediana provincial = 100",
        "qol_population": "Población",
        "qol_population_tooltip": "INE Padrón",
        "qol_population_5y": "Variación en 5 años",
        "qol_municipality_not_matched": "Municipio sin correspondencia en datos INE",
        "qol_no_reference_data": "Datos de referencia aún no importados",
        "qol_supermarkets": "Supermercados",
        "qol_straight_line": "línea recta",
        "qol_unnamed_shop": "Tienda sin nombre",
        "amenity_unnamed": "Sin nombre",
        "qol_convenience_tooltip": "OSM shop=convenience — tienda pequeña local",
        "qol_osm_empty": "Ninguno en OSM en 12 km — cobertura no garantizada",
        "qol_not_measured": "No medido",
        "qol_no_municipality": "Sin municipio registrado para este anuncio",
        "qol_no_coordinates": "Sin coordenadas — no medido",
        "qol_hospitals": "Hospitales",
        # La añada vive en el campo `source` del propio bloque, no aquí.
        "qol_hospital_grouping_note": (
            "Agrupación local derivada de campos del Catálogo Nacional de "
            "Hospitales (camas / acreditación docente / alta tecnología) — "
            "no es un nivel oficial"
        ),
        "qol_hospital_outside_coverage": (
            "Fuera de la cobertura del catálogo de hospitales (Asturias + Galicia)"
        ),
        "qol_hospital_teaching": "Docente / alta tecnología",
        "qol_hospital_general": "General de agudos",
        "qol_hospital_fields": "camas / docente / alta tecnología",
        # Pool criterion (proposal D17).
        "pool_title": "Piscina",
        "pool_unnamed": "Piscina sin nombre",
        "pool_indoor": "cubierta",
        "pool_indoor_likely": "¿cubierta?",
        "pool_unverified_absence": "Sin piscina verificada cerca — no se puntúa",
        "pool_unverified_tooltip": (
            "OSM no encontró ninguna y una comprobación en Places coincidió; "
            "una sola consulta no prueba exhaustividad, así que nunca es un 0"
        ),
        # Municipality comparison page (proposal D22). "municipality" itself
        # is already defined above with the table headers.
        "municipalities": "Municipios",
        "construccion": "Normas de construcción",
        "agencies": "Agencias",
        "agencies_title": "Top agencias — casas independientes ≤ 300.000 €, Asturias y Cantabria",
        "agencies_measured_on": "Medido el",
        "agencies_definition": (
            "Casa independiente = chalet independiente + casa rústica de idealista "
            "(casa de pueblo / rural / casona); adosados y pareados quedan fuera. "
            "Ordenado por el recuento de idealista; al lado, la cifra solo de chalets "
            "independientes. En fotocasa se cuentan Casa o chalet + Finca rústica con "
            "el mismo tope de precio. Cada recuento enlaza a la página filtrada de la que se leyó."
        ),
        "agencies_col_agency": "Agencia",
        "agencies_col_website": "Web",
        "agencies_col_idealista": "idealista · independientes ≤ 300k €",
        "agencies_col_fotocasa": "fotocasa · casa/chalet + finca ≤ 300k €",
        "agencies_col_reviews": "Opiniones",
        "agencies_col_description": "Descripción",
        "agencies_founded": "Fundada",
        "agencies_independientes": "de ellas chalets independientes",
        "agencies_all_houses": "todas las casas y chalets",
        "agencies_method": (
            "Método: cada cuenta de agencia de los directorios de idealista de ambas provincias "
            "se midió en su propio microsite con los filtros aplicados; las cifras de los portales "
            "cambian a diario y un anuncio puede estar en varios portales, así que no se suman."
        ),
        "agencies_no_data": (
            "La tabla de agencias no está disponible: falta data/top_agencies.json o no se puede leer."
        ),
        "concejo_select_label": "Concejo:",
        "concejo_none_selected_option": "— sin seleccionar —",
        "concejo_scope_group": "Perímetro de búsqueda",
        "concejo_all_group": "Toda Asturias",
        "concejo_snapshot_missing": (
            "Falta la instantánea de identidad de concejos — la página se "
            "niega antes que servir la identidad desde otra fuente."
        ),
        "concejo_code_rejected": "Código de concejo no reconocido — sin capa municipal.",
        "concejo_coverage_line": (
            "Perímetro: {scope} concejos. Búsqueda realizada en {searched}. "
            "Todos los temas obligatorios buscados en {mandatory} "
            "(valores confirmados: {confirmed}, de ellos caducos: {stale})."
        ),
        "concejo_beyond_scope": "Investigados además fuera del perímetro: {n}.",
        "concejo_not_researched_banner": (
            "nadie ha investigado este municipio. Ninguno de los campos "
            "siguientes está comprobado para él."
        ),
        "concejo_regional_norm": "Norma regional",
        "concejo_pick_first": (
            "concejo sin seleccionar — condiciones municipales sin comprobar"
        ),
        "concejo_for_name": "Para {name}",
        "concejo_not_researched": "sin investigar",
        "concejo_not_confirmed": "buscado, sin confirmar",
        "concejo_searched_in": "Buscado en",
        "concejo_agent_unverified": "sin verificar",
        "concejo_stale": "conviene recomprobar",
        "concejo_chapter_callout": ("Marco regional. Sin confirmar para {name}:"),
        "concejo_chapter_callout_none": (
            "Marco regional. Sin concejo seleccionado — las condiciones "
            "municipales no están comprobadas para ningún lugar."
        ),
        "concejo_topic_pgo": "PGO aprobado, y cuándo",
        "concejo_topic_cedula": "Cédula o solo certificado",
        "concejo_topic_coastal": "Concejo litoral (POLA/PESC)",
        "concejo_topic_silence": "Plazo de silencio de la licencia",
        "concejo_topic_occupation": "Régimen de primera ocupación",
        "concejo_topic_icio": "Tipo de ICIO",
        "construccion_full_title": "Dossier completo (regional)",
        "construccion_full_link": "Dossier completo",
        "construccion_full_back": "Volver a la referencia",
        "construccion_full_note": (
            "Este es el documento regional íntegro. No lleva contexto de "
            "concejo: nada de esta página está comprobado para ningún "
            "municipio — las condiciones municipales viven en la referencia."
        ),
        "construccion_full_missing": (
            "Falta el fichero del dossier completo en esta build."
        ),
        "listings_lower": "anuncios",
        "municipalities_listings": "Anuncios",
        "municipalities_score": "Puntuación",
        "municipalities_index": "vs prov.",
        "municipalities_unemployment": "Paro (proxy)",
        "municipalities_unemployment_tooltip": (
            "Parados registrados SEPE / población — una razón comparable, "
            "no la tasa oficial de paro"
        ),
        "municipalities_facts_label": "Datos del municipio",
        "municipalities_facts_note": "Cifras del INE y del SEPE para el municipio",
        "municipalities_medians_label": "Medianas de los anuncios",
        "municipalities_medians_note": (
            "mediana sobre los anuncios de este municipio; el número entre "
            "paréntesis es cuántos se midieron"
        ),
        "municipalities_coverage": "Anuncios medidos",
        "municipalities_none_measured": "Aún sin mediciones",
        "municipalities_archived": "Incluir archivados",
        "municipalities_archived_tooltip": "Contar también retirados y vendidos",
        "municipalities_pool_indoor_tooltip": "Mediana a la piscina más cercana con indicios de cubierta",
        "municipalities_unnamed_note": "anuncios no llevan municipio y no se comparan",
        "municipalities_unassigned_listings_one": "%s anuncio sin suscripción",
        "municipalities_unassigned_listings_other": "%s anuncios sin suscripción",
        "municipalities_scope_suffix": (
            "cada fila abre exactamente los anuncios que hay detrás de su número"
        ),
        "municipalities_drilldown_truncated": (
            "Este municipio lo sostienen más suscripciones de las que un enlace "
            "puede nombrar, así que su lista muestra menos anuncios que este recuento"
        ),
        "municipalities_empty": "Aún no hay anuncios que comparar.",
        "municipalities_scope_label": "Alcance",
        "municipalities_scope_control": "Suscripciones",
        "municipalities_scope_every": "todas",
        "municipalities_scope_live_only": "solo activas",
        "municipalities_scope_selected": "solo las suscripciones seleccionadas arriba",
        "municipalities_scope_note": (
            "todos los anuncios almacenados, sea cual sea su suscripción"
        ),
        "municipalities_scope_live_one": "%s activa",
        "municipalities_scope_live_other": "%s activas",
        "municipalities_scope_retired_one": "%s retirada",
        "municipalities_scope_retired_other": "%s retiradas",
        "municipalities_scope_hidden_one": "%s oculta",
        "municipalities_scope_hidden_other": "%s ocultas",
        "municipalities_scope_unknown_one": "%s cuya suscripción ya no existe",
        "municipalities_scope_unknown_other": "%s cuyas suscripciones ya no existen",
        "municipalities_scope_delisted_excluded": "sin retirados ni vendidos",
        "municipalities_scope_delisted_included": "con retirados y vendidos",
        "municipalities_scope_favorites": "solo favoritos",
        "municipalities_basis_label": "Base",
        "municipalities_basis_note": (
            "\u20ac/m\u00b2 es la mediana sin ajustar del precio pedido \u00f7 superficie "
            "sobre todos los tipos, subtipos y tamaños \u2014 describe la composición del "
            "inventario, no un precio del municipio ajustado por tamaño"
        ),
        "pool_unroutable": "sin ruta por carretera",
        "pool_unroutable_tooltip": "Google respondió: sin ruta en coche a esta piscina",
        "pool_owner_absence": "Confirmado por el propietario: sin piscina útil",
        "pool_owner_absence_set": "Confirmar: aquí no hay piscina útil",
        "pool_owner_absence_clear": "Quitar el veredicto de sin piscina",
        "pool_owner_absence_tooltip": (
            "La única vía para que la puntuación de piscina sea 0 — "
            "la ausencia calculada nunca llega tan lejos"
        ),
        "investment_score": "Puntuación de Inversión",
        "lifestyle_score": "Puntuación de Estilo de Vida",
        "criteria_breakdown": "Desglose de Criterios",
        "score_composition": "Composición de Puntuación",
        "investment_analysis": "Análisis de Inversión",
        "rental_market": "Mercado de Alquiler",
        "monthly_rent": "Alquiler Mensual",
        "rental_yield": "Rendimiento de Alquiler",
        "cap_rate": "Tasa de Capitalización",
        "investment_rating": "Calificación de Inversión",
        "risk_level": "Nivel de Riesgo",
        "market_position": "Posición en el Mercado",
        "development_cost": "Costo de Desarrollo",
        "total_investment": "Inversión Total",
        "cost_per_m2": "Costo por m²",
        # Criteria page
        "investment_profile": "Perfil de Inversión",
        "lifestyle_profile": "Perfil de Estilo de Vida",
        "save_changes": "Guardar Cambios",
        "weight": "Peso",
        # Messages
        "running_gmail_ingestion": "Ejecutando ingesta de Gmail...",
        "enrichment_in_progress": "Enriquecimiento en progreso...",
        "analysis_complete": "Análisis completado",
        # Common
        "yes": "Sí",
        "no": "No",
        "unknown": "Desconocido",
        "save": "Guardar",
        "cancel": "Cancelar",
        "edit": "Editar",
        "close": "Cerrar",
        "back_to_properties": "Volver a Propiedades",
    },
}


def get_current_language():
    """Get current language from session"""
    return session.get("language", "en")


def set_language(lang_code):
    """Set language in session"""
    if lang_code in TRANSLATIONS:
        session["language"] = lang_code
        return True
    return False


def t(key, lang=None):
    """Translate a key to current language"""
    if lang is None:
        lang = get_current_language()

    return TRANSLATIONS.get(lang, {}).get(key, TRANSLATIONS["en"].get(key, key))


def tn(key, count, lang=None):
    """Translate a counted phrase: `<key>_one` for 1, `<key>_other` otherwise.

    The smallest thing that makes a count correct in the two languages this UI
    speaks. Both put the boundary at exactly one, and Spanish inflects the
    adjective along with the noun -- "1 suscripcion oculta" against "2
    suscripciones ocultas" -- so a single string with a number substituted into
    it cannot be written correctly there at all. That is the whole reason this
    exists; it is not a general plural library, and a language with more forms
    (Russian, Polish) would need a real one rather than a third branch here.

    The count is substituted for a single `%s`, so a phrase carrying no
    placeholder is returned as written. A missing pair falls back through
    `t()`, which answers with the English form and then with the key itself --
    a visible key beats a page that raises.
    """
    suffix = "_one" if count == 1 else "_other"
    phrase = t(f"{key}{suffix}", lang)
    if "%s" not in phrase:
        return phrase
    return phrase % count


def get_browser_language():
    """Get preferred language from browser headers"""
    if request and hasattr(request, "accept_languages"):
        best_match = request.accept_languages.best_match(["en", "es"])
        return best_match or "en"
    return "en"
