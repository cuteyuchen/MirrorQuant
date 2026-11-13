"""Serialize execution across requests and workers, including exchange reconciliation.

SQLite is a single-host deployment and uses an OS lock released on process death.
PostgreSQL uses a session advisory lock on a dedicated connection. Neither lock
holds an application database transaction open during network calls.
"""
from __future__ import annotations

import asyncio
import contextvars
import fcntl
import hashlib
import os
import tempfile
import weakref
from contextlib import asynccontextmanager
from functools import wraps

from sqlalchemy import text
from app.core.database import get_engine

_locks = weakref.WeakKeyDictionary()
_owner = contextvars.ContextVar("execution_lock_owner", default=None)


@asynccontextmanager
async def execution_lock():
    task = asyncio.current_task()
    if _owner.get() is task:
        yield
        return
    loop = asyncio.get_running_loop()
    lock = _locks.setdefault(loop, asyncio.Lock())
    async with lock:
        engine = get_engine()
        conn = None
        fd = None
        token = None
        try:
            if engine.dialect.name == "postgresql":
                conn = await engine.connect()
                while not await conn.scalar(text("SELECT pg_try_advisory_lock(734908122)")):
                    await conn.commit()
                    await asyncio.sleep(0.05)
                await conn.commit()
            elif engine.dialect.name == "sqlite":
                name = hashlib.sha256(str(engine.url).encode()).hexdigest()[:24]
                path = os.path.join(tempfile.gettempdir(), f"mirrorquant-execution-{name}.lock")
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Execution serialization requires SQLite or PostgreSQL")
            token = _owner.set(task)
            yield
        finally:
            if token is not None:
                _owner.reset(token)
            if fd is not None:
                os.close(fd)
            if conn is not None:
                try:
                    await conn.execute(text("SELECT pg_advisory_unlock(734908122)"))
                    await conn.commit()
                finally:
                    await conn.close()


def serialized_execution(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        async with execution_lock():
            return await fn(*args, **kwargs)
    return wrapped
