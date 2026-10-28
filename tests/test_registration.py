import asyncio
from datetime import datetime

import pytest
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from core.security import password_hasher
from models.user import User


REGISTER_URL = "/api/v1/auth/register"


async def user_count(session_factory):
    async with session_factory() as session:
        return await session.scalar(
            select(func.count()).select_from(User)
        )


async def test_registration_saves_user_securely(
    client,
    session_factory,
    valid_payload,
):
    response = await client.post(
        REGISTER_URL,
        json=valid_payload,
    )

    assert response.status_code == 201

    body = response.json()

    assert set(body) == {
        "id",
        "email",
        "base_currency",
        "created_at",
    }
    assert body["email"] == "faiz@example.com"
    assert body["base_currency"] == "PKR"
    assert valid_payload["password"] not in response.text

    created_at = datetime.fromisoformat(body["created_at"])
    assert created_at.tzinfo is not None

    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(User.id == body["id"])
        )

        assert user is not None
        assert user.email == body["email"]
        assert user.base_currency == "PKR"
        assert user.password_hash != valid_payload["password"]
        assert user.password_hash.startswith("$argon2id$")

        assert await run_in_threadpool(
            password_hasher.verify,
            valid_payload["password"],
            user.password_hash,
        )

        assert not await run_in_threadpool(
            password_hasher.verify,
            "AnIncorrectPassword!",
            user.password_hash,
        )


@pytest.mark.parametrize(
    "second_email",
    [
        "faiz@example.com",
        "FAIZ@EXAMPLE.COM",
    ],
)
async def test_duplicate_email_is_rejected(
    client,
    session_factory,
    valid_payload,
    second_email,
):
    first = await client.post(
        REGISTER_URL,
        json=valid_payload,
    )
    assert first.status_code == 201

    second = await client.post(
        REGISTER_URL,
        json={
            **valid_payload,
            "email": second_email,
        },
    )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "email_exists"
    assert await user_count(session_factory) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("email", "invalid-email"),
        ("password", "short"),
        ("password", "x" * 129),
        ("base_currency", "XYZ"),
    ],
)
async def test_invalid_registration_is_rejected(
    client,
    session_factory,
    valid_payload,
    field,
    value,
):
    payload = {**valid_payload, field: value}

    response = await client.post(
        REGISTER_URL,
        json=payload,
    )

    assert response.status_code == 422

    error = response.json()["error"]

    assert error["code"] == "validation_error"
    assert error["details"]

    assert any(
        detail["field"] == f"body.{field}"
        for detail in error["details"]
    )

    assert all(
        "input" not in detail
        for detail in error["details"]
    )

    assert payload["password"] not in response.text
    assert await user_count(session_factory) == 0


@pytest.mark.parametrize(
    "missing_field",
    ["email", "password", "base_currency"],
)
async def test_missing_required_field_is_rejected(
    client,
    session_factory,
    valid_payload,
    missing_field,
):
    payload = valid_payload.copy()
    payload.pop(missing_field)

    response = await client.post(
        REGISTER_URL,
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert await user_count(session_factory) == 0


async def test_unknown_fields_are_rejected(
    client,
    session_factory,
    valid_payload,
):
    response = await client.post(
        REGISTER_URL,
        json={
            **valid_payload,
            "is_admin": True,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert await user_count(session_factory) == 0


async def test_parallel_registration_creates_one_user(
    client,
    session_factory,
    valid_payload,
):
    responses = await asyncio.gather(
        client.post(REGISTER_URL, json=valid_payload),
        client.post(REGISTER_URL, json=valid_payload),
    )

    assert sorted(
        response.status_code for response in responses
    ) == [201, 409]

    duplicate_response = next(
        response
        for response in responses
        if response.status_code == 409
    )

    assert (
        duplicate_response.json()["error"]["code"]
        == "email_exists"
    )
    assert await user_count(session_factory) == 1