#!/usr/bin/env python3
"""
Fix municipality problem for all lands with 'And' value
"""
import os
import sys
import logging
import re
from datetime import datetime

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db
from models import Land
from services.enrichment_service import EnrichmentService

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def extract_real_municipality(title, description):
    """
    Extract real municipality from title and description
    """
    # Spanish municipalities in the region
    known_municipalities = [
        'Santander', 'Torrelavega', 'Castro-Urdiales', 'Camargo', 'Piélagos',
        'El Astillero', 'Laredo', 'Santa Cruz de Bezana', 'Los Corrales de Buelna',
        'Santoña', 'Reinosa', 'Reocín', 'Suances', 'Colindres', 'Noja',
        'Cabezón de la Sal', 'Medio Cudeyo', 'Polanco', 'San Vicente de la Barquera',
        'Comillas', 'Cartes', 'Oviedo', 'Gijón', 'Avilés', 'Siero', 'Langreo',
        'Mieres', 'Castrillón', 'San Martín del Rey Aurelio', 'Corvera de Asturias',
        'Cangas del Narcea', 'Valdés', 'Villaviciosa', 'Llanes', 'Aller', 'Lena',
        'Cudillero', 'Llanera', 'Tineo', 'Navia', 'Gozón', 'Carreño', 'Pravia',
        'Cangas de Onís', 'Ribadesella', 'Salas', 'Piloña', 'Noreña', 'Laviana',
        'Ribadedeva', 'Parres', 'Miengo', 'Ribamontán al Mar', 'Marina de Cudeyo',
        'Bareyo', 'Arnuero', 'Meruelo', 'Escalante', 'Argoños', 'Hazas de Cesto',
        'Solórzano', 'Voto', 'Ampuero', 'Limpias', 'Colindres', 'Laredo',
        'Liendo', 'Guriezo', 'Udías', 'Alfoz de Lloredo', 'Ruiloba', 'Valdáliga',
        'Val de San Vicente', 'Herrerías', 'Tresviso', 'Peñarrubia', 'Cillorigo de Liébana',
        'Cabezón de Liébana', 'Camaleño', 'Potes', 'Vega de Liébana', 'Pesaguero',
        'La Hermandad de Campoo de Suso', 'Campoo de Enmedio', 'Campoo de Yuso',
        'Valdeolea', 'Valdeprado del Río', 'Valderredible', 'Las Rozas de Valdearroyo',
        'San Miguel de Aguayo', 'Santiurde de Reinosa', 'Pesquera', 'Bárcena de Pie de Concha',
        'Molledo', 'Arenas de Iguña', 'Anievas', 'Cieza', 'Los Tojos', 'Ruente',
        'Cabuérniga', 'Tudanca', 'Polaciones', 'Lamasón', 'Rionansa', 'Peñamellera Baja',
        'Peñamellera Alta', 'Cabrales', 'Onís', 'Amieva', 'Ponga', 'Illas'
    ]
    
    # Try to find known municipality in title or description
    text = f"{title} {description}".lower() if description else title.lower()
    
    for municipality in known_municipalities:
        if municipality.lower() in text:
            return municipality
    
    # Try to extract location from patterns
    # Pattern 1: "in [Location]" but skip "in your search"
    in_match = re.search(r'\bin\s+([A-Z][a-záéíóúñ\s\-]+)(?:\s+Land|[,!]|\s+\d)', text, re.IGNORECASE)
    if in_match:
        location = in_match.group(1).strip()
        if 'your search' not in location.lower() and location.lower() != 'cantabria land':
            return location.title()
    
    # Pattern 2: Look for Spanish/Asturian place names
    location_patterns = [
        r'(?:en|in)\s+([A-ZÁÉÍÓÚ][a-záéíóúñ]+(?:\s+de\s+[A-ZÁÉÍÓÚ][a-záéíóúñ]+)?)',
        r'(?:municipality|municipio|lugar|sitio|zona)\s*:?\s*([A-ZÁÉÍÓÚ][a-záéíóúñ\s\-]+)',
        r'([A-ZÁÉÍÓÚ][a-záéíóúñ]+(?:\s+de\s+[A-ZÁÉÍÓÚ][a-záéíóúñ]+)?)\s*,?\s*(?:Cantabria|Asturias)',
    ]
    
    for pattern in location_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            location = match.group(1).strip()
            if len(location) > 2 and location.lower() not in ['and', 'or', 'the', 'of']:
                return location.title()
    
    # Default to Cantabria if nothing found
    return "Cantabria"

def fix_municipalities():
    """Fix all lands with 'And' municipality"""
    
    with app.app_context():
        # Get all lands with 'And' municipality
        lands_to_fix = Land.query.filter(
            (Land.municipality == 'And') | 
            (Land.municipality == None) |
            (Land.municipality == '')
        ).all()
        
        logger.info(f"Found {len(lands_to_fix)} lands to fix")
        
        fixed_count = 0
        enrichment_service = EnrichmentService()
        
        for i, land in enumerate(lands_to_fix):
            logger.info(f"Processing {i+1}/{len(lands_to_fix)}: Land {land.id}")
            
            # Try to extract real municipality
            new_municipality = extract_real_municipality(land.title, land.description)
            
            if new_municipality and new_municipality != 'And':
                logger.info(f"  Found municipality: {new_municipality}")
                land.municipality = new_municipality
                db.session.commit()
                
                # Now enrich with proper location
                logger.info(f"  Enriching land {land.id}...")
                success = enrichment_service.enrich_land(land.id)
                
                if success:
                    db.session.refresh(land)
                    logger.info(f"  ✅ Enriched! Location: {land.location_lat}, {land.location_lon}, Score: {land.score_total}")
                    fixed_count += 1
                else:
                    logger.warning(f"  ⚠️ Enrichment failed")
            else:
                logger.info(f"  Municipality still unclear, setting to Cantabria")
                land.municipality = "Cantabria"
                db.session.commit()
                
                # Try enrichment anyway
                enrichment_service.enrich_land(land.id)
        
        logger.info(f"\n✅ Fixed {fixed_count} lands successfully")
        
        # Show statistics
        stats = db.session.query(
            db.func.count(Land.id).label('total'),
            db.func.count(Land.location_lat).label('with_coordinates'),
            db.func.count(Land.score_total).label('with_score')
        ).first()
        
        logger.info(f"\n📊 Final Statistics:")
        logger.info(f"  Total lands: {stats.total}")
        logger.info(f"  With coordinates: {stats.with_coordinates} ({100*stats.with_coordinates/stats.total:.1f}%)")
        logger.info(f"  With scores: {stats.with_score} ({100*stats.with_score/stats.total:.1f}%)")

if __name__ == "__main__":
    fix_municipalities()