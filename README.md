# expense_tracker

## Database configuration

Copy `.env.example` to `.env` in the project root and replace the placeholder
credentials with your local PostgreSQL connection details. The database must
already exist. Keep `.env` local; it is excluded by `.gitignore`.

`core/config.py` loads `DATABASE_URL` using Pydantic Settings. Environment variables
take priority over `.env`. The `.env` path is relative to the project files, so it
does not depend on which directory you run commands from. The URL must use the
`postgresql+asyncpg://` prefix. URL-encode special characters in credentials.

## Alembic setup

- `db/base.py` defines the shared SQLAlchemy `Base`. All database models must
  inherit from it.
- Import each new model module in `models/__init__.py` so Alembic can discover its
  tables.
- `migrations/env.py` loads the settings, imports the models, and passes
  `Base.metadata` to Alembic. Online migrations use SQLAlchemy's async engine;
  offline SQL generation uses the same configured URL.
- `alembic.ini` contains migration configuration without database credentials.

No application tables or initial migration have been added yet. Once models are
defined and imported, generate a migration, review it, and apply it to your local
database. Migrations are run separately from the API server.

## AI assistance

Codex helped review the initial setup and implemented the application settings,
shared SQLAlchemy Base, Alembic configuration, and this setup documentation.
