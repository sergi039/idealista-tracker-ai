# Contributing to Idealista Tracker AI

We're excited that you're interested in contributing! This document outlines the process and guidelines for contributing to this project.

## 🚀 Getting Started

### Prerequisites
- Python 3.11 or higher
- PostgreSQL database
- Git for version control
- Basic knowledge of Flask, SQLAlchemy, and web development

### Development Setup

1. **Fork and clone the repository**
```bash
git clone https://github.com/yourusername/idealista-land-watch.git
cd idealista-land-watch
```

2. **Set up your development environment**
```bash
pip install -r requirements.txt
```

3. **Configure environment variables**
```bash
export SESSION_SECRET="your-dev-session-key"
export DATABASE_URL="postgresql://user:pass@localhost/dbname"
export DEV_MODE="true"  # Enables dev logging and convenience defaults
```

4. **Initialize the database**
```bash
python -c "from app import create_app, db; app=create_app(); app.app_context().push(); db.create_all()"
```

5. **Run the application**
```bash
gunicorn --bind 0.0.0.0:5000 --reload main:app
```

## 🏗️ Project Structure

```
├── app.py              # Flask application factory
├── models.py           # SQLAlchemy database models
├── config.py           # Application configuration
├── routes/             # URL routes and request handlers
│   ├── main_routes.py  # Web page routes
│   ├── api_routes.py   # API endpoints
│   └── language_routes.py # Language switching
├── services/           # Business logic layer
│   ├── enrichment_service.py
│   ├── scoring_service.py
│   └── scheduler_service.py
├── utils/              # Utility functions
│   ├── auth.py         # Authentication and rate limiting
│   ├── cache.py        # Caching utilities
│   └── security.py     # Security validation
├── templates/          # Jinja2 HTML templates
└── tests/              # Test suite
```

## 🔄 Development Workflow

### Branching Strategy
- `main` - Production-ready code
- `develop` - Integration branch for features
- `feature/feature-name` - Individual feature branches
- `bugfix/bug-description` - Bug fix branches

### Making Changes

1. **Create a feature branch**
```bash
git checkout -b feature/amazing-new-feature
```

2. **Make your changes**
   - Follow the existing code style and patterns
   - Add tests for new functionality
   - Update documentation as needed

3. **Test your changes**
```bash
pytest tests/ -v
pytest tests/ --cov=app --cov-report=html
```

4. **Commit your changes**
```bash
git add .
git commit -m "feat: add amazing new feature"
```

5. **Push to your fork**
```bash
git push origin feature/amazing-new-feature
```

6. **Create a Pull Request**
   - Use a clear title and description
   - Reference any related issues
   - Include screenshots for UI changes

## 📝 Code Style Guidelines

### Python Code
- Follow PEP 8 style guidelines
- Use type hints where appropriate
- Keep functions focused and under 50 lines
- Use descriptive variable and function names
- Add docstrings to all public functions

### Frontend Code
- Use Bootstrap classes for styling
- Keep JavaScript minimal and vanilla (no jQuery)
- Use HTMX for dynamic interactions
- Follow existing naming conventions for CSS classes

### Database Changes
- Never change existing primary key types
- Use SQLAlchemy migrations for schema changes
- Add appropriate indexes for new query patterns
- Test database changes thoroughly

## 🧪 Testing Guidelines

### Writing Tests
- Write tests for all new functionality
- Aim for >80% code coverage
- Use descriptive test names that explain what's being tested
- Mock external API calls to ensure reliable tests

### Test Categories
- **Unit Tests**: Test individual functions and methods
- **Integration Tests**: Test API endpoints and database interactions
- **Security Tests**: Test authentication and authorization

### Running Tests
```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_scoring_service.py -v

# Run with coverage
pytest tests/ --cov=app --cov-report=html
```

### Local CI gate

Before pushing, run the same checks CI runs on GitHub — locally, in seconds:

```bash
tools/ci/local_ci.sh
```

It runs `ruff check .`, `ruff format --check .`, the no-source-bundles check,
and `uv run pytest tests/ -q`, with a clear PASS/FAIL per step. Install it as
a `pre-push` hook once per clone:

```bash
tools/ci/install_hooks.sh
```

The hook validates what is actually being pushed, not your working tree: if
the tree is clean it runs in place, and if it's dirty it checks out the
pushed commit(s) into a throwaway `git worktree` and runs the gate there, so
an uncommitted "fix" can't hide a broken committed blob.

Bypass a single push with `SKIP_LOCAL_CI=1 git push` if you need to push
work-in-progress.

## 🐛 Bug Reports

When reporting bugs, please include:

1. **Bug Description**: Clear description of what's wrong
2. **Steps to Reproduce**: Exact steps to recreate the issue
3. **Expected Behavior**: What should happen instead
4. **Actual Behavior**: What actually happens
5. **Environment**: OS, Python version, browser (if applicable)
6. **Logs**: Relevant error messages or logs

## 💡 Feature Requests

For feature requests, please include:

1. **Problem Statement**: What problem does this solve?
2. **Proposed Solution**: How should it work?
3. **Use Cases**: When would this be useful?
4. **Technical Considerations**: Any implementation thoughts?

## 🔒 Security

### Reporting Security Issues
Please **DO NOT** open public issues for security vulnerabilities. Instead:
- Email security concerns privately
- Include detailed information about the vulnerability
- Allow time for patching before public disclosure

### Security Guidelines
- Never commit secrets or API keys
- Use environment variables for all sensitive data
- Validate all user inputs
- Use parameterized queries to prevent SQL injection
- Implement proper authentication and authorization

## 📚 Documentation

### Code Documentation
- Add docstrings to all public functions
- Include type hints in function signatures
- Comment complex logic or algorithms
- Update README.md for significant changes

### API Documentation
- Document all API endpoints
- Include request/response examples
- Specify required vs optional parameters
- Document error responses

## 🎯 Contribution Areas

We especially welcome contributions in these areas:

### High Priority
- Performance optimizations
- Security enhancements
- Test coverage improvements
- Bug fixes

### Medium Priority
- New API integrations
- UI/UX improvements
- Documentation improvements
- Code refactoring

### Low Priority
- New features (discuss first)
- Experimental integrations
- Development tooling

## ✅ Pull Request Checklist

Before submitting a PR, ensure:

- [ ] Code follows project style guidelines
- [ ] All tests pass locally
- [ ] New features have appropriate tests
- [ ] Documentation is updated if needed
- [ ] Commit messages are clear and descriptive
- [ ] No secrets or sensitive data in commits
- [ ] Performance impact considered
- [ ] Security implications reviewed

## 📞 Getting Help

- **Questions**: Open a GitHub issue with the "question" label
- **Discussion**: Use GitHub Discussions for broader topics
- **Real-time**: Check if there's a Discord/Slack channel
- **Documentation**: Check the README and code comments first

## 🙏 Recognition

Contributors will be recognized in:
- GitHub contributors list
- Release notes for significant contributions
- README acknowledgments section

Thank you for contributing to making real estate investment analysis more accessible and powerful! 🏡
