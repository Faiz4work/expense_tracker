# Expense Tracker API

Async FastAPI backend for tracking expenses by category. This is a **partial assessment submission**: implemented behavior and unfinished requirements are separated below.

## Verification

Local pytest verification on 2026-10-29: **88 passed, 86% total coverage**, Python 3.11.15 on Windows. The focused expense suite passed 56 tests.

```powershell
pytest
```

**Postman verification:** The complete **20-request collection passed locally on 2026-10-29**, as confirmed by the applicant. The workflow covers all implemented routes and checks authentication, refresh rotation, categories, expense CRUD, filtering, cursor pagination, idempotent retries, stale-version rejection, and logout.

## Implemented

- Async FastAPI, SQLAlchemy 2.0, asyncpg, Pydantic v2, PostgreSQL, Redis, and Alembic migrations.
- Registration with Argon2 password hashing; login and current-user retrieval.
- JWT access tokens lasting 15 minutes and opaque refresh tokens lasting 7 days.
- Refresh rotation; replaying a used refresh token revokes all of that user’s refresh tokens and invalidates their access tokens through a stored token version.
- Logout revokes the supplied refresh token. Existing access tokens remain valid until their 15-minute expiry unless revoked by refresh-reuse protection.
- Redis rolling-window limit of 5 failed logins per normalized email in 15 minutes; subsequent attempts return 429 with Retry-After.
- Category creation and listing of owned category metadata with optional configured budgets.
- Expense create, retrieve, edit, and delete with owner-scoped queries and 404 for foreign resources.
- Decimal input and NUMERIC(18, 2) storage; positive finite amounts and at most two decimal places.
- Expense filters, signed keyset cursors, date/ID ordering, and supporting indexes.
- Version-based ETags; PATCH requires If-Match and performs the version check and update in one SQL statement.
- Per-user Idempotency-Key handling with 24-hour logical expiry, exact creation-response replay, 422 for changed input, and concurrent-request protection.
- A common error envelope and real PostgreSQL/Redis integration tests.

## Unfinished requirements and intended implementation

| Requirement | Current state and next implementation |
| --- | --- |
| Historical exchange rates and monthly reports | Not implemented. Add ExchangeRate(currency, rate_to_usd, valid_on), seed PKR/AED/USD/EUR for at least 90 days, and select the latest source/base rate on or before each expense date. Convert as amount × source_rate_to_usd / base_rate_to_usd. Use one SQL query with rate lookups, aggregation, and LAG for prior-month changes. Include the prior month even when it has no expenses so comparisons remain meaningful. |
| Category monthly spend versus budget | Not implemented. GET /categories currently returns category metadata and configured budgets only. Add current-month spend, remaining budget, and percentage used through SQL aggregation with historical currency conversion. |
| Budget alerts, outbox, worker, GET /notifications | Not implemented. Compute threshold crossings while serializing each category/month aggregate, then write expense changes and threshold outbox events in one transaction. A worker inserts notifications and marks events processed atomically, with a unique user/category/month/level constraint. |
| Budget behavior after edits/deletes | Proposed policy: recompute totals on edits/deletes, retain previously issued alerts, and issue no second alert for the same category/month/80% or 100% level after spending falls and crosses it again. This policy is not implemented yet. |
| Background CSV import and job status | Not implemented. Stream uploads while hashing bytes, deduplicate per user/file hash, and create a background job. Validate rows with Decimal and ownership rules, batch inserts, record numbered row errors, and commit progress with each batch. A unique job/row key protects retries; never load 100,000 rows at once. |
| S3 receipt upload, confirm, and download | Not implemented. Keep storage operations in one module; issue scoped presigned uploads, check object existence/size and real file signatures, and return download links expiring in 15 minutes. receipt_key remains null. |
| Deferred receipt deletion | Not implemented. Write a cleanup outbox entry with expense deletion, then retry object deletion in a worker until successful. |
| Physical cleanup of expired idempotency records | Not implemented. Expiry is already enforced on reads and expired keys are replaced on reuse; unused expired rows remain stored. Add coordinated maintenance with row locking and the expires_at index. |

There is no worker, ExchangeRate table, rate seeding, notifications endpoint, import endpoint, receipt endpoint, Docker configuration, or deployed AWS infrastructure in this submission. Category edit/delete endpoints are outside the specified category endpoint set.

## Local setup

Python 3.11+ and running PostgreSQL and Redis instances are required. The API does not start either service. The steps below describe a fresh checkout; keep existing working .env and .env.test files when updating a current installation.

Create two databases separately in pgAdmin or psql, owned by the configured PostgreSQL user: `expense_tracker` and `expense_tracker_test`. The test fixture explicitly requires the second name and truncates application tables there before and after each test.

From the repository root on Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item .env.test.example .env.test
```

On Linux/macOS, create/activate the environment using `python3.11 -m venv .venv` and `source .venv/bin/activate`, then install the same requirements and copy the example configuration files.

Edit only the local configuration files: replace the sample PostgreSQL passwords, and generate a private JWT_SECRET of at least 32 characters:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the generated value in JWT_SECRET in .env. Do not commit it. URL-encode special characters in database credentials. DATABASE_URL must start with postgresql+asyncpg://. Environment variables override .env; configuration files are resolved relative to the project files.

| Setting | Purpose |
| --- | --- |
| DATABASE_URL | Application PostgreSQL database |
| REDIS_URL | Application Redis connection |
| REDIS_NAMESPACE | Application key prefix; default expense-tracker |
| JWT_SECRET | Shared private JWT/cursor signing secret |
| TEST_DATABASE_URL | Separate expense_tracker_test PostgreSQL database |
| TEST_REDIS_URL | Redis connection used by tests, example database 15 |

Tests generate their own JWT secret and unique Redis namespace. They remove only Redis keys in that namespace. Both Redis client constructors use protocol=2 for compatibility with the local Redis 5 instance used during development.

Apply committed migrations, then start the development server:

```powershell
python -m alembic upgrade head
python -m alembic current
python -m uvicorn main:app --reload
```

If the development server is already running, use that process. Swagger: http://127.0.0.1:8000/docs. Current migration head at submission preparation: `4b28b8636507`.

## API endpoints

Application routes below are under `/api/v1`. GET / and GET /health are public service endpoints outside that prefix. The health route currently checks liveness only, not database/Redis readiness.

| Method | Path | Success | Authentication |
| --- | --- | --- | --- |
| POST | /auth/register | 201 | Public |
| POST | /auth/login | 200 | Public |
| POST | /auth/refresh | 200 | Refresh token in JSON body |
| POST | /auth/logout | 204 | Refresh token in JSON body |
| GET | /auth/me | 200 | Bearer access token |
| POST | /categories | 201 | Bearer access token |
| GET | /categories | 200 | Bearer access token |
| POST | /expenses | 201 | Bearer access token |
| GET | /expenses | 200 | Bearer access token |
| GET | /expenses/{id} | 200 | Bearer access token |
| PATCH | /expenses/{id} | 200 | Bearer token and If-Match |
| DELETE | /expenses/{id} | 204 | Bearer access token |

Authorize Swagger with the login response’s access_token only. The refresh token goes in the refresh/logout JSON body. Signing in does not automatically replace the token stored in Swagger’s Authorize dialog.

Expense creation example (use an owned category ID):

```json
{
  "category_id": 1,
  "amount": "1200.50",
  "currency": "PKR",
  "description": "Groceries",
  "spent_on": "2026-10-29"
}
```

Send fractional amounts as JSON strings. Floats and booleans are rejected. Whole integers are exact and accepted. Supported currencies are PKR, AED, USD, and EUR; input currencies are normalized. Category names are trimmed and unique per user, with case-sensitive matching. A missing/null category budget means no budget; a provided budget must be positive.

### Listing and cursors

GET /expenses accepts category_id, date_from, date_to, min_amount, max_amount, currency, q, limit, and cursor. Ranges are inclusive, q is a case-insensitive literal substring, and all supplied filters are combined. Amount filters compare stored amounts without conversion. Default limit is 50; maximum 100.

Results sort by spent_on DESC, id DESC. Return shape: {"items": [...], "next_cursor": "..."}; the last cursor is null. Keep filters unchanged while following a cursor. Cursors are signed, user/filter-bound, and expire in 24 hours. They are not authorization tokens.

The keyset condition is (spent_on, id) < (last_spent_on, last_id), fetching limit + 1. New rows ahead of the anchor do not shift older pages. Backdated inserts below the anchor may appear later. This is a live listing, not a snapshot across requests; edits that change sort dates or deletions can change later results.

Indexes `(user_id, spent_on DESC, id DESC)` and `(user_id, category_id, spent_on DESC, id DESC)` match the owner/category predicates and ordering. No specialized description-search index has been added.

### Safe retries

Send a unique Idempotency-Key header for each logical expense creation. Keys accept 1–128 ASCII letters, digits, dots, underscores, colons, and hyphens. The header is optional; omitting it performs a normal creation without deduplication.

The same user/key with equivalent validated input replays the original 201 JSON bytes and ETag for 24 hours. A changed input returns 422. Canonical hashing normalizes field order, currency, defaults, and amount scale, so 10.0 and 10.00 are equivalent.

A PostgreSQL transaction advisory lock serializes each user/key across API instances. A composite primary key enforces one saved key record. Expense insertion and response storage commit atomically. Errors roll back both writes; retries after a committed response was lost replay it. Replays remain the original creation response after later edits/deletion, so GET the current expense before editing.

### Edit conflicts and errors

GET-by-ID and successful create/update responses return an ETag such as `"1"`. PATCH requires `If-Match: "1"`, including the double quotes. The conditional UPDATE matches ID, owner, and version, increments version, and returns the updated object in one statement.

Errors share `{"error":{"code":"...","message":"...","details":[]}}`. Important statuses: 400 malformed cursor/ETag, 401 invalid token, 404 missing/foreign resource, 409 duplicate category/email, 412 stale version, 422 invalid input or changed idempotency payload, 428 missing If-Match, 429 login limit, and 503 unavailable login limiter.

## Tests and Postman

Run `pytest` from the repository root. Tests use httpx AsyncClient/ASGITransport, a real PostgreSQL test database, and Redis. They exercise refresh reuse, rate limits, ownership, money validation, filters, insertion-safe keyset paging, concurrent same-key POSTs, parallel PATCHes, key expiry, and transaction rollback. Remaining unimplemented features have no implementation tests.

Import `postman/ExpenseTracker.postman_collection.json`, select **No environment**, confirm collection variable et_base_url is http://127.0.0.1:8000, and run the full collection once in its listed order. The collection creates a fresh synthetic account and category, manages tokens/IDs/ETags/cursors, checks retries and stale updates, and logs out. It creates demo data in the configured application database; pytest uses the separate test database.

Use the clean JSON committed in this repository. Runtime credentials/tokens are generated locally and cleared on the collection’s final logout; do not commit a populated export if a run stops early. Inspect runner failures before declaring the Postman check passed.

## AWS deployment proposal

This section is a design proposal. No AWS resources have been deployed, and the future worker/storage features above remain unimplemented.

### API, worker, and releases

Package the API in an image and run two ECS Fargate API tasks across availability zones behind an HTTPS ALB. Scale the API on request/CPU metrics; ECS replaces failed tasks. Add a separate worker service when outbox/import processing exists, with bounded concurrency and scaling based on backlog age/depth.

Use rolling deployments with minimumHealthyPercent=100 and maximumPercent=200, sufficient capacity, health checks, and connection draining. Run a single migration task before deployment. Add production readiness checks; the current /health is liveness only. See [ECS deployment settings](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service_definition_parameters.html).

### Database and secrets

Use RDS PostgreSQL in private subnets with backups and Multi-AZ availability. Allow inbound TCP 5432 only from API/worker/migration security groups. Require database TLS and limit connection pools per task. Use a dedicated DDL role for migrations and a restricted application role for ordinary requests. See [RDS security groups](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Overview.RDSSecurityGroups.html).

Store the database URL and signing secret in Secrets Manager and inject them into ECS task environment variables. Grant retrieval to the task execution role; use a separate task role for application AWS access. Redeploy tasks to pick up changed injected values and coordinate signing-key rotation. See [ECS secret injection](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/specifying-sensitive-data-tutorial.html).

### Redis

Use private ElastiCache with TLS and access restricted to the API/worker security groups. All API instances share the rate-limit namespace and signing configuration. Current login fails closed with 503 when Redis cannot enforce its limit. Existing-token database-backed routes and PostgreSQL idempotency records do not depend on Redis availability.

### Private receipt storage and IAM

For the future receipt feature, use a private S3 bucket with Block Public Access, default SSE-S3 encryption, HTTPS, and keys receipts/{user_id}/{expense_id}/{uuid}.{ext}. Authenticate ownership before issuing any URL. Validate uploaded size and bytes before saving receipt_key.

Example application task-role policy for a non-versioned receipt bucket; replace the bucket placeholder. The proposed operations need only PutObject, GetObject (including HEAD/ranged reads), and DeleteObject on the receipt prefix. No bucket listing is required. With a versioned bucket, DeleteObject adds a delete marker; permanent version cleanup would need separate retention design/permissions.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReceiptObjectsOnly",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::REPLACE_WITH_PRIVATE_BUCKET/receipts/*",
      "Condition": {"Bool": {"aws:SecureTransport": "true"}}
    }
  ]
}
```

Use the ECS task role rather than access keys in configuration. Missing-object HEAD requests can return 403 without ListBucket; treat inaccessible/unverified objects as confirmation failures. See [S3 object permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security_iam_service-with-iam.html) and [HeadObject permissions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html).

### Migration compatibility

Use expand/contract releases: first add compatible tables/nullable columns/indexes, deploy code that works with old and new structures, backfill in bounded batches, then remove old structures only after old tasks are gone. Run Alembic once per release through a controlled task, not on every API startup. Use lock/statement timeouts and plan concurrent indexes for large production tables. Existing local migrations are ordinary schema migrations, not a claim of a tested zero-downtime release.

## Short answers

### 1. Why presigned uploads, what can go wrong, and how would I guard them?

Direct uploads keep file traffic and buffering off API workers.  
The API would authorize the expense and issue a short-lived URL/policy for one random owned key.  
A presigned POST policy can enforce a 5 MB content-length range and an allowed declared type.  
Declared type is insufficient, so confirmation would check object size and real JPG/PNG/PDF signatures.  
URLs are bearer capabilities: use HTTPS, avoid logging them, and keep the bucket private.  
Download links would expire in 15 minutes or earlier if the role credentials expire.  
Receipt storage and these checks are proposed work, not existing routes. See [POST policies](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sigv4-HTTPPOSTConstructPolicy.html) and [presigned expiry](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html).

### 2. How would I investigate a slow GET /expenses at 20 million rows?

I would capture the actual query, parameters, latency, and database wait information first.  
I would run EXPLAIN (ANALYZE, BUFFERS) on representative data and check estimates, scans, sorts, and reads.  
The current indexes begin with user_id and then the date/ID ordering, with a category-specific alternative.  
I would compare plans for different filters and keep planner statistics current before adding indexes.  
Keyset pagination avoids repeatedly scanning/skipping an increasing number of offset rows.  
The listing selects expense fields without lazy relationship lookups, avoiding per-row N+1 queries.  
For expensive description search, I would evaluate a trigram/GIN index or search design against measured plans.

### 3. What changes when the API runs on three servers?

All servers must share the same signing secret and database; current tokens are not process-local sessions.  
Redis Lua operations and PostgreSQL per-email transaction locks coordinate the current login limit.  
PostgreSQL user/key locks and a unique key record coordinate current idempotency across servers.  
The current conditional version UPDATE protects edits across servers without an in-memory lock.  
Future workers would claim bounded outbox batches and use unique notification constraints for replay safety.  
Future uploads would use shared S3 storage rather than each server’s filesystem.  
A single release migration task would run compatible migrations before rolling out the API tasks.

### 4. How would I drain 500,000 events after two hours of worker downtime?

The planned outbox would keep committed events durable while the worker is unavailable.  
I would claim small batches with FOR UPDATE SKIP LOCKED and cap worker/database concurrency.  
For database notifications, insertion and marking the event processed would share a short transaction.  
A unique category/month/level key would make crash retries and concurrent workers harmless.  
I would use backoff with jitter, an attempt limit, and a dead-letter path for persistent failures.  
I would monitor oldest-event age, throughput, errors, connection use, and database load.  
I would increase worker capacity gradually, protecting API capacity instead of draining everything at once.

### 5. Where did I choose simplicity, and why?

I kept current idempotency records and locks in PostgreSQL alongside the expense transaction.  
This lets expense creation and the original response commit together without cache synchronization.  
A lost HTTP response can be replayed, while a failed transaction leaves neither write committed.  
Redis is still used for the required login limiter.  
The tradeoff is an extra database write and lock for keyed requests plus retained expired records.  
I implemented logical expiry first and documented physical cleanup as follow-up operational work.  
Concurrent retry, changed-body, expiry, and rollback tests cover the chosen approach.

## AI assistance

OpenAI Codex assisted with assessment interpretation, architecture explanations, setup review, and implementation of the initial settings/Base/Alembic setup. It supplied code guides and pytest cases for authentication, category/expense CRUD, filtering/cursors, idempotency, and concurrency checks. It also drafted this README, environment examples, and the Postman collection. The applicant copied/applied the implementation guides and ran the local migrations, manual checks, and pytest commands reported above. Reported pytest results come from the applicant’s local runs. The applicant also successfully ran the complete 20-request Postman workflow locally.
