# AI Property Enrichment Research & Improvement Plan

## Executive Summary

Исследование системы AI-обогащения данных о недвижимости в проекте Idealista Tracker AI. Анализ текущей реализации, сравнение с лучшими практиками индустрии, и рекомендации по улучшению.

---

## 1. Текущая Архитектура

### 1.1 Как работает AI-обогащение сейчас

**Поток данных:**
```
Email (Idealista alert)
    ↓
Парсинг: title, price, area, description
    ↓
Geographic Enrichment:
  - Google Maps API (координаты, travel times)
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

### 1.2 Данные отправляемые в AI

**Базовые поля:**
- title, price, area, municipality, land_type, score_total
- description (max 500 chars)

**Location & Travel:**
- travel_time_nearest_beach, travel_time_oviedo, travel_time_gijon, travel_time_airport
- coordinates (lat, lon)

**Infrastructure:**
- Basic utilities (electricity, water, internet, gas)
- Extended amenities (supermarket, school, hospital, restaurant)
- Transport (train, bus, airport)

**Enriched Context:**
- Construction estimates (buildable area, costs)
- Market data (price/m², trends, sample size)
- Rental analysis (yields, cap rate, payback period)
- Similar properties (top 3 by score)

### 1.3 Структура ответа AI (5 блоков)

```json
{
  "price_analysis": { verdict, summary, price_per_m2, recommendation },
  "investment_potential": { rating, forecast, key_drivers, risk_level },
  "risks_analysis": { major_risks, minor_issues, advantages, mitigation },
  "development_ideas": { best_use, building_size, special_features, estimated_cost },
  "comparable_analysis": { market_position, advantages_vs_similar, disadvantages_vs_similar },
  "similar_objects": { comparison_summary, recommended_alternatives },
  "construction_value_estimation": { min/max/avg values, construction_type, total_investment },
  "market_price_dynamics": { price_trend, annual_growth_rate, market_factors },
  "rental_market_analysis": { monthly_rent, rental_yield, cap_rate, investment_rating }
}
```

---

## 2. Проблемы Текущей Реализации

### 2.1 Критические проблемы

| Проблема | Описание | Влияние |
|----------|----------|---------|
| **Hardcoded market data** | Стоимость строительства (€800-2000/m²) и rental yields (3.5-7.5%) захардкожены | AI получает неактуальные/неточные данные |
| **No real transaction data** | Нет доступа к реальным ценам сделок | Оценки основаны на listing prices, не на продажах |
| **Limited comparables** | Similar properties только из своей БД (max 3) | Малая выборка = ненадёжные тренды |
| **Regional assumptions** | Все расчёты для "Asturias" generic | Различия между муниципалитетами игнорируются |

### 2.2 Проблемы качества данных

| Проблема | Описание |
|----------|----------|
| **Municipality extraction** | Сложные паттерны адресов, частые ошибки парсинга |
| **Geocoding fallback** | При неточном geocoding используется центр города |
| **Duplicate coordinates** | Только для precise results, approximate могут дублироваться |
| **Description truncation** | 500 chars недостаточно для полного контекста |

### 2.3 Проблемы AI prompts

| Проблема | Описание |
|----------|----------|
| **No few-shot examples** | Промпт без примеров качественного ответа |
| **No chain-of-thought** | Нет пошагового reasoning |
| **Generic context** | Нет информации о текущем состоянии рынка Asturias |
| **No validation** | Нет проверки возвращённых значений на адекватность |

---

## 3. Лучшие Практики Индустрии

### 3.1 Automated Valuation Models (AVMs)

**Zillow Zestimate** достигает 1.8-2.4% median error для on-market homes за счёт:
- Миллионов data points
- Real-time market analysis
- Machine learning на исторических продажах

**Ключевые принципы:**
1. **Data quality > Algorithm complexity** - Качество данных важнее сложности модели
2. **Human-AI collaboration** - AI анализирует, человек принимает решения
3. **Hybrid approach** - Сочетание автоматизации и экспертной оценки

### 3.2 Prompting Best Practices для Real Estate

Из академического исследования [On the Performance of LLMs for Real Estate Appraisal](https://arxiv.org/html/2506.11812v1):

1. **Few-shot learning** - 10 similar properties в контексте значительно улучшает точность
2. **Geographic proximity** - Примеры должны быть географически близки
3. **Market report context** - Включение актуального market report улучшает temporal trends
4. **Hedonic variables** - LLMs эффективно используют: size, amenities, location quality

### 3.3 Data Sources для Spain

**CASAFARI** - AI Data Platform с 60M+ properties:
- Cadastral data для Spain
- Actual transaction prices (not just listings)
- Deduplicated listings

**Idealista/data** - Официальный data provider:
- Public sources: Cadastre, IGN, INE
- Private: Idealista.com listings
- Структурированная real-time информация

**TerceroB (Idealista):**
- Lookup в Spanish Cadastre по floor/door
- Enrichment с past transaction prices

---

## 4. Актуальные Данные по Рынку

### 4.1 Construction Costs Spain 2024-2025

| Тип | Стоимость (€/m²) | Источник |
|-----|------------------|----------|
| **Economic** | €1,100/m² | Square volume, basic finishes |
| **Standard** | €1,300-1,500/m² | Unique volumes, quality finishes |
| **Premium** | €1,700+/m² | Basement, premium materials |
| **Average 2024** | €2,235/m² | Including all costs |

**Текущие hardcoded значения (€800-2000/m²) занижены!**

### 4.2 Rental Yields Spain

| Регион/Тип | Gross Yield |
|------------|-------------|
| Barcelona/Madrid | 4-6% |
| Valencia/Málaga | до 8% |
| Average Spain 2020 | 7.5% (Idealista) |
| Expected return (Central Bank) | 11% (8.4% with mortgage) |

**Текущие hardcoded значения (3.5-7.5%) в целом корректны, но:**
- Не учитывают vacancy rate
- Не учитывают operating expenses (net yield на 1.5-2% ниже)

### 4.3 Purchase Costs Spain

Дополнительные расходы при покупке: **10-14% от цены**
- Transfer Tax (ITP): 8-10%
- Stamp Duty (AJD)
- Notary, Registry, Legal fees

**Это не учитывается в текущих расчётах total investment!**

---

## 5. Рекомендации по Улучшению

### 5.1 Приоритет 1: Улучшение Market Data

#### A. Dynamic Construction Costs

```python
# Вместо hardcoded
CONSTRUCTION_COSTS_2025 = {
    'economic': {'min': 1100, 'avg': 1200, 'max': 1300},
    'standard': {'min': 1300, 'avg': 1500, 'max': 1700},
    'premium': {'min': 1700, 'avg': 2000, 'max': 2500}
}

# + Annual inflation adjustment
# + Regional coefficient (Asturias ~0.85 of national average)
```

#### B. Realistic Rental Yields

```python
# Добавить:
# - Vacancy rate assumption (10-20% для vacation, 5% для long-term)
# - Operating expenses (maintenance, management, insurance)
# - Net yield calculation

RENTAL_ADJUSTMENTS = {
    'vacancy_rate': 0.10,  # 10% average
    'operating_expenses': 0.15,  # 15% of gross rent
    'management_fee': 0.08  # If using property manager
}
```

#### C. Include Purchase Costs

```python
# В total_investment добавить:
PURCHASE_COSTS_ASTURIAS = 0.11  # ~11% (ITP 8% + other)
total_investment = land_price * (1 + PURCHASE_COSTS_ASTURIAS) + construction_cost
```

### 5.2 Приоритет 2: Улучшение AI Prompts

#### A. Добавить Few-Shot Examples

```python
# В промпт добавить 2-3 примера качественного анализа:
EXAMPLE_ANALYSIS = """
Example 1 - Good Investment:
Property: 800m² developed land, €45,000, 25min to beach
Analysis: {structured_example_1}

Example 2 - Moderate Investment:
Property: 500m² buildable, €65,000, 45min to beach
Analysis: {structured_example_2}
"""
```

#### B. Chain-of-Thought Reasoning

```python
# Добавить в промпт:
"""
Before providing your final analysis, reason through these steps:
1. Compare price/m² to similar properties in the database
2. Evaluate location accessibility (beach, cities, airport)
3. Assess construction feasibility and costs
4. Calculate realistic rental potential
5. Identify major risks and opportunities

Then provide your structured analysis:
"""
```

#### C. Market Context Section

```python
# Добавить актуальный market context:
MARKET_CONTEXT_2025 = """
ASTURIAS REAL ESTATE MARKET CONTEXT (2025):
- Housing prices: Rising 4-5% annually
- Construction costs: €1,300-1,700/m² for quality builds
- Rental demand: Growing due to remote work trends
- Key drivers: Gijón tech hub growth, coastal lifestyle appeal
- Risks: Limited infrastructure in rural areas, harsh winters
"""
```

### 5.3 Приоритет 3: Внешние Data Sources

#### A. Интеграция с CASAFARI API (опционально)

Если бюджет позволяет:
- Real transaction data
- Comparable sales
- Market reports

#### B. Spanish Cadastre Integration

```python
# Бесплатно через Catastro API:
# - Official property dimensions
# - Land classification
# - Building permits history
# https://www.catastro.minhap.es/ws/webservices.pdf
```

#### C. INE Statistics

```python
# Instituto Nacional de Estadística:
# - Regional price indices
# - Construction cost indices
# - Population/employment data
# https://www.ine.es/
```

### 5.4 Приоритет 4: Validation & Quality

#### A. Response Validation

```python
def validate_ai_response(analysis: dict) -> dict:
    """Validate AI response values are within reasonable bounds."""

    validations = {
        'price_per_m2': (10, 5000),  # €10-5000/m²
        'rental_yield': (1, 15),      # 1-15%
        'cap_rate': (1, 12),          # 1-12%
        'annual_growth_rate': (-10, 20),  # -10% to +20%
    }

    for field, (min_val, max_val) in validations.items():
        value = get_nested_value(analysis, field)
        if value and not (min_val <= value <= max_val):
            log_warning(f"Suspicious {field}: {value}")
            # Flag for human review

    return analysis
```

#### B. Confidence Scoring

```python
# Добавить confidence score на основе:
# - Sample size для comparables
# - Geocoding accuracy
# - Description completeness
# - Infrastructure data availability

def calculate_confidence(property_data: dict) -> float:
    score = 1.0
    if property_data['geocoding_accuracy'] != 'precise':
        score *= 0.8
    if property_data['similar_count'] < 5:
        score *= 0.7
    if len(property_data['description']) < 100:
        score *= 0.9
    return score
```

---

## 6. Implementation Roadmap

### Phase 1: Quick Wins (1-2 дня)
- [ ] Update construction costs to 2025 values
- [ ] Add purchase costs (11%) to total investment
- [ ] Add market context to AI prompt
- [ ] Increase description limit to 1000 chars

### Phase 2: Prompt Engineering (3-5 дней)
- [ ] Create 3 high-quality few-shot examples
- [ ] Add chain-of-thought reasoning section
- [ ] Implement response validation
- [ ] Add confidence scoring

### Phase 3: Data Quality (1 неделя)
- [ ] Integrate Spanish Cadastre API for land verification
- [ ] Add INE price index data
- [ ] Improve municipality extraction
- [ ] Better vacancy/expenses modeling for rentals

### Phase 4: Advanced (future)
- [ ] CASAFARI integration (if budget allows)
- [ ] Historical price tracking per property
- [ ] ML-based price prediction (собственная модель)
- [ ] Automated market report generation

---

## 7. Выводы

### Что работает хорошо:
1. **Структурированный output** - 5-block JSON format удобен и понятен
2. **Travel time enrichment** - Реальные данные из Google Maps
3. **Multi-provider comparison** - Claude vs ChatGPT сравнение
4. **Similar properties context** - Хорошая идея, но limited sample

### Что требует улучшения:
1. **Market data актуальность** - Hardcoded values устарели
2. **Prompt engineering** - Нет few-shot, нет chain-of-thought
3. **Data validation** - AI может вернуть нереалистичные значения
4. **Regional specificity** - Все расчёты generic для региона

### Оценка точности текущих данных:

| Метрика | Текущая точность | После улучшений |
|---------|------------------|-----------------|
| Construction costs | ~60% (занижены) | ~85% |
| Rental yields | ~70% (gross, не net) | ~80% |
| Price trends | ~50% (малая выборка) | ~70% |
| Location quality | ~80% (Google APIs) | ~85% |

---

## Sources

- [AI Property Valuation Guide 2024](https://plotzy.ai/blog/ai-powered-property-valuation-guide-2024/)
- [On the Performance of LLMs for Real Estate Appraisal](https://arxiv.org/html/2506.11812v1)
- [CASAFARI Property Data API](https://www.casafari.com/products/property-data-api/)
- [Idealista/data](https://datos.gob.es/en/casos-exito/idealista)
- [Building Costs in Spain 2025](https://en.barymont.com/blog/essentials/cost-to-build-a-house-in-spain-2025)
- [Rental Yields in Spain 2025](https://www.globalpropertyguide.com/europe/spain/rent-yields)
- [How to Calculate ROI on Spanish Property](https://skanon.com/magazine/step-by-step-guide-on-how-to-calculate-the-roi-for-properties-in-spain/961866)
