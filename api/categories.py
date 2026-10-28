from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_current_user
from db.session import get_db
from models.category import Category
from models.user import User
from schemas.category import CategoryCreate, CategoryRead


router = APIRouter(prefix="/categories", tags=["Categories"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post("", response_model=CategoryRead, status_code=201)
async def create_category(
    payload: CategoryCreate,
    db: Db,
    user: CurrentUser,
) -> Category:
    async with db.begin():
        category = await db.scalar(
            insert(Category)
            .values(user_id=user.id, **payload.model_dump())
            .on_conflict_do_nothing(
                index_elements=[Category.user_id, Category.name]
            )
            .returning(Category)
        )
        if category is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "category_exists",
                    "message": "You already have a category with this name.",
                },
            )
    return category


@router.get("", response_model=list[CategoryRead])
async def list_categories(db: Db, user: CurrentUser) -> list[Category]:
    async with db.begin():
        categories = await db.scalars(
            select(Category)
            .where(Category.user_id == user.id)
            .order_by(Category.name, Category.id)
        )
        return list(categories.all())