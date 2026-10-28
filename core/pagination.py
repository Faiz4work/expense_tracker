import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import jwt
from fastapi import HTTPException

from core.config import get_settings
from schemas.expense import MAX_ID, ExpenseListQuery


CURSOR_ISSUER = "expense-tracker"
CURSOR_AUDIENCE = "expense-pagination"


def filter_hash(query: ExpenseListQuery) -> str:
    values = query.model_dump(mode="json", exclude={"cursor", "limit"})
    for field in ("min_amount", "max_amount"):
        amount = getattr(query, field)
        if amount is not None:
            values[field] = format(amount.quantize(Decimal("0.01")), "f")
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def encode_cursor(user_id: int, filters: str, spent_on: date, expense_id: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": CURSOR_ISSUER,
            "aud": CURSOR_AUDIENCE,
            "sub": str(user_id),
            "kind": "expense_cursor",
            "filters": filters,
            "spent_on": spent_on.isoformat(),
            "id": expense_id,
            "iat": now,
            "exp": now + timedelta(hours=24),
        },
        get_settings().jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


def decode_cursor(token: str, user_id: int, filters: str) -> tuple[date, int]:
    try:
        claims = jwt.decode(
            token,
            get_settings().jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=CURSOR_ISSUER,
            audience=CURSOR_AUDIENCE,
            options={
                "require": [
                    "iss", "aud", "sub", "kind", "filters", "spent_on", "id", "iat", "exp"
                ]
            },
        )
        expense_id = claims["id"]
        if (
            claims["kind"] != "expense_cursor"
            or claims["sub"] != str(user_id)
            or claims["filters"] != filters
            or type(expense_id) is not int
            or not 1 <= expense_id <= MAX_ID
        ):
            raise ValueError("Invalid cursor claims")
        return date.fromisoformat(claims["spent_on"]), expense_id
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_cursor",
                "message": "Cursor is invalid, expired, or belongs to different filters/user.",
            },
        ) from None