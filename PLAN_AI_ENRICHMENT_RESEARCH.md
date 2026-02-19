# AI Property Enrichment Research & Improvement Plan

## Executive Summary

This document reviews the AI property-enrichment system in Idealista Tracker AI, compares the current implementation against industry best practices, and provides a prioritized improvement plan.

---

## 1. Current Architecture

### 1.1 Current AI Enrichment Flow

**Data pipeline:**

```text
Email (Idealista alert)
    ↓
Parsing: title, price, area, description
    ↓
Geographic Enrichment:
  - Google Maps API (coordinates, travel times)
  - Google Places API (amenities)
  - OpenStreetMap (fallback POIs)
    ↓
Market Data Enrichment:
  - Construction costs (hardcoded €800-2000/m²)
  - Rental yields (hardcoded 3.5-7.5%)
  - Similar properties (from DB)
    ↓
AI Analysis (Claude/ChatGPT):
  - 5-block structured JSON response
  - Combines all enriched data
    ↓
Storage: Land.ai_analysis (JSON)
```

### 1.2 Data Sent to AI

**Core fields:**
- `title`, `price`, `area`, `municipality`, `land_type`, `score_total`
- `description` (max 500 chars)

**Location and travel:**
- `travel_time_nearest_beach`, `travel_time_oviedo`, `travel_time_gijon`, `travel_time_airport`
- `coordinates` (`lat`, `lon`)

**Infrastructure:**
- Basic utilities (electricity, water, internet, gas)
- Extended amenities (supermarket, school, hospital, restaurant)
- Transport (train, bus, airport)

**Enriched context:**
- Construction estimates (buildable area, costs)
- Market data (price/m², trends, sample size)
- Rental analysis (yields, cap rate, payback period)
- Similar properties (top 3 by score)

### 1.3 AI Response Structure

```json
{
  "price_analysis": { "verdict": "...", "summary": "...", "price_per_m2": 0, "recommendation": "..." },
  "investment_potential": { "rating": "...", "forecast": "...", "key_drivers": [], "risk_level": "..." },
  "risks_analysis": { "major_risks": [], "minor_issues": [], "advantages": [], "mitigation": [] },
  "development_ideas": { "best_use": "...", "building_size": "...", "special_features": [], "estimated_cost": "..." },
  "comparable_analysis": { "market_position": "...", "advantages_vs_similar": [], "disadvantages_vs_similar": [] },
  "similar_objects": { "comparison_summary": "...", "recommended_alternatives": [] },
  "construction_value_estimation": { "min": 0, "max": 0, "avg": 0, "construction_type": "...", "total_investment": 0 },
  "market_price_dynamics": { "price_trend": "...", "annual_growth_rate": 0, "market_factors": [] },
  "rental_market_analysis": { "monthly_rent": 0, "rental_yield": 0, "cap_rate": 0, "investment_rating": "..." }
}
```

---

## 2. Current Gaps

### 2.1 Critical Gaps

| Issue | Description | Impact |
|------|-------------|--------|
| Hardcoded market data | Construction costs (€800-2000/m²) and rental yields (3.5-7.5%) are hardcoded | AI receives stale or inaccurate market context |
| No real transaction data | No access to real closed-sale prices | Estimates depend on listing prices only |
| Limited comparables | Similar properties come only from local DB (max 3) | Small sample size lowers reliability |
| Regional assumptions | Generic Asturias assumptions for all calculations | Municipality-level differences are ignored |

### 2.2 Data Quality Gaps

| Issue | Description |
|------|-------------|
| Municipality extraction | Complex address patterns cause frequent parsing errors |
| Geocoding fallback | City center fallback can reduce precision |
| Duplicate coordinates | Duplicate filtering is stronger for precise than approximate results |
| Description truncation | 500 chars is often too short for full context |

### 2.3 Prompt Quality Gaps

| Issue | Description |
|------|-------------|
| No few-shot examples | Prompt has no high-quality example outputs |
| No explicit reasoning steps | Prompt does not force a consistent analytical sequence |
| Generic market context | No current Asturias market briefing in prompt |
| No output validation | Returned values are not validated for plausibility |

---

## 3. Industry Best Practices

### 3.1 AVM (Automated Valuation Model) Lessons

Zillow Zestimate reports low median error for on-market homes due to:
- Massive data coverage
- Real-time market inputs
- Training on historical transactions

**Core principles:**
1. Data quality matters more than model complexity.
2. Human-AI collaboration improves decision quality.
3. Hybrid automation plus expert review is more robust.

### 3.2 Prompt Engineering for Real Estate

From [On the Performance of LLMs for Real Estate Appraisal](https://arxiv.org/html/2506.11812v1):

1. Few-shot learning improves valuation quality.
2. Geographic proximity of examples is important.
3. Current market-report context improves temporal understanding.
4. Hedonic variables (size, amenities, location quality) are high-value features.

### 3.3 Data Sources for Spain

**CASAFARI**
- Cadastral information for Spain
- Transaction-focused market data
- Deduplicated listing coverage

**Idealista/data**
- Public and private source aggregation
- Structured real-time data outputs

**Spanish Cadastre integrations**
- Parcel-level lookup
- Property history enrichment

---

## 4. Current Market Inputs (Spain)

### 4.1 Construction Costs (2024-2025)

| Tier | Cost (€/m²) | Notes |
|------|--------------|-------|
| Economic | ~€1,100 | Basic finishes |
| Standard | ~€1,300-1,500 | Typical quality builds |
| Premium | ~€1,700+ | Premium materials and design |
| National average reference | Up to ~€2,235 | Includes broader total-cost assumptions |

Current hardcoded values (`€800-2000/m²`) are likely understated for many scenarios.

### 4.2 Rental Yields (Spain)

| Region / Segment | Gross Yield |
|------------------|-------------|
| Barcelona / Madrid | ~4-6% |
| Valencia / Málaga | up to ~8% |
| National reference | commonly in the ~5-8% band |

Current hardcoded yield values are directionally useful, but miss:
- Vacancy assumptions
- Operating expenses
- Net yield normalization

### 4.3 Transaction Cost Impact

Typical acquisition overhead in Spain is often around **10-14%** of purchase price (taxes, legal, notary, registry).

If excluded from `total_investment`, ROI can be significantly overstated.

---

## 5. Recommended Improvements

### 5.1 Priority 1: Better Market Inputs

#### A. Dynamic Construction Cost Table

```python
CONSTRUCTION_COSTS_2025 = {
    "economic": {"min": 1100, "avg": 1200, "max": 1300},
    "standard": {"min": 1300, "avg": 1500, "max": 1700},
    "premium": {"min": 1700, "avg": 2000, "max": 2500},
}

# Add annual inflation updates and regional coefficient multipliers.
```

#### B. Net Rental Yield Modeling

```python
RENTAL_ADJUSTMENTS = {
    "vacancy_rate": 0.10,
    "operating_expenses": 0.15,
    "management_fee": 0.08,
}
```

#### C. Purchase Cost Inclusion

```python
PURCHASE_COSTS_ASTURIAS = 0.11
total_investment = land_price * (1 + PURCHASE_COSTS_ASTURIAS) + construction_cost
```

### 5.2 Priority 2: Prompt Upgrades

#### A. Few-Shot Examples

```python
EXAMPLE_ANALYSIS = """
Example 1 - Good Investment:
Property: 800m² developed land, €45,000, 25min to beach
Analysis: {structured_example_1}

Example 2 - Moderate Investment:
Property: 500m² buildable, €65,000, 45min to beach
Analysis: {structured_example_2}
"""
```

#### B. Explicit Reasoning Sequence

```text
Before final output:
1. Compare price/m² with database comparables.
2. Evaluate accessibility (beach, city, airport).
3. Estimate construction feasibility and costs.
4. Calculate realistic rental potential.
5. Identify major risks and opportunities.
```

#### C. Dynamic Market Context Block

```python
MARKET_CONTEXT_2025 = """
ASTURIAS REAL ESTATE MARKET CONTEXT (2025):
- Housing prices: rising mid-single digits annually
- Construction costs: elevated versus pre-2022 baseline
- Rental demand: stronger in selected municipalities
- Risks: infrastructure variance in rural areas
"""
```

### 5.3 Priority 3: External Data Enrichment

#### A. Optional CASAFARI Integration
- Transaction-level data
- Better comparable sales coverage
- Broader market reporting

#### B. Spanish Cadastre Integration
- Official parcel and property dimensions
- Land classification details
- Permit and building-history enrichment

#### C. INE Statistics Integration
- Regional price indices
- Construction cost indices
- Population and employment signals

### 5.4 Priority 4: Validation and Confidence

#### A. Value Validation Layer

```python
def validate_ai_response(analysis: dict) -> dict:
    bounds = {
        "price_per_m2": (10, 5000),
        "rental_yield": (1, 15),
        "cap_rate": (1, 12),
        "annual_growth_rate": (-10, 20),
    }
    # Flag out-of-range values for human review.
    return analysis
```

#### B. Confidence Score

```python
def calculate_confidence(property_data: dict) -> float:
    score = 1.0
    if property_data["geocoding_accuracy"] != "precise":
        score *= 0.8
    if property_data["similar_count"] < 5:
        score *= 0.7
    if len(property_data.get("description", "")) < 100:
        score *= 0.9
    return score
```

---

## 6. Implementation Roadmap

### Phase 1: Quick Wins (1-2 days)
- [ ] Update construction costs to 2025 assumptions
- [ ] Add purchase costs to total investment
- [ ] Add market context to AI prompts
- [ ] Increase description length limit to 1000 chars

### Phase 2: Prompt Engineering (3-5 days)
- [ ] Create 3 strong few-shot examples
- [ ] Add explicit reasoning sequence
- [ ] Add response validation logic
- [ ] Add confidence scoring

### Phase 3: Data Quality (1 week)
- [ ] Integrate Spanish Cadastre API for verification
- [ ] Add INE market indicators
- [ ] Improve municipality extraction
- [ ] Improve vacancy/expense rental model

### Phase 4: Advanced (future)
- [ ] Add CASAFARI integration if budget allows
- [ ] Track historical price trajectories per property
- [ ] Add ML-assisted price estimation model
- [ ] Generate automated market reports

---

## 7. Conclusions

### What Works Well
1. Structured JSON output is clear and reusable.
2. Travel-time enrichment uses high-quality API signals.
3. Multi-provider AI comparison is useful.
4. Comparable-property context is conceptually strong.

### What Needs Improvement
1. Market data freshness is limited by hardcoded assumptions.
2. Prompt quality can improve with examples and reasoning steps.
3. Output validation is required for production reliability.
4. Regional granularity should be municipality-aware.

### Estimated Accuracy Impact

| Metric | Current | After Improvements |
|--------|---------|--------------------|
| Construction cost realism | ~60% | ~85% |
| Rental yield realism | ~70% | ~80% |
| Price trend confidence | ~50% | ~70% |
| Location signal quality | ~80% | ~85% |

---

## Sources

- [AI Property Valuation Guide 2024](https://plotzy.ai/blog/ai-powered-property-valuation-guide-2024/)
- [On the Performance of LLMs for Real Estate Appraisal](https://arxiv.org/html/2506.11812v1)
- [CASAFARI Property Data API](https://www.casafari.com/products/property-data-api/)
- [Idealista/data](https://datos.gob.es/en/casos-exito/idealista)
- [Building Costs in Spain 2025](https://en.barymont.com/blog/essentials/cost-to-build-a-house-in-spain-2025)
- [Rental Yields in Spain 2025](https://www.globalpropertyguide.com/europe/spain/rent-yields)
- [How to Calculate ROI on Spanish Property](https://skanon.com/magazine/step-by-step-guide-on-how-to-calculate-the-roi-for-properties-in-spain/961866)
