import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from models.expense import Expense
from models.idempotency_key import ExpenseIdempotency
from schemas.expense import ExpenseRead


URL = "/api/v1/expenses"


async def make_actor(client, email="owner@example.com"):
    credentials = {"email": email, "password": "ExamplePassword123!"}
    registered = await client.post(
        "/api/v1/auth/register", json={**credentials, "base_currency": "PKR"}
    )
    assert registered.status_code == 201, registered.text
    login = await client.post("/api/v1/auth/login", json=credentials)
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    category = await client.post(
        "/api/v1/categories",
        json={"name": "Food", "monthly_budget": "1000.00"},
        headers=headers,
    )
    assert category.status_code == 201, category.text
    return {
        "headers": headers,
        "user_id": registered.json()["id"],
        "category_id": category.json()["id"],
        "payload": {
            "category_id": category.json()["id"],
            "amount": "10.00",
            "currency": "PKR",
            "description": "Lunch",
            "spent_on": "2026-10-29",
        },
    }


@pytest_asyncio.fixture
async def actor(client):
    return await make_actor(client)


async def post_expense(client, actor, key=None, **changes):
    headers = actor["headers"].copy()
    if key is not None:
        headers["Idempotency-Key"] = key
    return await client.post(URL, json={**actor["payload"], **changes}, headers=headers)


async def create(client, actor, **changes):
    response = await post_expense(client, actor, **changes)
    assert response.status_code == 201, response.text
    return response


async def row_counts(session_factory):
    async with session_factory() as db:
        expenses = await db.scalar(select(func.count()).select_from(Expense))
        keys = await db.scalar(select(func.count()).select_from(ExpenseIdempotency))
        return expenses, keys


@pytest_asyncio.fixture
async def listing_data(client, actor):
    category = await client.post(
        "/api/v1/categories", json={"name": "Travel"}, headers=actor["headers"]
    )
    assert category.status_code == 201
    records = [
        {"amount": "10.00", "currency": "USD", "spent_on": "2026-10-29",
         "description": "Coffee 100%_deal"},
        {"amount": "20.00", "currency": "PKR", "spent_on": "2026-10-28",
         "description": "Taxi ride"},
        {"category_id": category.json()["id"], "amount": "30.00", "currency": "USD",
         "spent_on": "2026-10-27", "description": "Coffee 100ABdeal"},
    ]
    return [(await create(client, actor, **record)).json() for record in records]


async def test_empty_listing(client, actor):
    response = await client.get(URL, headers=actor["headers"])
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("params, indexes", [
    ({"category_id": "own"}, [0, 1]),
    ({"date_from": "2026-10-28"}, [0, 1]),
    ({"date_to": "2026-10-28"}, [1, 2]),
    ({"min_amount": "20.00"}, [1, 2]),
    ({"max_amount": "20.00"}, [0, 1]),
    ({"currency": "usd"}, [0, 2]),
    ({"q": "cOfFeE"}, [0, 2]),
    ({"q": "100%_"}, [0]),
    ({"currency": "USD", "min_amount": "25.00", "q": "coffee",
      "date_from": "2026-10-01", "date_to": "2026-10-31"}, [2]),
])
async def test_filters(client, actor, listing_data, params, indexes):
    params = params.copy()
    if params.get("category_id") == "own":
        params["category_id"] = actor["category_id"]
    response = await client.get(URL, params=params, headers=actor["headers"])
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [
        listing_data[index]["id"] for index in indexes
    ]
    assert response.json()["next_cursor"] is None


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"category_id": 0},
    {"date_from": "bad-date"},
    {"date_from": "2026-10-29", "date_to": "2026-10-01"},
    {"min_amount": "30", "max_amount": "10"},
    {"min_amount": "-1"}, {"max_amount": "1.234"},
    {"min_amount": "NaN"}, {"currency": "XYZ"}, {"q": ""}, {"offset": 10},
])
async def test_bad_list_filters(client, actor, params):
    response = await client.get(URL, params=params, headers=actor["headers"])
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


async def test_pagination_with_insertions_and_equal_dates(client, actor):
    originals = [
        (await create(client, actor, spent_on=day)).json()
        for day in ["2026-10-29", "2026-10-29", "2026-10-28", "2026-10-28", "2026-10-27"]
    ]
    first = await client.get(URL, params={"limit": 2}, headers=actor["headers"])
    assert first.status_code == 200
    page = first.json()
    seen = [item["id"] for item in page["items"]]
    assert page["next_cursor"]

    # These sort ahead of the current anchor, so they do not shift older pages.
    newer = (await create(client, actor, spent_on="2026-10-30")).json()
    tied_new = (await create(client, actor, spent_on="2026-10-29")).json()
    # Backdated inserts below the anchor can appear on a later page.
    backdated = (await create(client, actor, spent_on="2026-10-26")).json()
    for _ in range(10):
        cursor = page["next_cursor"]
        if cursor is None:
            break
        response = await client.get(
            URL, params={"limit": 2, "cursor": cursor}, headers=actor["headers"]
        )
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(item["id"] for item in page["items"])
    else:
        pytest.fail("Pagination did not finish")

    expected = sorted(originals + [backdated], key=lambda item: (item["spent_on"], item["id"]), reverse=True)
    assert seen == [item["id"] for item in expected]
    assert len(seen) == len(set(seen))
    assert newer["id"] not in seen and tied_new["id"] not in seen


async def test_cursor_validation_and_ownership(client, actor, listing_data):
    first = await client.get(URL, params={"limit": 1}, headers=actor["headers"])
    cursor = first.json()["next_cursor"]
    assert cursor
    other = await make_actor(client, "other@example.com")
    # Tampering, different filters, and another user cannot reuse this cursor.
    for token, filters, headers in [
        ("not-a-cursor", {}, actor["headers"]),
        (cursor + "broken", {}, actor["headers"]),
        (cursor, {"currency": "USD"}, actor["headers"]),
        (cursor, {}, other["headers"]),
    ]:
        response = await client.get(
            URL, params={"cursor": token, **filters}, headers=headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cursor"
    response = await client.get(
        URL, headers={"Authorization": f"Bearer {cursor}"}
    )
    assert response.status_code == 401


async def test_list_and_category_ownership(client, actor):
    own = await create(client, actor)
    other = await make_actor(client, "other@example.com")
    await create(client, other)
    response = await client.get(URL, headers=actor["headers"])
    assert [item["id"] for item in response.json()["items"]] == [own.json()["id"]]
    response = await client.get(
        URL, params={"category_id": other["category_id"]}, headers=actor["headers"]
    )
    assert response.status_code == 404
    response = await post_expense(client, actor, category_id=other["category_id"])
    assert response.status_code == 404


async def test_categories_are_owned_and_duplicates_are_rejected(client, actor):
    other = await make_actor(client, "other@example.com")
    response = await client.get("/api/v1/categories", headers=actor["headers"])
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [actor["category_id"]]
    assert other["category_id"] != actor["category_id"]
    duplicate = await client.post(
        "/api/v1/categories", json={"name": "Food"}, headers=actor["headers"]
    )
    assert duplicate.status_code == 409


async def test_same_key_replays_exact_response(client, actor, session_factory):
    first = await create(client, actor, key="same-payment")
    second = await create(client, actor, key="same-payment")
    assert first.content == second.content
    assert first.headers["etag"] == second.headers["etag"] == '"1"'
    assert await row_counts(session_factory) == (1, 1)
    async with session_factory() as db:
        saved = await db.get(ExpenseIdempotency, (actor["user_id"], "same-payment"))
        now = await db.scalar(select(func.clock_timestamp()))
        assert timedelta(hours=23, minutes=59) < saved.expires_at - now <= timedelta(hours=24)


async def test_same_key_changed_body_returns_422(client, actor, session_factory):
    await create(client, actor, key="same-payment")
    response = await post_expense(client, actor, key="same-payment", amount="99.00")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert await row_counts(session_factory) == (1, 1)


async def test_normalized_equivalent_money_replays(client, actor, session_factory):
    first = await create(client, actor, key="same-payment", amount="10.0", currency="pkr")
    again = await create(client, actor, key="same-payment", amount="10.00", currency="PKR")
    assert first.content == again.content
    assert await row_counts(session_factory) == (1, 1)


async def test_parallel_same_key_creates_one_expense(client, actor, session_factory):
    responses = await asyncio.gather(
        post_expense(client, actor, key="parallel-payment"),
        post_expense(client, actor, key="parallel-payment"),
    )
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].content == responses[1].content
    assert responses[0].headers["etag"] == responses[1].headers["etag"]
    assert await row_counts(session_factory) == (1, 1)


async def test_parallel_same_key_different_bodies(client, actor, session_factory):
    responses = await asyncio.gather(
        post_expense(client, actor, key="parallel-payment", description="One"),
        post_expense(client, actor, key="parallel-payment", description="Two"),
    )
    assert sorted(response.status_code for response in responses) == [201, 422]
    assert await row_counts(session_factory) == (1, 1)


async def test_different_keys_create_different_expenses(client, actor, session_factory):
    first = await create(client, actor, key="payment-one")
    second = await create(client, actor, key="payment-two")
    assert first.json()["id"] != second.json()["id"]
    assert await row_counts(session_factory) == (2, 2)


async def test_without_key_creates_normally(client, actor, session_factory):
    await create(client, actor)
    await create(client, actor)
    assert await row_counts(session_factory) == (2, 0)


async def test_key_is_scoped_to_user(client, actor, session_factory):
    other = await make_actor(client, "other@example.com")
    first = await create(client, actor, key="same-key")
    second = await create(client, other, key="same-key")
    assert first.json()["user_id"] != second.json()["user_id"]
    assert first.json()["id"] != second.json()["id"]
    assert await row_counts(session_factory) == (2, 2)


async def test_expired_key_can_be_reused(client, actor, session_factory):
    first = await create(client, actor, key="expired-key")
    async with session_factory() as db:
        async with db.begin():
            await db.execute(
                update(ExpenseIdempotency)
                .where(
                    ExpenseIdempotency.user_id == actor["user_id"],
                    ExpenseIdempotency.key == "expired-key",
                )
                .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
    replacement = await create(client, actor, key="expired-key", amount="40.00")
    assert replacement.json()["id"] != first.json()["id"]
    assert replacement.json()["amount"] == "40.00"
    replay = await create(client, actor, key="expired-key", amount="40.00")
    assert replay.content == replacement.content
    assert await row_counts(session_factory) == (2, 1)


async def test_retry_returns_original_response_after_edit_and_delete(client, actor, session_factory):
    first = await create(client, actor, key="original-response")
    expense_url = f"{URL}/{first.json()['id']}"
    edited = await client.patch(
        expense_url,
        json={"amount": "25.00"},
        headers={**actor["headers"], "If-Match": first.headers["etag"]},
    )
    assert edited.status_code == 200
    assert edited.json()["version"] == 2
    replay = await create(client, actor, key="original-response")
    assert replay.content == first.content
    assert replay.headers["etag"] == first.headers["etag"]
    current = await client.get(expense_url, headers=actor["headers"])
    assert current.json()["amount"] == "25.00"
    deleted = await client.delete(expense_url, headers=actor["headers"])
    assert deleted.status_code == 204 and deleted.content == b""
    replay = await create(client, actor, key="original-response")
    assert replay.content == first.content
    assert await row_counts(session_factory) == (0, 1)
    assert (await client.get(expense_url, headers=actor["headers"])).status_code == 404


async def test_failed_request_does_not_reserve_key(client, actor, session_factory):
    response = await post_expense(client, actor, key="retry-after-error", category_id=2147483647)
    assert response.status_code == 404
    assert await row_counts(session_factory) == (0, 0)
    await create(client, actor, key="retry-after-error")
    assert await row_counts(session_factory) == (1, 1)


async def test_transaction_rolls_back_before_response_is_saved(
    client, actor, session_factory, monkeypatch
):
    def fail_serialization(cls, value, *args, **kwargs):
        raise RuntimeError("simulated serialization failure")

    with monkeypatch.context() as patch:
        patch.setattr(ExpenseRead, "model_validate", classmethod(fail_serialization))
        with pytest.raises(RuntimeError, match="simulated serialization failure"):
            await post_expense(client, actor, key="retry-after-crash")
    assert await row_counts(session_factory) == (0, 0)
    await create(client, actor, key="retry-after-crash")
    assert await row_counts(session_factory) == (1, 1)


@pytest.mark.parametrize("key", ["", "bad key", "bad/key", "x" * 129])
async def test_invalid_idempotency_key(client, actor, key):
    response = await post_expense(client, actor, key=key)
    assert response.status_code == 422


@pytest.mark.parametrize("amount", ["0", "-1.00", "1.234", 1.5])
async def test_invalid_money_is_rejected(client, actor, amount):
    response = await post_expense(client, actor, amount=amount)
    assert response.status_code == 422


@pytest.mark.parametrize("method", ["GET", "PATCH", "DELETE"])
async def test_foreign_expense_returns_404(client, actor, method):
    created = await create(client, actor)
    other = await make_actor(client, "other@example.com")
    kwargs = {"json": {"description": "Unauthorized change"}} if method == "PATCH" else {}
    response = await client.request(
        method, f"{URL}/{created.json()['id']}",
        headers={**other["headers"], "If-Match": created.headers["etag"]}, **kwargs,
    )
    assert response.status_code == 404
    unchanged = await client.get(f"{URL}/{created.json()['id']}", headers=actor["headers"])
    assert unchanged.status_code == 200
    assert unchanged.json()["version"] == 1


@pytest.mark.parametrize("header, status", [(None, 428), ("1", 400), ('"9"', 412)])
async def test_update_preconditions(client, actor, header, status):
    created = await create(client, actor)
    headers = actor["headers"].copy()
    if header is not None:
        headers["If-Match"] = header
    response = await client.patch(
        f"{URL}/{created.json()['id']}", json={"description": "Changed"}, headers=headers
    )
    assert response.status_code == status


async def test_parallel_edits_only_one_succeeds(client, actor):
    created = await create(client, actor)
    url = f"{URL}/{created.json()['id']}"
    headers = {**actor["headers"], "If-Match": created.headers["etag"]}
    responses = await asyncio.gather(
        client.patch(url, json={"description": "First edit"}, headers=headers),
        client.patch(url, json={"description": "Second edit"}, headers=headers),
    )
    assert sorted(response.status_code for response in responses) == [200, 412]
    current = await client.get(url, headers=actor["headers"])
    assert current.json()["version"] == 2
    assert current.headers["etag"] == '"2"'


@pytest.mark.parametrize("payload", [{}, {"amount": None}])
async def test_empty_or_null_update_is_rejected(client, actor, payload):
    created = await create(client, actor)
    response = await client.patch(
        f"{URL}/{created.json()['id']}", json=payload,
        headers={**actor["headers"], "If-Match": created.headers["etag"]},
    )
    assert response.status_code == 422


async def test_expense_listing_requires_authentication(client):
    response = await client.get(URL)
    assert response.status_code == 401