"""
Internationalization utilities for the application
"""
from flask import session, request

TRANSLATIONS = {
    'en': {
        # Navigation
        'app_title': 'Idealista Land Watch & Rank',
        'properties': 'Properties',
        'scoring_criteria': 'Scoring Criteria',
        'manual_sync': 'Manual Sync',
        
        # Property list
        'property_overview': 'Property Overview',
        'total_properties': 'Total Properties',
        'average_score': 'Average Score',
        'price_range': 'Price Range',
        'filters': 'Filters',
        'filters_search': 'Filters & Search',
        'search': 'Search',
        'search_properties': 'Search properties...',
        'land_type_all': 'All Types',
        'developed': 'Developed',
        'buildable': 'Buildable',
        'all_municipalities': 'All Municipalities',
        'sea_view_only': 'Sea View Only',
        'price': 'Price',
        'area': 'Area',
        'beach_distance': 'Beach Distance',
        'date_added': 'Date Added',
        'sort_by': 'Sort by',
        'apply': 'Apply',
        'clear': 'Clear',
        'export_csv': 'Export CSV',
        'properties_found': 'properties found',
        'last_sync': 'Last sync',
        'loading': 'Loading',
        
        # Table headers
        'score': 'SCORE',
        'title': 'TITLE',
        'coords': 'COORDS',
        'beach': 'Beach',
        'travel': 'TRAVEL',
        'type': 'TYPE',
        'added': 'ADDED',
        'actions': 'ACTIONS',
        'municipality': 'Municipality',
        'land_type': 'Land Type',
        'legal_status': 'Legal Status',
        'price': 'Price',
        'area': 'Area',
        'score': 'Score',
        'view_details': 'View Details',
        'clear_filters': 'Clear Filters',
        
        # Property details
        'property_description': 'Property Description',
        'key_highlights': 'Key Highlights',
        'location': 'Location',
        'view_on_maps': 'View on Maps',
        'view_on_idealista': 'View on Idealista',
        'travel_times_distances': 'Travel Times & Distances',
        'nearest_airport': 'Nearest Airport',
        'train_station': 'Train Station',
        'hospital': 'Hospital',
        'basic_infrastructure': 'Basic Infrastructure',
        'extended_infrastructure': 'Extended Infrastructure',
        'transport': 'Transport',
        'environment': 'Environment',
        'services_quality': 'Services Quality',
        'information': 'Information',
        'added': 'Added',
        'email_received': 'Email Received',
        'ai_analysis': 'AI Analysis',
        'enhance_description': 'Enhance Description',
        'enrich_google_api': 'Enrich with Google API',
        
        # Scoring
        'dual_scoring_analysis': 'Dual Scoring Analysis',
        'investment_score': 'Investment Score',
        'lifestyle_score': 'Lifestyle Score',
        'criteria_breakdown': 'Criteria Breakdown',
        'score_composition': 'Score Composition',
        'investment_analysis': 'Investment Analysis',
        'rental_market': 'Rental Market',
        'monthly_rent': 'Monthly Rent',
        'rental_yield': 'Rental Yield',
        'cap_rate': 'Cap Rate',
        'investment_rating': 'Investment Rating',
        'risk_level': 'Risk Level',
        'market_position': 'Market Position',
        'development_cost': 'Development Cost',
        'total_investment': 'Total Investment',
        'cost_per_m2': 'Cost per m²',
        
        # Criteria page
        'investment_profile': 'Investment Profile',
        'lifestyle_profile': 'Lifestyle Profile',
        'save_changes': 'Save Changes',
        'weight': 'Weight',
        
        # Messages
        'loading': 'Loading...',
        'running_gmail_ingestion': 'Running Gmail ingestion...',
        'enrichment_in_progress': 'Enrichment in progress...',
        'analysis_complete': 'Analysis complete',
        
        # Common
        'yes': 'Yes',
        'no': 'No',
        'unknown': 'Unknown',
        'save': 'Save',
        'cancel': 'Cancel',
        'edit': 'Edit',
        'close': 'Close',
        'back': 'Back',
        'back_to_properties': 'Back to Properties',
        
        # Languages and platforms
        'english': 'English',
        'spanish': 'Spanish',
        'idealista': 'Idealista',
        
        # Property details specific
        'property_details': 'Property Details',
        'ai_investment_analysis': 'AI Investment Analysis',
        'powered_by_claude_ai': 'Powered by Claude AI',
        'click_ai_analysis_button': 'Click "AI Analysis" button to generate investment insights',
        'property_scoring_analysis': 'Property Scoring Analysis',
        'multi_criteria_decision_making': 'Multi-Criteria Decision Making',
        'overall_score': 'Overall Score',
        'combined_analysis': 'Combined Analysis',
        'roi_focused': 'ROI Focused',
        'quality_of_life': 'Quality of Life',
        'view_detailed_breakdown': 'View Detailed Breakdown',
        'enriching_property_google_api': 'Enriching property with Google API data...',
        'scores': 'Scores',
        'investment': 'Investment',
        'lifestyle': 'Lifestyle',
        'nearby_amenities': 'Nearby Amenities',
        
        # Email and source
        'source_email': 'Source Email',
        'open_in_gmail': 'Open in Gmail',
        'find_in_gmail': 'Find in Gmail',
        'search_in_gmail': 'Search in Gmail',
        'subject': 'Subject',
        'from': 'From',
        'no_email_source': 'No email source available',
        
        # Score editing
        'edit_score': 'Edit Score',
        'manual_score_0_100': 'Manual Score (0-100)',
        'enter_score_description': 'Enter a score between 0 and 100. This will override the automatically calculated score.',
        'save_score': 'Save Score'
    },
    'es': {
        # Navigation
        'app_title': 'Idealista Land Watch & Rank',
        'properties': 'Propiedades',
        'scoring_criteria': 'Criterios de Puntuación',
        'manual_sync': 'Sincronización Manual',
        
        # Property list
        'property_overview': 'Resumen de Propiedades',
        'total_properties': 'Total de Propiedades',
        'average_score': 'Puntuación Promedio',
        'price_range': 'Rango de Precios',
        'filters': 'Filtros',
        'filters_search': 'Filtros y Búsqueda',
        'search': 'Buscar',
        'search_properties': 'Buscar propiedades...',
        'land_type_all': 'Todos los Tipos',
        'developed': 'Desarrollado',
        'buildable': 'Construible',
        'all_municipalities': 'Todos los Municipios',
        'sea_view_only': 'Solo Vista al Mar',
        'price': 'Precio',
        'area': 'Área',
        'beach_distance': 'Distancia a Playa',
        'date_added': 'Fecha Agregada',
        'sort_by': 'Ordenar por',
        'apply': 'Aplicar',
        'clear': 'Limpiar',
        'export_csv': 'Exportar CSV',
        'properties_found': 'propiedades encontradas',
        'last_sync': 'Última sincronización',
        'loading': 'Cargando',
        
        # Table headers
        'score': 'PUNTUACIÓN',
        'title': 'TÍTULO',
        'coords': 'COORDENADAS',
        'beach': 'Playa',
        'travel': 'VIAJE',
        'type': 'TIPO',
        'added': 'AGREGADO',
        'actions': 'ACCIONES',
        'municipality': 'Municipio',
        'land_type': 'Tipo de Terreno',
        'legal_status': 'Estado Legal',
        'price': 'Precio',
        'area': 'Área',
        'score': 'Puntuación',
        'view_details': 'Ver Detalles',
        'clear_filters': 'Limpiar Filtros',
        
        # Property details
        'property_description': 'Descripción de la Propiedad',
        'key_highlights': 'Aspectos Destacados',
        'location': 'Ubicación',
        'view_on_maps': 'Ver en Mapas',
        'view_on_idealista': 'Ver en Idealista',
        'travel_times_distances': 'Tiempos de Viaje y Distancias',
        'nearest_airport': 'Aeropuerto Más Cercano',
        'train_station': 'Estación de Tren',
        'hospital': 'Hospital',
        'basic_infrastructure': 'Infraestructura Básica',
        'extended_infrastructure': 'Infraestructura Extendida',
        'transport': 'Transporte',
        'environment': 'Entorno',
        'services_quality': 'Calidad de Servicios',
        'information': 'Información',
        'added': 'Añadido',
        'email_received': 'Correo Recibido',
        'ai_analysis': 'Análisis IA',
        'enhance_description': 'Mejorar Descripción',
        'enrich_google_api': 'Enriquecer con Google API',
        
        # Scoring
        'dual_scoring_analysis': 'Análisis de Puntuación Dual',
        'investment_score': 'Puntuación de Inversión',
        'lifestyle_score': 'Puntuación de Estilo de Vida',
        'criteria_breakdown': 'Desglose de Criterios',
        'score_composition': 'Composición de Puntuación',
        'investment_analysis': 'Análisis de Inversión',
        'rental_market': 'Mercado de Alquiler',
        'monthly_rent': 'Alquiler Mensual',
        'rental_yield': 'Rendimiento de Alquiler',
        'cap_rate': 'Tasa de Capitalización',
        'investment_rating': 'Calificación de Inversión',
        'risk_level': 'Nivel de Riesgo',
        'market_position': 'Posición en el Mercado',
        'development_cost': 'Costo de Desarrollo',
        'total_investment': 'Inversión Total',
        'cost_per_m2': 'Costo por m²',
        
        # Criteria page
        'investment_profile': 'Perfil de Inversión',
        'lifestyle_profile': 'Perfil de Estilo de Vida',
        'save_changes': 'Guardar Cambios',
        'weight': 'Peso',
        
        # Messages
        'loading': 'Cargando...',
        'running_gmail_ingestion': 'Ejecutando ingesta de Gmail...',
        'enrichment_in_progress': 'Enriquecimiento en progreso...',
        'analysis_complete': 'Análisis completado',
        
        # Common
        'yes': 'Sí',
        'no': 'No',
        'unknown': 'Desconocido',
        'save': 'Guardar',
        'cancel': 'Cancelar',
        'edit': 'Editar',
        'close': 'Cerrar',
        'back': 'Volver',
        'back_to_properties': 'Volver a Propiedades',
        
        # Languages and platforms
        'english': 'Inglés',
        'spanish': 'Español',
        'idealista': 'Idealista',
        
        # Property details specific
        'property_details': 'Detalles de la Propiedad',
        'ai_investment_analysis': 'Análisis de Inversión IA',
        'powered_by_claude_ai': 'Impulsado por Claude AI',
        'click_ai_analysis_button': 'Haz clic en "Análisis IA" para generar análisis de inversión',
        'property_scoring_analysis': 'Análisis de Puntuación de Propiedad',
        'multi_criteria_decision_making': 'Toma de Decisiones Multi-Criterio',
        'overall_score': 'Puntuación General',
        'combined_analysis': 'Análisis Combinado',
        'roi_focused': 'Enfocado en ROI',
        'quality_of_life': 'Calidad de Vida',
        'view_detailed_breakdown': 'Ver Desglose Detallado',
        'enriching_property_google_api': 'Enriqueciendo propiedad con datos de Google API...',
        'scores': 'Puntuaciones',
        'investment': 'Inversión',
        'lifestyle': 'Estilo de Vida',
        'nearby_amenities': 'Servicios Cercanos',
        
        # Email and source
        'source_email': 'Correo de Origen',
        'open_in_gmail': 'Abrir en Gmail',
        'find_in_gmail': 'Encontrar en Gmail',
        'search_in_gmail': 'Buscar en Gmail',
        'subject': 'Asunto',
        'from': 'De',
        'no_email_source': 'No hay fuente de correo disponible',
        
        # Score editing
        'edit_score': 'Editar Puntuación',
        'manual_score_0_100': 'Puntuación Manual (0-100)',
        'enter_score_description': 'Ingrese una puntuación entre 0 y 100. Esto anulará la puntuación calculada automáticamente.',
        'save_score': 'Guardar Puntuación'
    }
}

def get_current_language():
    """Get current language from session"""
    return session.get('language', 'en')

def set_language(lang_code):
    """Set language in session"""
    if lang_code in TRANSLATIONS:
        session['language'] = lang_code
        return True
    return False

def t(key, lang=None):
    """Translate a key to current language"""
    if lang is None:
        lang = get_current_language()
    
    return TRANSLATIONS.get(lang, {}).get(key, TRANSLATIONS['en'].get(key, key))

def get_browser_language():
    """Get preferred language from browser headers"""
    if request and hasattr(request, 'accept_languages'):
        best_match = request.accept_languages.best_match(['en', 'es'])
        return best_match or 'en'
    return 'en'

# Field name mappings for proper display
FIELD_MAPPINGS = {
    # Scoring criteria
    'investment_yield': 'Investment Yield',
    'location_quality': 'Location Quality',
    'transport': 'Transport',
    'infrastructure_basic': 'Basic Infrastructure',
    'infrastructure_extended': 'Extended Infrastructure',
    'environment': 'Environment',
    'physical_characteristics': 'Physical Characteristics',
    'services_quality': 'Services Quality',
    'legal_status': 'Legal Status',
    'development_potential': 'Development Potential',
    
    # Infrastructure and amenities
    'water_supply': 'Water Supply',
    'electricity': 'Electricity',
    'sewage_system': 'Sewage System',
    'gas_supply': 'Gas Supply',
    'internet_access': 'Internet Access',
    'road_access': 'Road Access',
    'public_transport': 'Public Transport',
    'parking_available': 'Parking Available',
    
    # Amenities
    'osm_amenities': 'Nearby Amenities',
    'bank': 'Bank',
    'hospital': 'Hospital',
    'pharmacy': 'Pharmacy',
    'police': 'Police',
    'school': 'School',
    'restaurant': 'Restaurant',
    'shopping': 'Shopping',
    'fuel': 'Fuel Station',
    'post_office': 'Post Office',
    
    # Environment and views
    'sea_view': 'Sea View',
    'mountain_view': 'Mountain View',
    'city_view': 'City View',
    'garden_view': 'Garden View',
    'natural_areas': 'Natural Areas',
    'noise_level': 'Noise Level',
    'air_quality': 'Air Quality',
    
    # Common fields  
    'price_per_m2': 'Price per m²',
    'land_type': 'Land Type',
    'legal_status': 'Legal Status',
    'building_permit': 'Building Permit',
    'plot_area': 'Plot Area',
    'built_area': 'Built Area',
    'construction_year': 'Construction Year',
    'renovation_needed': 'Renovation Needed',
    'energy_rating': 'Energy Rating',
    
    # Travel times and distances
    'travel_time_airport': 'Travel Time to Airport',
    'travel_time_train_station': 'Travel Time to Train Station',
    'travel_time_hospital': 'Travel Time to Hospital',
    'distance_airport': 'Distance to Airport',
    'distance_train_station': 'Distance to Train Station',
    'distance_hospital': 'Distance to Hospital',
    'distance_beach': 'Distance to Beach',
    'beach_distance': 'Beach Distance',
}

def format_field_name(field_name, lang=None):
    """
    Format a field name for display using proper mapping or fallback formatting
    
    Args:
        field_name (str): The internal field name (e.g., 'price_per_m2', 'investment_yield')
        lang (str): Language code (optional)
    
    Returns:
        str: Properly formatted display name
    """
    if not field_name:
        return ''
    
    # Convert to string in case it's not
    field_str = str(field_name)
    
    # First try the field mappings
    if field_str in FIELD_MAPPINGS:
        return FIELD_MAPPINGS[field_str]
    
    # Try translation system for known keys
    translated = t(field_str, lang)
    if translated != field_str:  # Found a translation
        return translated
    
    # Fallback to formatting: handle special cases
    formatted = field_str
    
    # Remove common prefixes and suffixes
    if formatted.endswith('_view'):
        formatted = formatted.replace('_view', '') + ' View'
    
    # Replace underscores with spaces and title case
    formatted = formatted.replace('_', ' ').title()
    
    # Fix common title case issues
    formatted = formatted.replace('Osm ', 'OSM ')
    formatted = formatted.replace('Api ', 'API ')
    formatted = formatted.replace('Gps ', 'GPS ')
    formatted = formatted.replace('Id ', 'ID ')
    formatted = formatted.replace('Url ', 'URL ')
    formatted = formatted.replace('M2', 'm²')
    
    return formatted