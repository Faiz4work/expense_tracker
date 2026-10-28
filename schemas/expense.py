from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.common import Currency, Money


MAX_ID = 2147483647


class ExpenseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: int = Field(gt=0, le=MAX_ID)
    amount: Money
    currency: Currency
    description: str = Field(default="", max_length=2000)
    spent_on: date


class ExpenseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: int | None = Field(default=None, gt=0, le=MAX_ID)
    amount: Money | None = None
    currency: Currency | None = None
    description: str | None = Field(default=None, max_length=2000)
    spent_on: date | None = None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self


class ExpenseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    category_id: int
    amount: Decimal
    currency: str
    description: str
    spent_on: date
    receipt_key: str | None
    version: int
    created_at: datetime
    updated_at: datetime