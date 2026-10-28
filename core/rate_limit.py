import hashlib
import secrets

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError


# All timestamps come from Redis, so API servers do not need matching clocks here.
LOGIN_SCRIPT = """
local window = 900000
local current = redis.call('TIME')
local now = tonumber(current[1]) * 1000 + math.floor(tonumber(current[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)

if ARGV[1] == 'fail' then
    redis.call('ZADD', KEYS[1], now, ARGV[2])
    redis.call('PEXPIRE', KEYS[1], window)
end

if redis.call('ZCARD', KEYS[1]) < 5 then
    return 0
end

local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return math.max(1, math.ceil((tonumber(oldest[2]) + window - now) / 1000))
"""


class LoginRateLimiter:
    def __init__(self, redis: Redis, namespace: str):
        self.redis = redis
        self.namespace = namespace

    def key(self, email: str) -> str:
        digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
        return f"{self.namespace}:login:{digest}"

    async def _call(self, email: str, action: str) -> int:
        try:
            result = await self.redis.eval(
                LOGIN_SCRIPT, 1, self.key(email), action, secrets.token_hex(16)
            )
            return int(result)
        except RedisError:
            # Do not silently disable the login limit during an outage.
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "auth_unavailable",
                    "message": "Login is temporarily unavailable.",
                },
            ) from None

    async def check(self, email: str) -> None:
        retry_after = await self._call(email, "check")
        if retry_after:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "too_many_logins",
                    "message": "Too many failed logins. Try again later.",
                },
                headers={"Retry-After": str(retry_after)},
            )

    async def record_failure(self, email: str) -> None:
        await self._call(email, "fail")