# Idealista Land Watch & Rank

## Overview

This is a production-ready web application that automates property listing analysis from Idealista real estate emails. The system fetches property listings from Gmail twice daily, enriches the data using external APIs (Google Maps, Google Places, OSM), applies a multi-criteria scoring algorithm, and presents the results through a web interface with filtering, sorting, and export capabilities.

The application serves as a real estate investment analysis tool, helping users evaluate land properties based on infrastructure, transportation, environment, neighborhood quality, and legal status factors.

## User Preferences

Preferred communication style: Simple, everyday language.

### Interface Language Policy
- **All user interfaces must be in English only**
- Source content (property titles, descriptions from Idealista emails) remains in original Spanish
- System messages, forms, buttons, labels, confirmations - all in English
- No Russian, Spanish (except source content), or other languages in UI elements

### Scoring Methodology
- **MCDM (Multi-Criteria Decision Making) methodology implemented**
- Weights must sum to exactly 1.0 (100%) as per ISO 31000 and RICS standards
- When one weight changes, others are proportionally adjusted automatically
- Professional real estate evaluation standards compliance
- Real-time weight normalization and validation

## System Architecture

### Backend Architecture
- **Framework**: Flask with SQLAlchemy ORM for database operations
- **Application Factory Pattern**: Uses `create_app()` function for proper initialization
- **Blueprint Structure**: Separates routes into `main_routes` (web pages) and `api_routes` (REST endpoints)
- **Service Layer**: Modular services for Gmail integration, data enrichment, scoring, and scheduling
- **Configuration Management**: Centralized config class with environment variable support

### Frontend Architecture
- **Template Engine**: Jinja2 for server-side rendering
- **UI Framework**: Bootstrap with dark theme for responsive design
- **Progressive Enhancement**: HTMX for dynamic interactions without full page reloads
- **Vanilla JavaScript**: Custom scripts for table interactions, form handling, and UI enhancements

### Data Storage
- **Primary Database**: PostgreSQL with SQLAlchemy ORM
- **Schema Design**: Single `lands` table with JSONB fields for complex scoring data
- **Data Enrichment**: Structured storage for infrastructure, transport, environment, and neighborhood metrics
- **Flexible Scoring**: Separate criteria management for customizable scoring weights

### Authentication & Security
- **Centralized Security Validation**: SecurityValidator class validates all required and optional secrets at startup
- **Required Secrets Management**: SESSION_SECRET and DATABASE_URL must be set in Replit Secrets (no fallbacks)
- **Standardized API Key Names**: GOOGLE_MAPS_API_KEY, GOOGLE_PLACES_API_KEY, claude_key (ANTHROPIC_API_KEY)
- **Gmail Integration**: IMAP_USER and IMAP_PASSWORD for secure email access via App Passwords
- **Zero Hardcoded Secrets**: All sensitive values loaded from environment variables
- **Security Logging**: Startup validation logs missing optional secrets as warnings
- **Fail-Fast Validation**: Application refuses to start if required secrets are missing
- **Input Validation**: SQLAlchemy model constraints and form validation
- **Proxy Support**: ProxyFix middleware for deployment behind reverse proxies

### Scheduling System
- **Background Scheduler**: APScheduler for automated email ingestion
- **Cron-like Jobs**: Twice-daily execution (7 AM and 7 PM CET)
- **Manual Triggers**: API endpoints for on-demand ingestion
- **Error Handling**: Comprehensive logging and graceful failure handling

### Scoring Algorithm
- **MCDM Methodology**: Multi-Criteria Decision Making using professional standards
- **Weight Normalization**: Weights automatically normalized to sum to 1.0 (100%)
- **Proportional Adjustment**: When one weight changes, others adjust proportionally
- **Professional Compliance**: ISO 31000 and RICS real estate evaluation standards
- **Real-time Validation**: Instant weight validation and normalization feedback
- **Transparent Process**: Score breakdown shows individual criteria contributions
- **Scientific Approach**: Eliminates subjective bias through structured methodology

### Rental Market Analysis
- **Rental Income Estimation**: Calculates monthly and annual rental income based on location and property size
- **Investment Metrics**: Provides rental yield, cap rate, price-to-rent ratio, and payback period
- **Location-Based Analysis**: Different rental rates for urban, suburban, and rural areas in Asturias
- **Market Comparison**: Shows expected yield ranges and investment ratings (Excellent/Good/Moderate/Below Average)
- **Demand Factors**: Identifies rental demand drivers specific to property location
- **Real Estate Investment Analysis**: Comprehensive ROI calculations including NOI and capitalization rates
- **Rental Strategy Recommendations**: AI-powered suggestions for long-term vs vacation rental approaches

## External Dependencies

### Email Integration
- **Gmail API**: Fetches property listings from labeled emails
- **Google OAuth2**: Authentication for Gmail access
- **Email Parsing**: Custom regex-based parser for Idealista email formats

### Geospatial Services
- **Google Maps Geocoding API**: Converts addresses to coordinates
- **Google Places API**: Finds nearby amenities and services
- **OpenStreetMap Overpass API**: Infrastructure and transportation data
- **Distance Matrix API**: Travel time calculations to key locations

### Data Enrichment APIs
- **Google Places**: Restaurant ratings, school quality, nearby services
- **OSM Overpass**: Public transportation, utilities, environmental features
- **Geocoding Services**: Fallback geocoding when Google APIs unavailable

### Infrastructure Dependencies
- **PostgreSQL**: Primary database (Replit SQL)
- **APScheduler**: Background job scheduling
- **Flask-SQLAlchemy**: Database ORM and migrations
- **Bootstrap CDN**: Frontend styling and components
- **HTMX**: Dynamic frontend interactions
- **Font Awesome**: Icon library for UI elements

### Development & Testing
- **Pytest**: Comprehensive test suite with fixtures
- **Mock/Patch**: External API mocking for reliable testing
- **SQLite**: In-memory database for test isolation
- **Logging**: Structured logging throughout application layers