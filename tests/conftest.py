import os
import secrets
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from dotenv import dotenv_values
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
test_env = dotenv_values(PROJECT_ROOT / ".env.test")
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or test_env.get("TEST_DATABASE_URL")
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL") or test_env.get("TEST_REDIS_URL")

if not TEST_DATABASE_URL:
    raise RuntimeError("Set TEST_DATABASE_URL in .env.test or the environment.")
database_url = make_url(TEST_DATABASE_URL)
if (
    database_url.drivername != "postgresql+asyncpg"
    or database_url.database != "expense_tracker_test"
):
    raise RuntimeError("Tests require async PostgreSQL database expense_tracker_test.")
if not TEST_REDIS_URL:
    raise RuntimeError("Set TEST_REDIS_URL in .env.test or the environment.")
redis_url = urlparse(TEST_REDIS_URL)
if redis_url.scheme not in {"redis", "rediss"}:
    raise RuntimeError("TEST_REDIS_URL must use redis:// or rediss://.")

TEST_NAMESPACE = f"expense-tests:{uuid4().hex}"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["REDIS_NAMESPACE"] = TEST_NAMESPACE
os.environ["JWT_SECRET"] = secrets.token_hex(32)

# Import the app only after replacing settings in this pytest process.
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
        preparer.format_table(table) for table in Base.metadata.sorted_tables
    )
    if not table_names:
        raise RuntimeError("No application models are registered.")
    async with engine.begin() as connection:
        await connection.execute(
            text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE")
        )


@pytest_asyncio.fixture
async def session_factory(migrated_database):
    engine = create_async_engine(
        TEST_DATABASE_URL, poolclass=NullPool, hide_parameters=True
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await clear_test_data(engine)
        yield factory
    finally:
        try:
            await clear_test_data(engine)
        finally:
            await engine.dispose()


async def clear_test_redis(redis):
    # Delete only keys from this pytest run; never flush an entire Redis DB.
    keys = [key async for key in redis.scan_iter(match=f"{TEST_NAMESPACE}:*")]
    if keys:
        await redis.unlink(*keys)


@pytest_asyncio.fixture
async def redis_client():
    redis = Redis.from_url(
    TEST_REDIS_URL,
    protocol=2,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
    )
    try:
        await redis.ping()
        await clear_test_redis(redis)
        yield redis
    finally:
        try:
            await clear_test_redis(redis)
        finally:
            await redis.aclose()


@pytest_asyncio.fixture
async def client(session_factory, redis_client):
    async def override_get_db():
        async with session_factory() as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_get_db
    try:
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=manager.app)
            async with AsyncClient(transport=transport, base_url="http://test") as test_client:
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