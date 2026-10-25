"""The memory index: each chat session's turns and the user memory files it wrote, into the
ast-everything graph (source `memory-<user>`, `ast_everything.memory`), on the host.

A session is indexed when its Worker closes (`closed`, the Workers' `on_closed`), when its
event log has not changed for `IDLE_S` (checked every `CHECK_EVERY_S`), and by the same
check with no idle time at startup. A session whose log is older than its last index is
not read. A failed run is logged and the session stays unindexed until the next check;
the chat never waits for it. The guest tenant is not indexed. On only with `MEMORY_INDEX`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ast_everything.indexer import source_name
from ast_everything.memory import index_session
from ast_everything.store import Store

from app.config import USERS_ROOT
from app.session.eventlog import memory_changes, session_turns
from app.tenants.registry import GUEST_TENANT

logger = logging.getLogger(__name__)

IDLE_S = 30 * 60
CHECK_EVERY_S = 5 * 60
MAX_PER_CHECK = 20   # sessions one check indexes; a backlog (the first start) drains over the next checks


def memory_source(user: str) -> str:
    return source_name(f"memory-{user}")


def _newest_write(session_root: Path) -> float:
    """The mtime of the newest segment of the session's runs (0 with none); the host log is not one."""
    return max((path.stat().st_mtime for path in session_root.glob("events/r*/*.jsonl")), default=0.0)


class MemoryIndexer:
    def __init__(self, sessions, tenants, connect=Store.connect):
        self.sessions = sessions          # the DirStore
        self.tenants = [tenant for tenant in tenants if tenant.name != GUEST_TENANT]
        self.connect = connect
        self.store: Store | None = None
        self._running: set[str] = set()   # the sessions being indexed: a second trigger skips
        self._tasks: set[asyncio.Task] = set()

    def _store(self) -> Store:
        if self.store is None:
            self.store = self.connect()
        return self.store

    def closed(self, key: str) -> asyncio.Task | None:
        """Index the session of a Worker that closed (`user/tenant/session_id`), in the background."""
        user, tenant, session_id = key.split("/")
        if tenant == GUEST_TENANT:
            return None
        task = asyncio.get_running_loop().create_task(self.index(user, tenant, session_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def index(self, user: str, tenant: str, session_id: str) -> None:
        key = f"{user}/{tenant}/{session_id}"
        if key in self._running:
            return
        self._running.add(key)
        try:
            await asyncio.to_thread(self._index, user, tenant, session_id)
        except Exception as exc:  # noqa: BLE001 - the next check tries again
            logger.warning("memory index of session %s failed: %s: %s", session_id, type(exc).__name__, exc)
        finally:
            self._running.discard(key)

    def _index(self, user: str, tenant: str, session_id: str) -> None:
        """Index the session unless its log is no newer than its last index (a trigger that
        fired with another, which got there first). The record keeps `log_mtime`, the newest
        log write read before the turns, so a turn written during the run is indexed next."""
        session = self.sessions.get(user, tenant, session_id)
        if session is None:
            return
        source, path = memory_source(user), f"sessions/{tenant}/{session_id}"
        record = self._store().sessions.find_one({"_id": f"{source}/{path}"}) or {}
        newest = _newest_write(session.root)
        if newest <= record.get("log_mtime", 0):
            return
        counts = index_session(self._store(), source, path,
                               session.read_meta().get("title") or session_id, session_turns(session.root),
                               memory_root=Path(USERS_ROOT) / user / "memory", memory_files=memory_changes(session.root))
        if counts is not None:
            self._store().sessions.update_one({"_id": f"{source}/{path}"}, {"$set": {"log_mtime": newest}})

    async def check(self, idle_s: float) -> None:
        """Index each session of the tenants whose log is idle for `idle_s` and newer than its last
        index, at most `MAX_PER_CHECK`."""
        due = [(tenant, session_id) for tenant in self.tenants
               for session_id in await asyncio.to_thread(self._due, tenant.user, tenant.name, idle_s)]
        if len(due) > MAX_PER_CHECK:
            logger.warning("memory index: %d sessions due, %d indexed now, the rest at the next checks",
                           len(due), MAX_PER_CHECK)
        for tenant, session_id in due[:MAX_PER_CHECK]:
            await self.index(tenant.user, tenant.name, session_id)

    def _due(self, user: str, tenant: str, idle_s: float) -> list[str]:
        source = memory_source(user)
        indexed = {doc["_id"]: doc.get("log_mtime", 0) for doc in self._store().sessions.find({"source": source})}
        idle_since = time.time() - idle_s
        due = []
        for meta in self.sessions.list_for_tenant(user, tenant):
            newest = _newest_write(meta["root"])
            if 0 < newest <= idle_since and newest > indexed.get(f"{source}/sessions/{tenant}/{meta['session_id']}", 0):
                due.append(meta["session_id"])
        return due

    async def run(self) -> None:
        """The startup catch-up, then the idle check every `CHECK_EVERY_S`."""
        idle_s = 0
        while True:
            try:
                await self.check(idle_s)
            except Exception as exc:  # noqa: BLE001 - the loop never dies
                logger.warning("memory index check failed: %s: %s", type(exc).__name__, exc)
            idle_s = IDLE_S
            await asyncio.sleep(CHECK_EVERY_S)
