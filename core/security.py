from pwdlib import PasswordHash
from starlette.concurrency import run_in_threadpool


password_hasher = PasswordHash.recommended()


async def hash_password(password: str) -> str:
    return await run_in_threadpool(
        password_hasher.hash,
        password,
    )


