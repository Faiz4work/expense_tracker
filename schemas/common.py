from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field


MAX_MONEY = Decimal("9999999999999999.99")


def parse_money(value: object) -> Decimal:
    # Fractional JSON numbers are decoded as floats. Require a string instead.
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('Send money as a decimal string, for example "25.50".')
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid monetary amount.") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Money must be a finite amount greater than zero.")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Money must have at most two decimal places.")
    if amount > MAX_MONEY:
        raise ValueError("Money exceeds the supported maximum.")
    return amount


def normalize_currency(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


Money = Annotated[
    Decimal,
    BeforeValidator(parse_money),
    Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False),
]
Currency = Annotated[
    Literal["PKR", "AED", "USD", "EUR"],
    BeforeValidator(normalize_currency),
]