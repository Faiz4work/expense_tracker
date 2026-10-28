import asyncio
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select, update

from core.config import get_settings
from core.rate_limit import LoginRateLimiter
from core.security import AUDIENCE, ISSUER, refresh_digest
from main import app
from models.refresh_token import RefreshToken


AUTH = "/api/v1/auth"


def bearer(pair):
    return {"Authorization": f"Bearer {pair['access_token']}"}


def decoded(pair):
    return jwt.decode(
        pair["access_token"],
        get_settings().jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        audience=AUDIENCE,
        issuer=ISSUER,
    )


@pytest_asyncio.fixture
async def account(client, valid_payload):
    response = await client.post(f"{AUTH}/register", json=valid_payload)
    assert response.status_code == 201
    return {
        "user": response.json(),
        "credentials": {
            "email": "faiz@example.com",
            "password": valid_payload["password"],
        },
    }


@pytest_asyncio.fixture
async def tokens(client, account):
    response = await client.post(f"{AUTH}/login", json=account["credentials"])
    assert response.status_code == 200
    return response.json()


async def test_login_issues_valid_tokens(client, account, tokens, session_factory):
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 900
    claims = decoded(tokens)
    assert claims["sub"] == str(account["user"]["id"])
    assert claims["type"] == "access"
    assert claims["exp"] - claims["iat"] == 900
    assert claims["jti"]

    response = await client.get(f"{AUTH}/me", headers=bearer(tokens))
    assert response.status_code == 200
    assert response.json() == account["user"]

    async with session_factory() as db:
        row = await db.get(RefreshToken, refresh_digest(tokens["refresh_token"]))
        assert row is not None
        assert row.token_hash != tokens["refresh_token"]
        remaining = row.expires_at - datetime.now(timezone.utc)
        assert timedelta(days=7) - timedelta(minutes=1) < remaining <= timedelta(days=7)


@pytest.mark.parametrize("case", ["wrong_password", "unknown_email"])
async def test_bad_credentials_return_generic_401(client, account, case):
    credentials = account["credentials"].copy()
    if case == "wrong_password":
        credentials["password"] = "incorrect-password"
    else:
        credentials["email"] = "unknown@example.com"
    response = await client.post(f"{AUTH}/login", json=credentials)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert response.json()["error"]["message"] == "Invalid email or password."


async def test_login_normalizes_email(client, account):
    credentials = {**account["credentials"], "email": "FAIZ@EXAMPLE.COM"}
    response = await client.post(f"{AUTH}/login", json=credentials)
    assert response.status_code == 200


async def test_sixth_failed_login_is_limited(client, account):
    credentials = {**account["credentials"], "password": "incorrect"}
    for _ in range(5):
        response = await client.post(f"{AUTH}/login", json=credentials)
        assert response.status_code == 401

    response = await client.post(f"{AUTH}/login", json=credentials)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_logins"
    assert 1 <= int(response.headers["Retry-After"]) <= 900

    # A different email has its own limit.
    response = await client.post(
        f"{AUTH}/login", json={**credentials, "email": "another@example.com"}
    )
    assert response.status_code == 401


async def test_old_failures_expire_without_waiting(client, account, redis_client):
    limiter = LoginRateLimiter(redis_client, get_settings().redis_namespace)
    seconds, microseconds = await redis_client.time()
    now_ms = seconds * 1000 + microseconds // 1000
    await redis_client.zadd(
        limiter.key(account["credentials"]["email"]),
        {f"old-{i}": now_ms - 901000 + i for i in range(5)},
    )
    response = await client.post(f"{AUTH}/login", json=account["credentials"])
    assert response.status_code == 200


async def test_parallel_failed_logins_cannot_bypass_limit(client, account):
    credentials = {**account["credentials"], "password": "incorrect"}
    responses = await asyncio.gather(
        *(client.post(f"{AUTH}/login", json=credentials) for _ in range(6))
    )
    assert sorted(r.status_code for r in responses) == [401] * 5 + [429]


async def test_login_fails_closed_when_redis_is_unavailable(client, account, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise RedisConnectionError("simulated outage")

    monkeypatch.setattr(app.state.redis, "eval", unavailable)
    response = await client.post(f"{AUTH}/login", json=account["credentials"])
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "auth_unavailable"


async def test_refresh_rotates_token(client, tokens, session_factory):
    response = await client.post(
        f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 200
    new_pair = response.json()
    assert new_pair["refresh_token"] != tokens["refresh_token"]
    assert new_pair["access_token"] != tokens["access_token"]
    async with session_factory() as db:
        old = await db.get(RefreshToken, refresh_digest(tokens["refresh_token"]))
        new = await db.get(RefreshToken, refresh_digest(new_pair["refresh_token"]))
        assert old.used_at is not None
        assert new.used_at is None
        assert new.revoked_at is None


async def test_refresh_reuse_revokes_all_user_tokens(client, account, tokens, session_factory):
    other_response = await client.post(f"{AUTH}/login", json=account["credentials"])
    assert other_response.status_code == 200
    other_pair = other_response.json()

    rotated_response = await client.post(
        f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert rotated_response.status_code == 200
    rotated_pair = rotated_response.json()

    replay = await client.post(
        f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401

    for pair in [tokens, other_pair, rotated_pair]:
        response = await client.get(f"{AUTH}/me", headers=bearer(pair))
        assert response.status_code == 401

    for pair in [other_pair, rotated_pair]:
        response = await client.post(
            f"{AUTH}/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert response.status_code == 401

    async with session_factory() as db:
        rows = (await db.scalars(select(RefreshToken))).all()
        assert rows
        assert all(row.revoked_at is not None for row in rows)


async def test_parallel_refresh_triggers_reuse_protection(client, tokens):
    responses = await asyncio.gather(
        client.post(f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]}),
        client.post(f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]}),
    )
    assert sorted(r.status_code for r in responses) == [200, 401]
    pair = next(r.json() for r in responses if r.status_code == 200)
    response = await client.get(f"{AUTH}/me", headers=bearer(pair))
    assert response.status_code == 401
    response = await client.post(
        f"{AUTH}/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert response.status_code == 401


async def test_unknown_refresh_token_is_rejected(client):
    response = await client.post(
        f"{AUTH}/refresh", json={"refresh_token": "not-a-real-refresh-token"}
    )
    assert response.status_code == 401


async def test_expired_refresh_token_is_rejected(client, tokens, session_factory):
    async with session_factory() as db:
        async with db.begin():
            await db.execute(
                update(RefreshToken)
                .where(RefreshToken.token_hash == refresh_digest(tokens["refresh_token"]))
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
    response = await client.post(
        f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401


async def test_logout_revokes_current_refresh_and_is_idempotent(client, tokens):
    body = {"refresh_token": tokens["refresh_token"]}
    response = await client.post(f"{AUTH}/logout", json=body)
    assert response.status_code == 204
    assert response.content == b""
    response = await client.post(f"{AUTH}/logout", json=body)
    assert response.status_code == 204
    response = await client.post(f"{AUTH}/refresh", json=body)
    assert response.status_code == 401
    # Ordinary logout revokes the refresh token. Access lasts up to 15 minutes.
    response = await client.get(f"{AUTH}/me", headers=bearer(tokens))
    assert response.status_code == 200


async def test_logout_does_not_revoke_other_device_refresh(client, account, tokens):
    response = await client.post(f"{AUTH}/login", json=account["credentials"])
    assert response.status_code == 200
    other_pair = response.json()
    response = await client.post(
        f"{AUTH}/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 204
    response = await client.post(
        f"{AUTH}/refresh", json={"refresh_token": other_pair["refresh_token"]}
    )
    assert response.status_code == 200


async def test_protected_endpoint_requires_access_token(client):
    response = await client.get(f"{AUTH}/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("case", ["expired", "wrong_signature", "wrong_type"])
async def test_invalid_access_tokens_are_rejected(client, tokens, case):
    claims = decoded(tokens)
    key = get_settings().jwt_secret.get_secret_value()
    if case == "expired":
        claims["exp"] = int(datetime.now(timezone.utc).timestamp()) - 1
    elif case == "wrong_signature":
        key = "an-incorrect-test-signing-key-of-sufficient-length"
    else:
        claims["type"] = "refresh"
    token = jwt.encode(claims, key, algorithm="HS256")
    response = await client.get(
        f"{AUTH}/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


async def test_access_token_missing_expiration_is_rejected(client, tokens):
    claims = decoded(tokens)
    claims.pop("exp")
    token = jwt.encode(
        claims, get_settings().jwt_secret.get_secret_value(), algorithm="HS256"
    )
    response = await client.get(
        f"{AUTH}/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401