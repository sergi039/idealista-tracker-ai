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
        # Property list
        "property_overview": "Property Overview",
        "total_properties": "Total Properties",
        "average_score": "Average Score",
        "price_range": "Price Range",
        "filters": "Filters",
        "filters_search": "Filters & Search",
        "search": "Search",
        "search_properties": "Search properties...",
        "land_type_all": "All Types",
        "developed": "Developed",
        "buildable": "Buildable",
        "all_municipalities": "All Municipalities",
        # Four states, not a flag -- see services/sea_view_service.py.
        "sea_view": "Sea view",
        "sea_view_any": "Sea view: any",
        "sea_view_confirmed": "Sea view: confirmed",
        "sea_view_yes_or_likely": "Sea view: confirmed or likely",
        "sea_view_state_yes": "Sea view",
        "sea_view_state_likely": "Sea view likely",
        # A `likely` that rests on bare-earth terrain alone. It says the ground
        # does not block the line, not that the listing has a sea view -- and
        # what the line reaches can be an estuary channel rather than open
        # water (#334). Named for what was computed; see state_label_key().
        "sea_view_state_likely_geometry": "Terrain allows a sea view",
        "sea_view_state_no": "No sea view",
        "sea_view_state_unknown": "Sea view unknown",
        # A plot inside the 500 m coastal band cannot carry a dwelling at all
        # (POLA/PESC, which outranks the municipal PGOU), so the list states
        # the verdict rather than the raw metre count alone.
        "plot_coast_ban": "Coastal ban zone",
        "plot_coast_ok": "Outside coastal ban",
        "plot_coast_unmeasured": "Coast distance unmeasured",
        "plot_coast_none_near": "No coast within radius",
        "plot_class_check": "Classification unverified",
        "plot_pgou_warning": "Outside PGOU urban zones?",
        "plot_listing_conflict": "Listing contradicts itself",
        "plot_geocoding_failed": "Coordinate is wrong",
        # The coastline point the geometry was measured to, on a map: the one
        # thing that answers "what water is this?" without re-deriving it from
        # a distance and a bearing.
        "sea_view_target_link": "see the point measured to",
        # Shown instead of the literal source code "none": nobody computed a
        # verdict here, which is not the same as computing "no sea view".
        "sea_view_not_computed": "Not computed yet",
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
        "component_not_measured": "not measured — excluded",
        "weight_renormalized_tooltip": "Effective weight after excluding unmeasured components",
        "combined_score": "Combined",
        "show_original_email": "Show original email text",
        "no_photos": "No photos",
        "no_photos_tooltip": "The email alerts carry no images; photo sourcing is a separate decision",
        "route_from_property": "Route from this property",
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
        "municipalities_empty": "No listings to compare yet.",
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
        # Property list
        "property_overview": "Resumen de Propiedades",
        "total_properties": "Total de Propiedades",
        "average_score": "Puntuación Promedio",
        "price_range": "Rango de Precios",
        "filters": "Filtros",
        "filters_search": "Filtros y Búsqueda",
        "search": "Buscar",
        "search_properties": "Buscar propiedades...",
        "land_type_all": "Todos los Tipos",
        "developed": "Desarrollado",
        "buildable": "Construible",
        "all_municipalities": "Todos los Municipios",
        "sea_view": "Vista al mar",
        "sea_view_any": "Vista al mar: cualquiera",
        "sea_view_confirmed": "Vista al mar: confirmada",
        "sea_view_yes_or_likely": "Vista al mar: confirmada o probable",
        "sea_view_state_yes": "Vista al mar",
        "sea_view_state_likely": "Vista al mar probable",
        "sea_view_state_likely_geometry": "El terreno permite vista al mar",
        "sea_view_state_no": "Sin vista al mar",
        "sea_view_state_unknown": "Vista al mar sin determinar",
        "plot_coast_ban": "Zona de prohibición costera",
        "plot_coast_ok": "Fuera de la franja costera",
        "plot_coast_unmeasured": "Distancia a la costa sin medir",
        "plot_coast_none_near": "Sin costa en el radio",
        "plot_class_check": "Clasificación sin verificar",
        "plot_pgou_warning": "¿Fuera de zonas urbanas del PGOU?",
        "plot_listing_conflict": "El anuncio se contradice",
        "plot_geocoding_failed": "Coordenada errónea",
        "sea_view_not_computed": "Sin calcular todavía",
        "sea_view_target_link": "ver el punto medido",
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
        "component_not_measured": "no medido — excluido",
        "weight_renormalized_tooltip": "Peso efectivo tras excluir componentes no medidos",
        "combined_score": "Combinada",
        "show_original_email": "Mostrar el texto original del correo",
        "no_photos": "Sin fotos",
        "no_photos_tooltip": "Las alertas de correo no llevan imágenes; obtener fotos es una decisión aparte",
        "route_from_property": "Ruta desde esta propiedad",
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
        "municipalities_empty": "Aún no hay anuncios que comparar.",
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


def get_browser_language():
    """Get preferred language from browser headers"""
    if request and hasattr(request, "accept_languages"):
        best_match = request.accept_languages.best_match(["en", "es"])
        return best_match or "en"
    return "en"
