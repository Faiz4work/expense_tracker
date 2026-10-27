from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import hash_password
from db.session import get_db
from models.user import User
from schemas.user import UserCreate, UserRead


router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
)
async def register(payload: UserCreate, db: Annotated[AsyncSession, Depends(get_db)],) -> User:
    password_hash = await hash_password(
        payload.password.get_secret_value()
    )

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
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "email_exists",
                    "message": "This email is already registered.",
                },
            )

    return user