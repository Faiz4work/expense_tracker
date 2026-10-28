from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.rate_limit import LoginRateLimiter
from core.security import decode_access_token, invalid_token
from db.session import get_db
from models.user import User


bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    if credentials is None:
        raise invalid_token()

    user_id, version = decode_access_token(credentials.credentials)
    async with db.begin():
        user = await db.get(User, user_id)

    if user is None or user.token_version != version:
        raise invalid_token()
    return user


async def get_login_limiter(request: Request) -> LoginRateLimiter:
    return LoginRateLimiter(
        request.app.state.redis,
        get_settings().redis_namespace,
    )