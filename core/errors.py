import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


logger = logging.getLogger(__name__)


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or [],
            }
        },
    )


async def http_error_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {}

    return error_response(
        status_code=exc.status_code,
        code=detail.get("code", "http_error"),
        message=detail.get("message", str(exc.detail)),
        details=detail.get("details"),
        headers=exc.headers,
    )


async def validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    details = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]

    return error_response(
        status_code=422,
        code="validation_error",
        message="Request validation failed.",
        details=details,
    )


async def unexpected_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    logger.error(
        "Unhandled application error",
        exc_info=(type(exc), exc, exc.__traceback__),
    )

    return error_response(
        status_code=500,
        code="internal_error",
        message="An unexpected error occurred.",
    )