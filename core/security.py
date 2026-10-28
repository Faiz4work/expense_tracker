import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi import HTTPException
from pwdlib import PasswordHash
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from core.config import get_settings
from models.refresh_token import RefreshToken
from models.user import User
from schemas.auth import TokenPair


ISSUER = "expense-tracker"
AUDIENCE = "expense-tracker-api"
ACCESS_LIFETIME = timedelta(minutes=15)
REFRESH_LIFETIME = timedelta(days=7)
password_hasher = PasswordHash.recommended()
# Generated once during module loading, outside async request handlers.
DUMMY_HASH = password_hasher.hash("dummy-password-for-timing-equalization")


async def hash_password(password: str) -> str:
    return await run_in_threadpool(password_hasher.hash, password)


async def verify_password(password: str, password_hash: str) -> bool:
    return await run_in_threadpool(password_hasher.verify, password, password_hash)


def invalid_token() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "invalid_token", "message": "Invalid or expired token."},
        headers={"WWW-Authenticate": "Bearer"},
    )


def refresh_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_tokens(db: AsyncSession, user: User) -> TokenPair:
    """Call inside a transaction while holding this user's row lock."""
    now = datetime.now(timezone.utc)
    refresh_token = secrets.token_urlsafe(48)
    db.add(
        RefreshToken(
            token_hash=refresh_digest(refresh_token),
            user_id=user.id,
            expires_at=now + REFRESH_LIFETIME,
        )
    )

    access_token = jwt.encode(
        {
            "sub": str(user.id),
            "ver": user.token_version,
            "type": "access",
            "jti": str(uuid4()),
            "iat": now,
            "exp": now + ACCESS_LIFETIME,
            "iss": ISSUER,
            "aud": AUDIENCE,
        },
        get_settings().jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    return TokenPair(access_token=access_token, refresh_token=refresh_token)


def decode_access_token(token: str) -> tuple[int, int]:
    try:
        claims = jwt.decode(
            token,
            get_settings().jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={
                "require": ["sub", "ver", "type", "jti", "iat", "exp", "iss", "aud"]
            },
        )
        user_id = int(claims["sub"])
        version = claims["ver"]
        if (
            claims["type"] != "access"
            or user_id < 1
            or type(version) is not int
            or version < 0
        ):
            raise ValueError("Invalid access-token claims")
        return user_id, version
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        raise invalid_token() from None