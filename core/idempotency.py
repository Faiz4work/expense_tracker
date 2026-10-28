import hashlib
import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.expense import ExpenseCreate


def expense_request_hash(payload: ExpenseCreate) -> str:
    values = payload.model_dump(mode="json")
    values["amount"] = format(payload.amount.quantize(Decimal("0.01")), "f")
    encoded = json.dumps(
        values, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def lock_expense_request(db: AsyncSession, user_id: int, key: str) -> None:
    # Stable across API processes. A hash collision only serializes unrelated keys.
    lock_id = int.from_bytes(
        hashlib.sha256(f"expense-create:{user_id}:{key}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id}
    )