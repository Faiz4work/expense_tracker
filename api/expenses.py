import re
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_current_user
from core.idempotency import expense_request_hash, lock_expense_request
from core.pagination import decode_cursor, encode_cursor, filter_hash
from db.session import get_db
from models.category import Category
from models.expense import Expense
from models.idempotency_key import ExpenseIdempotency
from models.user import User
from schemas.expense import (
    MAX_ID, ExpenseCreate, ExpenseListQuery, ExpensePage, ExpenseRead, ExpenseUpdate,
)


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
    db: Db,
    user: CurrentUser,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ] = None,
) -> Response:
    request_hash = expense_request_hash(payload)
    async with db.begin():
        saved = None
        now = None
        if idempotency_key is not None:
            # All API servers share this PostgreSQL transaction lock.
            await lock_expense_request(db, user.id, idempotency_key)
            # clock_timestamp is evaluated after waiting, unlike transaction now().
            now = await db.scalar(select(func.clock_timestamp()))
            saved = await db.get(ExpenseIdempotency, (user.id, idempotency_key))
            if saved is not None and saved.expires_at > now:
                if saved.request_hash != request_hash:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "idempotency_key_reused",
                            "message": "This Idempotency-Key was used with a different body.",
                        },
                    )
                return Response(
                    content=saved.response_body,
                    status_code=201,
                    media_type="application/json",
                    headers={"ETag": saved.etag},
                )

        await require_category(db, user.id, payload.category_id)
        result = await db.execute(
            insert(Expense)
            .values(user_id=user.id, **payload.model_dump())
            .returning(Expense)
        )
        expense = result.scalar_one()
        body = ExpenseRead.model_validate(expense).model_dump_json()
        etag = make_etag(expense.version)

        if idempotency_key is not None:
            assert now is not None
            # Expense and saved response commit together, or both roll back.
            if saved is None:
                saved = ExpenseIdempotency(user_id=user.id, key=idempotency_key)
                db.add(saved)
            saved.request_hash = request_hash
            saved.response_body = body
            saved.etag = etag
            saved.expires_at = now + timedelta(hours=24)

    return Response(
        content=body,
        status_code=201,
        media_type="application/json",
        headers={"ETag": etag},
    )


@router.get("", response_model=ExpensePage)
async def list_expenses(
    query: Annotated[ExpenseListQuery, Query()],
    db: Db,
    user: CurrentUser,
) -> ExpensePage:
    fingerprint = filter_hash(query)
    position = (
        decode_cursor(query.cursor, user.id, fingerprint)
        if query.cursor is not None else None
    )
    async with db.begin():
        if query.category_id is not None:
            await require_category(db, user.id, query.category_id)

        statement = select(Expense).where(Expense.user_id == user.id)
        if query.category_id is not None:
            statement = statement.where(Expense.category_id == query.category_id)
        if query.date_from is not None:
            statement = statement.where(Expense.spent_on >= query.date_from)
        if query.date_to is not None:
            statement = statement.where(Expense.spent_on <= query.date_to)
        if query.min_amount is not None:
            statement = statement.where(Expense.amount >= query.min_amount)
        if query.max_amount is not None:
            statement = statement.where(Expense.amount <= query.max_amount)
        if query.currency is not None:
            statement = statement.where(Expense.currency == query.currency)
        if query.q is not None:
            statement = statement.where(
                Expense.description.icontains(query.q, autoescape=True)
            )
        if position is not None:
            last_date, last_id = position
            statement = statement.where(
                tuple_(Expense.spent_on, Expense.id) < tuple_(last_date, last_id)
            )
        rows = list((await db.scalars(
            statement.order_by(Expense.spent_on.desc(), Expense.id.desc())
            .limit(query.limit + 1)
        )).all())

    items = rows[:query.limit]
    next_cursor = None
    if len(rows) > query.limit:
        last = items[-1]
        next_cursor = encode_cursor(user.id, fingerprint, last.spent_on, last.id)
    return ExpensePage(
        items=[ExpenseRead.model_validate(expense) for expense in items],
        next_cursor=next_cursor,
    )


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