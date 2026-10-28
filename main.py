from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from redis.asyncio import Redis
from starlette.exceptions import HTTPException

from api.auth import router as auth_router
from api.categories import router as categories_router
from api.expenses import router as expenses_router
from core.config import get_settings
from core.errors import (
    http_error_handler,
    unexpected_error_handler,
    validation_error_handler,
)
from db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    app.state.redis = Redis.from_url(
    get_settings().redis_url,
    protocol=2,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
    )
    try:
        yield
    finally:
        try:
            await app.state.redis.aclose()
        finally:
            await engine.dispose()


app = FastAPI(title="Expense Tracker API", lifespan=lifespan)
app.add_exception_handler(HTTPException, http_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unexpected_error_handler)
app.include_router(auth_router, prefix="/api/v1")
app.include_router(categories_router, prefix="/api/v1")
app.include_router(expenses_router, prefix="/api/v1")


@app.get("/")
async def read_root():
    return {"Status": "Working"}


@app.get("/health")
async def health():
    return {"status": "ok"}