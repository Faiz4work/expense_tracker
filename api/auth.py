import hashlib
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_current_user, get_login_limiter
from core.rate_limit import LoginRateLimiter
from core.security import (
    DUMMY_HASH,
    hash_password,
    invalid_token,
    issue_tokens,
    refresh_digest,
    verify_password,
)
from db.session import get_db
from models.refresh_token import RefreshToken
from models.user import User
from schemas.auth import LoginRequest, RefreshRequest, TokenPair
from schemas.user import UserCreate, UserRead


router = APIRouter(prefix="/auth", tags=["Authentication"])
Db = Annotated[AsyncSession, Depends(get_db)]
Limiter = Annotated[LoginRateLimiter, Depends(get_login_limiter)]


@router.post("/register", response_model=UserRead, status_code=201)
async def register(payload: UserCreate, db: Db) -> User:
    password_hash = await hash_password(payload.password.get_secret_value())
    statement = (
        insert(User)
        .values(
            email=str(payload.email),
            password_hash=password_hash,
            base_currency=payload.base_currency,
        )
        .on_conflict_do_nothing(index_elements=[User.email])
        .returning(User)
    )
    async with db.begin():
        user = await db.scalar(statement)
        if user is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "email_exists",
                    "message": "This email is already registered.",
                },
            )
    return user


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: Db, limiter: Limiter) -> TokenPair:
    email = str(payload.email)
    # Python's hash() varies between processes. SHA-256 gives every server
    # the same PostgreSQL advisory-lock key for this normalized email.
    lock_key = int.from_bytes(
        hashlib.sha256(f"login:{email}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    pair = None
    async with db.begin():
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
        )
        await limiter.check(email)
        user = await db.scalar(
            select(User).where(User.email == email).with_for_update()
        )
        valid_password = await verify_password(
            payload.password.get_secret_value(),
            user.password_hash if user is not None else DUMMY_HASH,
        )
        if user is None or not valid_password:
            await limiter.record_failure(email)
        else:
            pair = issue_tokens(db, user)

    if pair is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_credentials",
                "message": "Invalid email or password.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    return pair


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: Db) -> TokenPair:
    digest = refresh_digest(payload.refresh_token.get_secret_value())
    pair = None

    async with db.begin():
        # Read only the owner ID first; do not cache a token ORM object before
        # obtaining the user lock, because another request may rotate it.
        user_id = await db.scalar(
            select(RefreshToken.user_id).where(RefreshToken.token_hash == digest)
        )
        if user_id is not None:
            user = await db.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            token = await db.get(RefreshToken, digest)
            now = datetime.now(timezone.utc)

            if user is not None and token is not None:
                if token.used_at is not None:
                    user.token_version += 1
                    await db.execute(
                        update(RefreshToken)
                        .where(RefreshToken.user_id == user.id)
                        .values(revoked_at=now)
                    )
                elif token.revoked_at is None and token.expires_at > now:
                    token.used_at = now
                    pair = issue_tokens(db, user)

    # Raise AFTER the transaction commits. Raising inside it would roll back
    # the security changes made when a used refresh token is replayed.
    if pair is None:
        raise invalid_token()
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, db: Db) -> Response:
    digest = refresh_digest(payload.refresh_token.get_secret_value())
    valid = False

    async with db.begin():
        user_id = await db.scalar(
            select(RefreshToken.user_id).where(RefreshToken.token_hash == digest)
        )
        if user_id is not None:
            user = await db.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            token = await db.get(RefreshToken, digest)
            now = datetime.now(timezone.utc)
            if (
                user is not None
                and token is not None
                and token.used_at is None
                and token.expires_at > now
            ):
                token.revoked_at = token.revoked_at or now
                valid = True

    if not valid:
        raise invalid_token()
    return Response(status_code=204)


@router.get("/me", response_model=UserRead)
async def me(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user