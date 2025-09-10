# 🏡 Idealista Land Watch & Rank

An advanced real estate investment analysis platform that automates property evaluation for the Asturias region of Spain. The system monitors Idealista property listings via email integration, enriches data with multiple APIs, and provides AI-powered investment insights through a professional web interface.

## ✨ What Makes This Special

This isn't just another property listing tool. It's a comprehensive investment analysis platform that:
- **Saves hours of manual research** by automatically processing property emails
- **Provides institutional-grade analysis** using MCDM methodology and AI insights
- **Delivers actionable investment recommendations** based on real market data
- **Tracks market dynamics** to identify the best investment opportunities

## 🚀 Key Features

### 📧 Automated Email Processing
- **Smart Email Ingestion**: Automatically fetches and processes Idealista property emails twice daily
- **Intelligent Parsing**: Extracts property details, prices, locations, and descriptions
- **Duplicate Detection**: Prevents duplicate listings from being processed
- **Manual Triggers**: On-demand ingestion available through the web interface

### 🤖 AI-Powered Analysis (NEW)
- **Investment Insights**: Claude AI provides detailed investment potential analysis
- **Construction Estimates**: Calculates development costs and ROI projections
- **Market Comparisons**: Analyzes similar properties to determine fair pricing
- **Risk Assessment**: Identifies major risks and advantages for each property
- **Rental Market Analysis**: Projects rental income and investment returns
- **Development Ideas**: Suggests best use cases for land development

### 📊 Professional Scoring System
- **MCDM Methodology**: Multi-Criteria Decision Making following ISO 31000 standards
- **5 Key Categories**:
  - Infrastructure (25%): Utilities, internet, road access
  - Transportation (25%): Public transport, airports, highways
  - Environment (20%): Natural features, pollution, noise levels
  - Neighborhood (20%): Safety, amenities, schools, healthcare
  - Legal Status (10%): Zoning, permits, development restrictions
- **Dynamic Weight Adjustment**: Weights auto-normalize to 100% for accuracy
- **Transparent Scoring**: Detailed breakdown of how each score is calculated

### 🗺️ Location Intelligence
- **Google Maps Integration**: Precise geocoding and distance calculations
- **Travel Time Analysis**: Real-time travel estimates to major cities (Oviedo, Gijón, Avilés)
- **Nearby Amenities**: Schools, hospitals, restaurants, shopping centers
- **Infrastructure Mapping**: Public transport, utilities, road networks via OpenStreetMap
- **Beach Proximity**: Distance to nearest beaches for tourism potential

### 💰 Investment Analytics
- **Rental Yield Calculations**: Expected returns based on location and property type
- **Cap Rate Analysis**: Capitalization rates for investment comparison
- **Price-to-Rent Ratios**: Evaluate if buying or renting is better
- **Payback Period**: Years to recover investment through rental income
- **Market Trends**: Historical price dynamics and future projections
- **Similar Properties**: Find and compare similar investment opportunities

### 🎨 Modern Web Interface
- **Responsive Design**: Works seamlessly on desktop, tablet, and mobile
- **Dark Theme**: Professional dark mode interface that's easy on the eyes
- **Dynamic Updates**: Real-time content updates without page refreshes (HTMX)
- **Advanced Filtering**: Filter by price, location, score, property type
- **Multiple Views**: Table and card layouts for different preferences
- **Export Capabilities**: Download data as CSV for external analysis

## 🛠️ Technical Architecture

### Backend Stack
- **Framework**: Flask with production-ready Gunicorn WSGI server
- **Database**: PostgreSQL with SQLAlchemy ORM
- **Task Scheduling**: APScheduler for automated email ingestion
- **API Integration**: RESTful endpoints for all functionality
- **Security**: Environment-based secrets management with validation

### Frontend Stack
- **Template Engine**: Jinja2 for server-side rendering
- **CSS Framework**: Bootstrap 5 with custom dark theme
- **JavaScript**: Vanilla JS with HTMX for progressive enhancement
- **Icons**: Font Awesome for professional iconography

### External Services
- **Google Cloud Platform**: Maps, Places, Geocoding APIs
- **Anthropic Claude**: AI-powered property analysis
- **OpenStreetMap**: Infrastructure and transportation data
- **Gmail API**: Secure email integration with OAuth2

## 📦 Installation

### Prerequisites
- Python 3.11 or higher
- PostgreSQL database
- Gmail account with App Password enabled
- API keys for Google Maps/Places (optional but recommended)
- Anthropic API key (optional for AI features)

### Quick Start

1. **Clone the repository**
```bash
git clone https://github.com/yourusername/idealista-land-watch.git
cd idealista-land-watch
```

2. **Install dependencies**
```bash
pip install -r requirements.txt
```

3. **Set up environment variables**
```bash
# Required
export SESSION_SECRET="your-secure-session-key"
export DATABASE_URL="postgresql://user:pass@localhost/dbname"

# Optional but recommended
export GOOGLE_MAPS_API_KEY="your-google-maps-key"
export GOOGLE_PLACES_API_KEY="your-google-places-key"
export ANTHROPIC_API_KEY="your-claude-api-key"
export IMAP_USER="your-gmail@gmail.com"
export IMAP_PASSWORD="your-app-specific-password"
```

4. **Initialize database**
```bash
python -c "from app import app, db; app.app_context().push(); db.create_all()"
```

5. **Run the application**
```bash
gunicorn --bind 0.0.0.0:5000 --reload main:app
```

Visit `http://localhost:5000` to access the application.

## 🔐 Security Features

- **No Hardcoded Secrets**: All sensitive data in environment variables
- **Validation on Startup**: Application verifies all required secrets before starting
- **Secure Email Access**: Uses Gmail App Passwords, never stores main password
- **SQL Injection Protection**: SQLAlchemy ORM prevents SQL injection attacks
- **XSS Protection**: Template auto-escaping prevents cross-site scripting
- **CSRF Protection**: Flask-WTF forms include CSRF tokens

## 📊 API Endpoints

### Property Management
- `GET /lands` - View all properties with filtering
- `GET /lands/<id>` - Detailed property view
- `POST /api/lands/<id>/enrich` - Enrich property with API data
- `POST /api/analyze/property/<id>/structured` - Generate AI analysis

### Email Integration
- `POST /api/ingest` - Trigger manual email ingestion
- `GET /api/scheduler/status` - Check scheduler status
- `POST /api/scheduler/trigger/<job_id>` - Run scheduled job manually

### Data Export
- `GET /api/export/csv` - Export all properties to CSV
- `POST /api/scoring/weights` - Update scoring weights

## 🧪 Testing

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=app --cov-report=html
```

## 🚀 Deployment

### Deploy on Replit
1. Import this repository to Replit
2. Add secrets in the Secrets tab
3. Click Run - Replit handles the rest

### Deploy on Heroku
```bash
heroku create your-app-name
heroku addons:create heroku-postgresql:hobby-dev
heroku config:set SESSION_SECRET="your-secret"
git push heroku main
```

### Docker Deployment
```bash
docker build -t idealista-watch .
docker run -p 5000:5000 --env-file .env idealista-watch
```

## 📈 Performance & Monitoring

- **Structured Logging**: Comprehensive logs for debugging
- **Performance Metrics**: Track API response times and success rates
- **Error Handling**: Graceful fallbacks when external services fail
- **Database Optimization**: Indexed queries for fast property searches

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request. For major changes:

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- **Idealista** - For providing the property data source
- **Google Cloud Platform** - For mapping and location services
- **Anthropic** - For Claude AI integration
- **OpenStreetMap** - For open-source geographic data
- **Flask Community** - For the excellent web framework

## 📞 Support

Having issues? 
- Check the [Issues](https://github.com/yourusername/idealista-land-watch/issues) page
- Review application logs in `/tmp/logs/`
- Ensure all API keys are correctly configured

---

**Built with passion for smart real estate investment in Asturias, Spain** 🇪🇸

*Making property investment decisions easier, one analysis at a time.*