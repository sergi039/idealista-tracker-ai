# Idealista Land Watch & Rank

A production-ready real estate property analysis application that automates property listing analysis from Idealista real estate emails. The system fetches property listings from Gmail twice daily, enriches the data using external APIs, applies a multi-criteria scoring algorithm, and presents the results through a web interface with filtering, sorting, and export capabilities.

## 🏗️ Architecture Overview

This application serves as a real estate investment analysis tool, helping users evaluate land properties in Asturias, Spain based on infrastructure, transportation, environment, neighborhood quality, and legal status factors.

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

## 🚀 Key Features

### Email Integration & Data Processing
- **Automated Email Ingestion**: Fetches property listings from Gmail twice daily (7 AM and 7 PM CET)
- **Email Parsing**: Custom regex-based parser for Idealista email formats
- **Manual Triggers**: API endpoints for on-demand ingestion
- **Error Handling**: Comprehensive logging and graceful failure handling

### Advanced Scoring System
- **MCDM Methodology**: Multi-Criteria Decision Making using professional standards (ISO 31000, RICS)
- **Weight Normalization**: Weights automatically normalized to sum to 1.0 (100%)
- **Proportional Adjustment**: When one weight changes, others adjust proportionally
- **Transparent Process**: Score breakdown shows individual criteria contributions
- **Real-time Validation**: Instant weight validation and normalization feedback

### Data Enrichment
- **Google Maps Integration**: Geocoding, distance calculations, travel time analysis
- **Google Places API**: Restaurant ratings, school quality, nearby services
- **OpenStreetMap Integration**: Infrastructure and transportation data via Overpass API
- **Multi-source Fallbacks**: Robust data collection with multiple API sources

### Web Interface
- **Property Management**: View, filter, and sort property listings
- **Detailed Analysis**: Individual property pages with comprehensive scoring breakdown
- **Export Capabilities**: CSV export functionality for data analysis
- **Responsive Design**: Mobile-friendly interface with Bootstrap styling
- **Real-time Updates**: HTMX-powered dynamic content updates

## 🛠️ Installation & Setup

### Prerequisites
- Python 3.11+
- PostgreSQL database
- Gmail account with App Password
- Google Cloud Platform APIs (Maps, Places)
- Anthropic Claude API (optional, for AI analysis)

### Environment Variables

#### Required Secrets
Set these in your environment or Replit Secrets:

```bash
SESSION_SECRET=your-secure-session-secret
DATABASE_URL=postgresql://user:password@host:port/database
```

#### Optional Secrets (for enhanced functionality)
```bash
# Google APIs (use existing names if you have them)
GOOGLE_MAPS_API_KEY=your-google-maps-api-key
GOOGLE_PLACES_API_KEY=your-google-places-api-key
Google_api=your-google-api-key  # Alternative naming
GOOGLE_MAPS_API=your-google-maps-api  # Alternative naming

# Anthropic Claude AI
claude_key=your-anthropic-api-key

# Gmail Integration
IMAP_USER=your-gmail-address
IMAP_PASSWORD=your-gmail-app-password
```

### Installation Steps

1. **Clone the repository**
```bash
git clone https://github.com/sergi039/IdealistaRank.git
cd IdealistaRank
```

2. **Install dependencies**
```bash
pip install -r requirements.txt
```

3. **Set up environment variables**
- Copy `.env.example` to `.env` and fill in your values
- Or set variables in your hosting platform (Replit, Heroku, etc.)

4. **Initialize the database**
```bash
python -c "from app import app, db; app.app_context().push(); db.create_all()"
```

5. **Run the application**
```bash
gunicorn --bind 0.0.0.0:5000 --reuse-port --reload main:app
```

## 🔒 Security Features

### Centralized Security Validation
- **SecurityValidator Class**: Validates all required and optional secrets at startup
- **Fail-Fast Validation**: Application refuses to start if required secrets are missing
- **Zero Hardcoded Secrets**: All sensitive values loaded from environment variables
- **Security Logging**: Startup validation logs missing optional secrets as warnings

### API Key Management
- **Standardized Names**: Consistent naming conventions for all API keys
- **Multiple Naming Support**: Backward compatibility with existing key names
- **Secure Storage**: Integration with encrypted secret management systems

## 📊 Scoring Methodology

The application uses a **Multi-Criteria Decision Making (MCDM)** approach for property evaluation:

### Scoring Categories
1. **Infrastructure** (25%): Utilities, internet, road access
2. **Transportation** (25%): Public transport, airports, highways  
3. **Environment** (20%): Natural features, pollution levels, noise
4. **Neighborhood** (20%): Safety, amenities, schools, healthcare
5. **Legal Status** (10%): Zoning, permits, restrictions

### Weight Management
- Weights must sum to exactly 1.0 (100%) as per ISO 31000 standards
- When one weight changes, others are proportionally adjusted automatically
- Real-time weight normalization and validation
- Professional compliance with RICS real estate evaluation standards

## 🔄 API Endpoints

### Property Management
- `GET /api/lands` - Retrieve all properties
- `GET /api/lands/<id>` - Get specific property details
- `POST /api/lands/<id>/enrich` - Trigger data enrichment
- `POST /api/lands/<id>/score` - Recalculate property score

### Email Integration
- `POST /api/ingest` - Manual email ingestion trigger
- `GET /api/ingest/status` - Check ingestion status
- `GET /api/ingest/logs` - View ingestion logs

### Export Functions
- `GET /api/export/csv` - Export properties to CSV
- `GET /api/export/scores` - Export scoring data

## 🗄️ Database Schema

### Primary Table: `lands`
```sql
CREATE TABLE lands (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    price NUMERIC(12,2),
    location VARCHAR(500),
    location_lat DECIMAL(10,8),
    location_lon DECIMAL(11,8),
    location_accuracy VARCHAR(50),
    infrastructure_score JSONB,
    transport_score JSONB,
    environment_score JSONB,
    neighborhood_score JSONB,
    legal_score JSONB,
    total_score DECIMAL(5,2),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 🧪 Testing

Run the test suite:
```bash
pytest tests/ -v
```

### Test Coverage
- Unit tests for all service classes
- API endpoint testing
- Database integration tests
- Security validation tests

## 🚀 Deployment

### Replit Deployment
1. Import project from GitHub
2. Set environment variables in Secrets tab
3. Run with provided configuration

### Traditional Hosting
1. Set up PostgreSQL database
2. Configure environment variables
3. Deploy with any WSGI server (Gunicorn recommended)

## 📈 Performance Monitoring

### Logging
- Structured logging throughout application layers
- APScheduler job monitoring
- API response time tracking
- Error aggregation and alerting

### Metrics
- Property processing rates
- API call success rates
- Database query performance
- Email ingestion statistics

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Development Guidelines
- Follow PEP 8 coding standards
- Add tests for new features
- Update documentation as needed
- Ensure all security validations pass

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🆘 Support

For support and questions:
- Create an issue on GitHub
- Check the documentation in `/docs`
- Review the application logs for troubleshooting

## 🏆 Acknowledgments

- **Idealista**: For providing the real estate data source
- **Google Maps/Places APIs**: For geospatial data enrichment
- **OpenStreetMap**: For open-source mapping data
- **Anthropic Claude**: For AI-powered analysis capabilities

---

**Built with ❤️ for real estate investment analysis in Asturias, Spain**