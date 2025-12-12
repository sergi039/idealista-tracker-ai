import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import requests

from config import Config

logger = logging.getLogger(__name__)


STRUCTURED_JSON_SCHEMA = r"""
{
    "price_analysis": {
        "verdict": "FAIR_PRICE|OVERPRICED|UNDERPRICED",
        "summary": "Brief market comparison and price per m² analysis",
        "price_per_m2": estimated_market_price_per_m2,
        "recommendation": "Short recommendation about pricing"
    },
    "investment_potential": {
        "rating": "HIGH|MEDIUM|LOW",
        "forecast": "Growth forecast with timeframe",
        "key_drivers": ["main factor 1", "main factor 2", "main factor 3"],
        "risk_level": "LOW|MEDIUM|HIGH"
    },
    "risks_analysis": {
        "major_risks": ["significant risk 1", "significant risk 2"],
        "minor_issues": ["minor issue 1", "minor issue 2"],
        "advantages": ["advantage 1", "advantage 2", "advantage 3"],
        "mitigation": "How to address main risks"
    },
    "development_ideas": {
        "best_use": "Recommended development type",
        "building_size": "Recommended building size and type",
        "special_features": "Unique opportunities for this property",
        "estimated_cost": "Development cost estimate"
    },
    "comparable_analysis": {
        "market_position": "Position vs similar properties",
        "advantages_vs_similar": ["what makes this better"],
        "disadvantages_vs_similar": ["what makes this worse"],
        "price_comparison": "How price compares to similar properties"
    },
    "similar_objects": {
        "comparison_summary": "Brief comparison with similar properties from our database",
        "recommended_alternatives": ["ID:1 - Brief reason why this is similar", "ID:2 - Brief reason", "ID:3 - Brief reason"]
    },
    "construction_value_estimation": {
        "minimum_value": estimated_minimum_construction_value,
        "maximum_value": estimated_maximum_construction_value,
        "average_value": estimated_average_construction_value,
        "construction_type": "Modern house type recommended for this plot",
        "value_per_m2": estimated_value_per_m2_for_built_property,
        "total_investment": "Land price + construction cost estimate"
    },
    "market_price_dynamics": {
        "price_trend": "RISING|STABLE|DECLINING",
        "annual_growth_rate": estimated_annual_growth_percentage,
        "trend_period": "Time period for this trend (e.g., '2020-2025')",
        "trend_analysis": "Brief explanation of what drives the price trend in this area",
        "future_outlook": "1-3 year price forecast for similar properties",
        "market_factors": ["key factor 1 affecting prices", "key factor 2", "key factor 3"]
    },
    "rental_market_analysis": {
        "monthly_rent_min": minimum_monthly_rental,
        "monthly_rent_avg": average_monthly_rental,
        "monthly_rent_max": maximum_monthly_rental,
        "annual_rent_avg": average_annual_rental,
        "rental_yield": expected_rental_yield_percentage,
        "price_to_rent_ratio": price_to_annual_rent_ratio,
        "payback_period_years": years_to_recover_investment,
        "cap_rate": capitalization_rate_percentage,
        "investment_rating": "EXCELLENT|GOOD|MODERATE|BELOW_AVERAGE",
        "demand_factors": ["rental demand factor 1", "factor 2", "factor 3"],
        "rental_strategy": "Recommended rental strategy (long-term, vacation, etc.)"
    }
}
""".strip()


def _clean_json_text(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


class OpenAIService:
    def __init__(self):
        self.api_key = Config.OPENAI_API_KEY
        self.model = Config.OPENAI_MODEL
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY must be set")

    def _build_prompt(self, land, enriched_data: Dict[str, Any], similar_properties: list[dict]) -> str:
        title = land.title or f"Land #{land.id}"
        price = float(land.price) if land.price else None
        area = float(land.area) if land.area else None
        price_per_m2 = None
        if price and area and area > 0:
            price_per_m2 = price / area

        lines = [
            "Analyze this Asturias real estate property and provide structured insights in ENGLISH:",
            "",
            f"PROPERTY: {title}",
            f"PRICE: €{price:,.0f}" if price is not None else "PRICE: N/A",
            f"AREA: {area:,.0f}m²" if area is not None else "AREA: N/A",
            f"PRICE PER M²: €{price_per_m2:,.0f}/m²" if price_per_m2 is not None else "PRICE PER M²: N/A",
            f"LOCATION: {land.municipality or 'Unknown'}",
            f"TYPE: {land.land_type or 'unknown'}",
            f"TOTAL SCORE: {float(land.score_total):.2f}/100" if land.score_total is not None else "TOTAL SCORE: N/A",
        ]

        if land.travel_time_nearest_beach:
            beach_name = land.nearest_beach_name or "nearest beach"
            lines.append(f"BEACH ACCESS: {land.travel_time_nearest_beach} min to {beach_name}")
        if land.travel_time_oviedo:
            lines.append(f"OVIEDO: {land.travel_time_oviedo} min")
        if land.travel_time_gijon:
            lines.append(f"GIJÓN: {land.travel_time_gijon} min")
        if land.travel_time_airport:
            lines.append(f"AIRPORT: {land.travel_time_airport} min")

        if land.description:
            lines.append(f"DESCRIPTION: {land.description[:1200]}")

        construction = enriched_data.get("construction_value_estimation") or {}
        market = enriched_data.get("market_price_dynamics") or {}
        rental = enriched_data.get("rental_market_analysis") or {}

        if construction:
            lines += [
                "",
                "CONSTRUCTION ESTIMATES (Asturias 2024-2025):",
                f"Buildable area: {construction.get('buildable_area', 'N/A')}m² ({construction.get('buildability_ratio', 'N/A')} of land)",
                f"Construction cost per m²: €{construction.get('value_per_m2', 1000)}",
                f"Estimated construction: €{construction.get('minimum_value', 0):,.0f} - €{construction.get('maximum_value', 0):,.0f}",
                f"Total investment needed: €{construction.get('total_investment_min', 0):,.0f} - €{construction.get('total_investment_max', 0):,.0f}",
            ]

        if market:
            lines += [
                "",
                f"MARKET DATA (Based on {market.get('sample_size', 0)} similar properties):",
                f"Average price per m²: €{market.get('avg_price_per_m2', 50)}",
                f"Price range: €{market.get('min_price_per_m2', 30)} - €{market.get('max_price_per_m2', 150)}/m²",
                f"Current trend: {market.get('price_trend', 'STABLE')}",
                f"Annual growth: {market.get('annual_growth_rate', 3.5):.1f}%",
            ]

        if rental:
            lines += [
                "",
                f"RENTAL MARKET ANALYSIS ({rental.get('location_type', 'Unknown')} area):",
                f"Estimated monthly rent: €{rental.get('monthly_rent_min', 0):,.0f} - €{rental.get('monthly_rent_max', 0):,.0f} (avg: €{rental.get('monthly_rent_avg', 0):,.0f})",
                f"Annual rental income: €{rental.get('monthly_rent_min', 0)*12:,.0f} - €{rental.get('monthly_rent_max', 0)*12:,.0f}",
                f"Rental yield: {rental.get('rental_yield', 0):.1f}% (expected range: 5.0-7.5%)",
                f"Price-to-rent ratio: {rental.get('price_to_rent_ratio', 0):.1f}",
                f"Payback period: {rental.get('payback_period_years', 0):.1f} years",
                f"Cap rate: {rental.get('cap_rate', 0):.1f}%",
                f"Investment rating: {rental.get('investment_rating', 'MODERATE')}",
            ]

        if similar_properties:
            lines += ["", "Similar properties in our database:"]
            for idx, prop in enumerate(similar_properties, start=1):
                lines.append(
                    f"{idx}. {prop.get('title','')} - €{prop.get('price',0):,.0f} - {prop.get('area',0)}m² - {prop.get('municipality','')} - Score: {prop.get('score_total',0):.1f}/100"
                )

        lines += [
            "",
            "Provide analysis in this EXACT JSON format (keep all text in English).",
            "Return valid JSON ONLY (no markdown, no extra text):",
            STRUCTURED_JSON_SCHEMA,
            "",
            "IMPORTANT: Use the provided CONSTRUCTION ESTIMATES and MARKET DATA in your analysis.",
            "Keep all responses concise and focused on practical investment insights for Asturias real estate market.",
        ]

        return "\n".join(lines)

    def analyze_property_structured(self, land) -> Dict[str, Any]:
        from services.market_analysis_service import MarketAnalysisService
        from models import Land as LandModel
        from app import db

        market_service = MarketAnalysisService()
        enriched_data = market_service.get_enriched_data(land)

        # Lightweight "similar properties" list for context (top scored, same land type)
        similar_query = db.session.query(LandModel).filter(LandModel.id != land.id)
        if land.land_type:
            similar_query = similar_query.filter(LandModel.land_type == land.land_type)
        similar_query = similar_query.filter(LandModel.score_total.isnot(None)).order_by(LandModel.score_total.desc().nullslast())
        similar_lands = similar_query.limit(3).all()
        similar_properties = [
            {
                "id": p.id,
                "title": (p.title or f"Property #{p.id}")[:60] + ("..." if p.title and len(p.title) > 60 else ""),
                "price": float(p.price) if p.price else 0,
                "area": float(p.area) if p.area else 0,
                "municipality": p.municipality or "",
                "score_total": float(p.score_total) if p.score_total else 0,
                "land_type": p.land_type,
                "url": p.url,
            }
            for p in similar_lands
        ]

        prompt = self._build_prompt(land, enriched_data=enriched_data, similar_properties=similar_properties)

        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 4000,
        }

        # Prefer strict JSON output when supported.
        payload["response_format"] = {"type": "json_object"}

        started = datetime.utcnow()
        resp = requests.post(url, headers=headers, json=payload, timeout=600)
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )

        cleaned = _clean_json_text(content)
        analysis_data = json.loads(cleaned)
        if similar_properties:
            analysis_data["similar_properties_data"] = similar_properties

        return {
            "structured_analysis": analysis_data,
            "model": self.model,
            "status": "success",
        }


_openai_service: Optional[OpenAIService] = None


def get_openai_service() -> OpenAIService:
    global _openai_service
    if _openai_service is None:
        _openai_service = OpenAIService()
    return _openai_service

