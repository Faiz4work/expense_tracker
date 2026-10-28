import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_current_user
from db.session import get_db
from models.category import Category
from models.expense import Expense
from models.user import User
from schemas.expense import MAX_ID, ExpenseCreate, ExpenseRead, ExpenseUpdate


router = APIRouter(prefix="/expenses", tags=["Expenses"])
Db = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
ExpenseId = Annotated[int, Path(gt=0, le=MAX_ID)]


def not_found(resource: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "not_found",
            "message": f"{resource} not found.",
        },
    )


def make_etag(version: int) -> str:
    return f'"{version}"'


def parse_version(if_match: str | None) -> int:
    if if_match is None:
        raise HTTPException(
            status_code=428,
            detail={
                "code": "precondition_required",
                "message": "Send the expense ETag in the If-Match header.",
            },
        )
    match = re.fullmatch(r'"([1-9][0-9]{0,9})"', if_match.strip())
    if match is None or int(match.group(1)) > MAX_ID:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_etag",
                "message": 'If-Match must be one quoted version, for example "1".',
            },
        )
    return int(match.group(1))


async def require_category(db: AsyncSession, user_id: int, category_id: int) -> None:
    category_id_found = await db.scalar(
        select(Category.id).where(
            Category.id == category_id,
            Category.user_id == user_id,
        )
    )
    if category_id_found is None:
        raise not_found("Category")


async def require_expense(db: AsyncSession, user_id: int, expense_id: int) -> None:
    expense_id_found = await db.scalar(
        select(Expense.id).where(
            Expense.id == expense_id,
            Expense.user_id == user_id,
        )
    )
    if expense_id_found is None:
        raise not_found("Expense")


@router.post("", response_model=ExpenseRead, status_code=201)
async def create_expense(
    payload: ExpenseCreate,
    response: Response,
    db: Db,
    user: CurrentUser,
) -> Expense:
    async with db.begin():
        await require_category(db, user.id, payload.category_id)
        result = await db.execute(
            insert(Expense)
            .values(user_id=user.id, **payload.model_dump())
            .returning(Expense)
        )
        expense = result.scalar_one()
    response.headers["ETag"] = make_etag(expense.version)
    return expense


@router.get("/{expense_id}", response_model=ExpenseRead)
async def get_expense(
    expense_id: ExpenseId,
    response: Response,
    db: Db,
    user: CurrentUser,
) -> Expense:
    async with db.begin():
        expense = await db.scalar(
            select(Expense).where(
                Expense.id == expense_id,
                Expense.user_id == user.id,
            )
        )
        if expense is None:
            raise not_found("Expense")
    response.headers["ETag"] = make_etag(expense.version)
    return expense


@router.patch("/{expense_id}", response_model=ExpenseRead)
async def update_expense(
    expense_id: ExpenseId,
    payload: ExpenseUpdate,
    response: Response,
    db: Db,
    user: CurrentUser,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Expense:
    changes = payload.model_dump(exclude_unset=True)
    async with db.begin():
        # This existence check makes foreign or missing expenses return 404.
        # The version check itself happens in the UPDATE below.
        await require_expense(db, user.id, expense_id)
        expected_version = parse_version(if_match)
        if "category_id" in changes:
            await require_category(db, user.id, changes["category_id"])

        expense = await db.scalar(
            update(Expense)
            .where(
                Expense.id == expense_id,
                Expense.user_id == user.id,
                Expense.version == expected_version,
            )
            .values(
                **changes,
                version=Expense.version + 1,
                updated_at=func.now(),
            )
            .returning(Expense)
        )
        if expense is None:
            # A simultaneous deletion is 404; an existing newer version is 412.
            await require_expense(db, user.id, expense_id)
            raise HTTPException(
                status_code=412,
                detail={
                    "code": "version_conflict",
                    "message": "Expense changed. Reload it before retrying the edit.",
                },
            )
    response.headers["ETag"] = make_etag(expense.version)
    return expense


@router.delete("/{expense_id}", status_code=204)
async def delete_expense(expense_id: ExpenseId, db: Db, user: CurrentUser,) -> Response:
    async with db.begin():
        deleted_id = await db.scalar(
            delete(Expense)
            .where(
                Expense.id == expense_id,
                Expense.user_id == user.id,
            )
            .returning(Expense.id)
        )
        if deleted_id is None:
            raise not_found("Expense")
    return Response(status_code=204)