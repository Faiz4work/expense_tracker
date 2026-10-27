from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from api.auth import router as auth_router
from core.errors import (
    http_error_handler,
    unexpected_error_handler,
    validation_error_handler,
)
from db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(
    title="Expense Tracker API",
    lifespan=lifespan,
)

app.add_exception_handler(
    HTTPException,
    http_error_handler,
)
app.add_exception_handler(
    RequestValidationError,
    validation_error_handler,
)
app.add_exception_handler(
    Exception,
    unexpected_error_handler,
)

app.include_router(auth_router, prefix="/api/v1")


@app.get("/")
async def read_root():
    return {"Status": "Working"}


@app.get("/health")
async def health():
    return {"status": "ok"}