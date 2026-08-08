# Changelog

All notable changes to the Idealista Tracker AI project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### 🛟 Fixed: last_seen_uid no longer runs ahead of the database (2026-08-08, #24)
- **What**: both IMAP services persisted `max(uids)` inside
  `get_idealista_emails()`, before `run_ingestion()` had written anything. A
  crash, restart or DB failure between fetch and commit lost those listings,
  price changes and removal notices permanently and silently. The cursor now
  advances per email, only after that email's rows are committed (or it was
  deliberately filtered out), via the new `utils/uid_cursor.py`.
- **Why**: silent, permanent data loss with no signal — `SyncHistory` could not
  be reconciled against the cursor.
- **Consequence to keep in mind**: the cursor file is now written atomically,
  and an unreadable/corrupt cursor **raises** instead of silently resetting to 0
  and reprocessing the whole mailbox. An email that keeps failing to commit
  holds the cursor back (logged as `Holding last_seen_uid at …`), so a stuck
  ingestion is now visible in the logs instead of losing mail.

### 🔓 Removed: admin authentication (2026-08-08)
- **Login removed**: deleted `utils/auth.py`, the `/login` and `/logout` routes,
  `templates/login.html`, all 42 `@admin_required` decorators and every inline
  `check_admin_auth()` gate. `ADMIN_API_TOKEN` is no longer read anywhere.
- **Why**: single-owner local tool, published only on `127.0.0.1:5001`; the
  owner wanted direct access rather than re-entering a token each month.
- **Consequence to keep in mind**: nothing gates the API any more, and the JSON
  blueprints stay CSRF-exempt — the loopback binding in `docker-compose.yml` is
  now the only thing standing between the database (and the paid AI/enrichment
  endpoints) and the network. Put authentication back before exposing the app.

## [2.0.0] - 2025-09-11

### 🚀 Major Features Added
- **Dual Score System**: Implemented separate Investment Score (32%) and Lifestyle Score (68%) for targeted property analysis
- **Three Analysis Modes**: Investment-focused, Lifestyle-focused, and Balanced approaches for different user needs
- **Complete Bilingual Support**: Full English/Spanish localization with session-based language switching
- **AI-Powered Analysis**: Claude Sonnet 4 integration for detailed investment insights and market predictions

### ⚡ Performance Improvements
- **Database Optimization**: Added 7 strategic indexes resulting in 3-5x faster query performance
- **Memory Efficiency**: Implemented deferred JSONB column loading, reducing memory usage by 60%
- **Caching Layer**: Added Flask-Caching with Redis support for API responses and enrichment data
- **Page Load Speed**: Achieved 40% reduction in page load times through query optimization

### 🔒 Security Enhancements
- **Fail-Closed Authentication**: Implemented secure admin authentication with token-based access
- **Rate Limiting**: Added configurable rate limits for different endpoint types
- **Input Validation**: Comprehensive SQLAlchemy constraints and form validation
- **Secrets Management**: Centralized SecurityValidator with startup validation

### 🐛 Critical Bug Fixes
- **Scheduler Reliability**: Added file-based locking to prevent duplicate scheduler instances
- **Download Endpoints**: Fixed filename handling for property data exports
- **Email Backend**: Simplified to stable IMAP implementation, removed deprecated Gmail API service
- **UI Text Overflow**: Implemented comprehensive text truncation with tooltips for Spanish content

### 🧹 Code Quality Improvements
- **Dead Code Removal**: Cleaned up unused gmail_service.py and related test files
- **Architecture Refactoring**: Improved separation of concerns with service layer pattern
- **Error Handling**: Enhanced error handling and logging throughout the application
- **Testing**: Expanded test coverage for critical functionality

## [1.5.0] - 2025-09-10

### Added
- **Manual Sync Button**: User-friendly one-click property synchronization
- **Enhanced UI Components**: Improved property cards and table layouts
- **Export Functionality**: CSV export with filtered data support

### Fixed
- **Language Switching**: Improved language toggle reliability
- **Mobile Responsiveness**: Better mobile device support
- **Data Validation**: Enhanced property data validation

## [1.4.0] - 2025-09-09

### Added
- **Advanced Filtering**: Multi-criteria filtering by price, location, scores
- **Sorting Options**: Sortable columns in property tables
- **Responsive Design**: Enhanced mobile and tablet support

### Changed
- **UI Theme**: Refined dark theme for better accessibility
- **Navigation**: Improved navigation structure and user flow

## [1.3.0] - 2025-09-08

### Added
- **MCDM Scoring**: Multi-Criteria Decision Making methodology implementation
- **Weight Management**: Dynamic scoring weight adjustment interface
- **Location Intelligence**: Enhanced geocoding and distance calculations

### Fixed
- **API Rate Limiting**: Improved handling of external API rate limits
- **Data Enrichment**: More reliable enrichment process

## [1.2.0] - 2025-09-07

### Added
- **Email Integration**: Automated Idealista email processing
- **Property Enrichment**: External API integration for location data
- **Scoring System**: Initial implementation of property scoring

### Security
- **Environment Variables**: Moved all secrets to environment configuration
- **Input Sanitization**: Added protection against common web vulnerabilities

## [1.1.0] - 2025-09-06

### Added
- **Database Models**: Core property and scoring models
- **Web Interface**: Basic Flask web application
- **Property Display**: Initial property listing functionality

## [1.0.0] - 2025-09-05

### Added
- **Initial Release**: Basic Flask application structure
- **PostgreSQL Integration**: Database setup and configuration
- **Project Foundation**: Core architecture and development environment

---

## Legend

- 🚀 **Major Features**: Significant new functionality
- ⚡ **Performance**: Speed and efficiency improvements
- 🔒 **Security**: Security enhancements and fixes
- 🐛 **Bug Fixes**: Bug fixes and stability improvements
- 🧹 **Code Quality**: Refactoring and code improvements
- 📝 **Documentation**: Documentation updates
- 🎨 **UI/UX**: User interface and experience improvements

## Upcoming Features

### Planned for v2.1.0
- **Background Processing**: Async enrichment jobs for better performance
- **Advanced Analytics**: Enhanced investment metrics and market analysis
- **Export Enhancements**: Additional export formats and scheduling
- **API Documentation**: Comprehensive API documentation with examples

### Under Consideration
- **Mobile App**: Native mobile application
- **Multi-Region Support**: Support for other Spanish regions
- **Advanced Mapping**: Interactive maps with property overlays
- **Machine Learning**: Predictive pricing models
