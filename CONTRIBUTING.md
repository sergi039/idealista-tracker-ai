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

### Testing a migration

`migrations/*.sql` is PostgreSQL-only and multi-statement, so SQLite cannot
execute it and `db.create_all()` says nothing about whether it works.
`tests/test_postgres_migrations.py` applies the real files to a real server and
skips unless `TEST_DATABASE_URL_POSTGRES` is set. Those tests CREATE and DROP
databases on whatever server it names, so it must be a **throwaway** nobody
else is using.

**This project runs in Docker on the Mac mini and keeps no local database**
(owner, 2026-08-31): your laptop is a client that reaches the mini over
Tailscale, so the throwaway is a container over there, tunnelled to
127.0.0.1:55432 for the duration of the run:

```bash
eval "$(tools/ci/migration_test_db.sh start)"
uv run pytest tests/test_postgres_migrations.py -v
tools/ci/migration_test_db.sh stop
```

`start` runs one `docker run --rm` of the same `postgres:15-alpine` the
deployment's `idealista-db` runs — so migrations are exercised on production's
own major version — under its own name, on the mini's loopback, with no compose
labels and no volume. It never touches `idealista-db` and a deploy does not
disturb it. Offline, `start` fails and says so; the fallback is CI, **never a
database on your own machine**.

**Two servers are not throwaways and the suite refuses both**: `127.0.0.1:5434`
is the mini's running `idealista-db`, and `127.0.0.1:5432` on a Mac here is
Postgres.app — the inbox-zero project's database server, which holds its live
`inboxzero` database. `tests/postgres_server_guard.py` checks `pg_database`
before the first CREATE DATABASE and fails, naming what it found; see the
"Writing a migration?" rule in CLAUDE.md for the incident behind it.

CI runs the same tests against a service container with
`REQUIRE_POSTGRES_TESTS=1`, so a missing server fails the job rather than
turning the only coverage of migration SQL into a silent skip.

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

The hook validates what is actually being pushed, not your working tree:
every pushed commit is checked out into a throwaway `git worktree` and the
gate runs there, deliberately with no working-tree shortcut — a tree that
merely *looks* clean can still differ from the committed blob (excluded or
ignored files), so an uncommitted "fix" can't hide a broken commit.
`tests/test_local_ci_hook.py` pins this contract.

The hook also carries a canary on the clone's shared `.git/config`: nothing
the gate spawns may write there, because a stray `core.bare = true` breaks
every worktree of the clone at once (issue #74). It compares config *keys*,
not the file's bytes, and ignores exactly the four a parallel session writes
— `branch.<name>.remote`, `.merge`, `.rebase` and `.vscode-merge-base`, which
is what `git push -u`, `git checkout -b --track` and `git worktree add -b`
leave behind (issue #155). Everything else, including the rest of the
`[branch]` section, aborts the push and is named in the output.

Only `core.bare`, `core.worktree`, `core.repositoryformatversion`,
`extensions.*` and `include.*` are written back, because only git's own
plumbing writes those, so reverting one cannot undo a peer session's work.
Anything else — `user.email`, `core.hooksPath`, which this repo's own
`install_hooks.sh` writes — is reported and left exactly as found: the hook
cannot tell its own leak from a peer's legitimate write, and guessing is what
the old whole-file restore did when it wiped out a parallel session's freshly
set upstream. The order of the entries counts too, because git reads the file
top to bottom and an `[include]` moved below `[core]` overrides it without
changing a single value. If the snapshot or the comparison cannot be made at
all, the push is refused rather than waved through; `SKIP_LOCAL_CI=1` is the
deliberate way past.

Bypass a single push with `SKIP_LOCAL_CI=1 git push` if you need to push
work-in-progress.

### Lint

`ruff` is a locked dev dependency, so always invoke it as `uv run ruff` —
that is the version CI uses (`uv.lock`), not whatever your PATH points at.
Both commands run in the `ruff` job of `.github/workflows/ci.yml`, so the
gate applies to every author, not only to those who installed the local
hooks (issue #81):

```bash
uv run ruff check .
uv run ruff format --check .
uv run ruff check --fix . && uv run ruff format .   # to fix
```

Rule selection lives in `pyproject.toml` under `[tool.ruff.lint]` and is
deliberately explicit: ruff's default rule set changes between releases
(59 rules in 0.15.x, 413 in 0.16.0), so relying on the default would make
the gate's verdict depend on which ruff you happen to have installed.
Expanding the selection is welcome — as its own PR, with the resulting
findings actually fixed.

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
