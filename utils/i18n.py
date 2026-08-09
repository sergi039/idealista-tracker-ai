"""
Internationalization utilities for the application
"""

from flask import session, request

TRANSLATIONS = {
    "en": {
        # Navigation
        "app_title": "Idealista Tracker AI",
        "properties": "Properties",
        "scoring_criteria": "Scoring Criteria",
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
        "sea_view_any": "Any",
        "sea_view_confirmed": "Confirmed",
        "sea_view_yes_or_likely": "Confirmed or likely",
        "sea_view_state_yes": "Sea view",
        "sea_view_state_likely": "Sea view likely",
        "sea_view_state_no": "No sea view",
        "sea_view_state_unknown": "Sea view unknown",
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
        "scoring_criteria": "Criterios de Puntuación",
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
        "sea_view_any": "Cualquiera",
        "sea_view_confirmed": "Confirmada",
        "sea_view_yes_or_likely": "Confirmada o probable",
        "sea_view_state_yes": "Vista al mar",
        "sea_view_state_likely": "Vista al mar probable",
        "sea_view_state_no": "Sin vista al mar",
        "sea_view_state_unknown": "Vista al mar sin determinar",
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
