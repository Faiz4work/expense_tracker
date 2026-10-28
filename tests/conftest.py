import os
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from dotenv import dotenv_values
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]

test_env = dotenv_values(PROJECT_ROOT / ".env.test")

TEST_DATABASE_URL = (
    os.environ.get("TEST_DATABASE_URL")
    or test_env.get("TEST_DATABASE_URL")
)

if not TEST_DATABASE_URL:
    raise RuntimeError(
        "Set TEST_DATABASE_URL in .env.test or the environment."
    )

database_url = make_url(TEST_DATABASE_URL)

if (
    database_url.drivername != "postgresql+asyncpg"
    or database_url.database != "expense_tracker_test"
):
    raise RuntimeError(
        "Tests require async PostgreSQL database expense_tracker_test."
    )

# Set this before importing the application.
# This affects only the pytest process.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from db.base import Base
from db.session import get_db
from main import app


@pytest.fixture(scope="session")
def migrated_database():
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")


async def clear_test_data(engine):
    preparer = engine.sync_engine.dialect.identifier_preparer

    table_names = ", ".join(
        preparer.format_table(table)
        for table in Base.metadata.sorted_tables
    )

    if not table_names:
        raise RuntimeError("No application models are registered.")

    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"TRUNCATE TABLE {table_names} "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest_asyncio.fixture
async def session_factory(migrated_database):
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        hide_parameters=True,
    )

    factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )

    try:
        await clear_test_data(engine)
        yield factory
    finally:
        try:
            await clear_test_data(engine)
        finally:
            await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory):
    async def override_get_db():
        async with session_factory() as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_get_db

    try:
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=manager.app)

            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as test_client:
                yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.fixture
def valid_payload():
    return {
        "email": "Faiz@example.com",
        "password": "ExamplePassword123!",
        "base_currency": " pkr ",
    }