"""The memory index: each chat session's turns and the memory files it wrote, into the
ast-everything graph (source `memory-<user>`, `ast_everything.memory`), on the host, and the
recall from it for each input.

A session is indexed when its Worker closes (`closed`, the Workers' `on_closed`), when its
event log has not changed for `IDLE_S` (checked every `CHECK_EVERY_S`), and by the same
check with no idle time at startup. A session whose log is older than its last index is
not read. A failed run is logged and the session stays unindexed until the next check,
which takes it after the sessions that did not fail; the chat never waits for it. The
guest tenant is not indexed. On only with `MEMORY_INDEX`.

Memory files are indexed from two folders: the user's (`<slug>.md` in the source) and each
session's own, its `remember(scope=session)` files and compaction summaries (at
`sessions/<tenant>/<id>/memory/<slug>.md`, beside the session's turns). A file is indexed
when `remember` or `forget` changes it (`memory_written`, in the background), with its
session's index, and by each check, which makes the source hold exactly the files of each
user's folder (a file written by hand, or before the index was on). All of a user's indexing
runs under one lock in this process: the triggers can meet on the same file.

`recall` brings back, for an input, the nodes of the user's graph nearest it by meaning
(`MEMORY_RECALL_LIMIT`, scoring `MEMORY_RECALL_MIN_SCORE` or more), other than the current
session's and the memory files the description recall already brought; within
`RECALL_TIMEOUT_S`, else nothing.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from pathlib import Path

from ast_everything.memory import MemoryFiles, index_memory, index_session, memory_source, sync_memory
from ast_everything.search import recall as recall_nodes
from ast_everything.store import Store

from app.config import MEMORY_RECALL_LIMIT, MEMORY_RECALL_MIN_SCORE, USERS_ROOT
from app.session.eventlog import memory_changes, session_turns
from app.session.ids import session_key
from app.tenants.registry import GUEST_TENANT
from app.user.memory import VALID_SLUG

logger = logging.getLogger(__name__)

IDLE_S = 30 * 60
CHECK_EVERY_S = 5 * 60
MAX_PER_CHECK = 20   # sessions one check indexes; a backlog (the first start) drains over the next checks
RECALL_TIMEOUT_S = 2.0
RECALL_BODY_CHARS = 1500

_active: MemoryIndexer | None = None   # the running indexer (agent-api startup), else None


def activate(indexer: MemoryIndexer | None) -> None:
    """Make `indexer` the one `memory_written` and `recall` use (None: neither does anything)."""
    global _active
    _active = indexer


def memory_written(user: str, tenant: str, session_id: str, session_memory: Path | None, scope: str,
                   slug: str) -> asyncio.Task | None:
    """`remember` or `forget` changed a memory file: index it in the background."""
    if _active is None or tenant == GUEST_TENANT or not VALID_SLUG.fullmatch(slug):
        return None
    if scope == "session":
        if session_memory is None:
            return None
        files = _session_files(tenant, session_id, session_memory, [slug])
    else:
        files = MemoryFiles(_user_memory(user), [slug])
    return _active.file_changed(user, files)


def recall(user: str, tenant: str, session_id: str, text: str, skip_slugs=()) -> list[dict]:
    """The input's attachments from the memory graph (blocking: called off the loop)."""
    if _active is None or tenant == GUEST_TENANT or MEMORY_RECALL_LIMIT <= 0 or not text.strip():
        return []
    return _active.recall(user, tenant, session_id, text, skip_slugs)


def _session_path(tenant: str, session_id: str) -> str:
    """The session's path in its memory source; `<source>/<path>` is its record's `_id`."""
    return f"sessions/{tenant}/{session_id}"


def _user_memory(user: str) -> Path:
    return Path(USERS_ROOT) / user / "memory"


def _session_files(tenant: str, session_id: str, root: Path, slugs) -> MemoryFiles:
    return MemoryFiles(Path(root), slugs, f"{_session_path(tenant, session_id)}/memory/")


def _newest_write(session_root: Path) -> float:
    """The mtime of the newest segment of the session's runs (0 with none); the host log is not one."""
    return max((path.stat().st_mtime for path in session_root.glob("events/r*/*.jsonl")), default=0.0)


def _folder_slugs(root: Path) -> list[str] | None:
    """The memory files in `root` by slug (not MEMORY.md, not a symlink); None without the folder."""
    if not root.is_dir():
        return None
    return sorted(path.stem for path in root.glob("*.md")
                  if VALID_SLUG.fullmatch(path.stem) and path.is_file() and not path.is_symlink())


def _attachment(doc: dict) -> dict:
    """One recalled node as an input attachment (the guest renders `title` and `body`)."""
    if doc["type"] == "turn":
        at = doc.get("at")
        title = f"earlier conversation: {doc['title']} ({doc.get('said_by', '?')}"
        title += f", {at:%Y-%m-%d})" if at else ")"
    else:
        title = f"memory: {doc['path']}" + (f" > {' > '.join(doc['heading_path'])}" if doc["heading_path"] else "")
    return {"kind": "memory", "source": "index", "title": title, "id": doc["nid"],
            "score": round(float(doc["score"]), 3), "body": doc["text"][:RECALL_BODY_CHARS].strip()}


class MemoryIndexer:
    def __init__(self, sessions, tenants, connect=Store.connect):
        self.sessions = sessions          # the DirStore
        self.tenants = [tenant for tenant in tenants if tenant.name != GUEST_TENANT]
        self.connect = connect
        self.store: Store | None = None
        self._running: set[str] = set()   # the sessions being indexed: a second trigger skips
        self._tasks: set[asyncio.Task] = set()
        self._failed: dict[str, float] = {}   # session key: when its last run failed
        self._guard = threading.Lock()
        self._user_locks: dict[str, threading.Lock] = {}
        self._recalls = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-recall")

    def _store(self) -> Store:
        with self._guard:
            if self.store is None:
                store = self.connect()
                store.prepare()
                self.store = store
            return self.store

    def _lock(self, user: str) -> threading.Lock:
        """The lock all of `user`'s indexing runs under."""
        with self._guard:
            return self._user_locks.setdefault(user, threading.Lock())

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def closed(self, key: str) -> asyncio.Task | None:
        """Index the session of a Worker that closed (`user/tenant/session_id`), in the background."""
        user, tenant, session_id = key.split("/")
        if tenant == GUEST_TENANT:
            return None
        return self._spawn(self.index(user, tenant, session_id))

    def file_changed(self, user: str, files: MemoryFiles) -> asyncio.Task:
        """Index one folder's files that just changed, in the background; a failure is logged (the
        session's index or the next check indexes it again)."""
        async def run() -> None:
            try:
                await asyncio.to_thread(self._index_files, user, files)
            except Exception as exc:  # noqa: BLE001 - the next check tries again
                logger.warning("memory index of %s%s failed: %s: %s", files.prefix, list(files.slugs),
                               type(exc).__name__, exc)
        return self._spawn(run())

    def _index_files(self, user: str, files: MemoryFiles) -> None:
        with self._lock(user):
            index_memory(self._store(), memory_source(user), files)

    async def index(self, user: str, tenant: str, session_id: str) -> None:
        key = session_key(user, tenant, session_id)
        if key in self._running:
            return
        self._running.add(key)
        try:
            await asyncio.to_thread(self._index, user, tenant, session_id)
            self._failed.pop(key, None)
        except Exception as exc:  # noqa: BLE001 - the next check tries again
            self._failed[key] = time.time()
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
        source, path = memory_source(user), _session_path(tenant, session_id)
        with self._lock(user):
            record = self._store().sessions.find_one({"_id": f"{source}/{path}"}) or {}
            newest = _newest_write(session.root)
            if newest <= record.get("log_mtime", 0):
                return
            changes = memory_changes(session.root)
            memory = [MemoryFiles(_user_memory(user), changes["user"]),
                      _session_files(tenant, session_id, session.memory, changes["session"])]
            counts = index_session(self._store(), source, path, session.read_meta().get("title") or session_id,
                                   session_turns(session.root), memory=memory)
            if counts is not None:
                self._store().sessions.update_one({"_id": f"{source}/{path}"}, {"$set": {"log_mtime": newest}})

    async def check(self, idle_s: float) -> None:
        """Bring each user's memory folder into the index, then index each session of the tenants
        whose log is idle for `idle_s` and newer than its last index, at most `MAX_PER_CHECK`: the
        sessions whose last run failed come last, the oldest failure first, so they cannot hold
        every slot."""
        for user in sorted({tenant.user for tenant in self.tenants}):
            try:
                await asyncio.to_thread(self._sync_user, user)
            except Exception as exc:  # noqa: BLE001 - the next check tries again
                logger.warning("memory folder of %s not indexed: %s: %s", user, type(exc).__name__, exc)
        due = [(tenant, session_id) for tenant in self.tenants
               for session_id in await asyncio.to_thread(self._due, tenant.user, tenant.name, idle_s)]
        due.sort(key=lambda item: self._failed.get(session_key(item[0].user, item[0].name, item[1]), 0.0))
        if len(due) > MAX_PER_CHECK:
            logger.warning("memory index: %d sessions due, %d indexed now, the rest at the next checks",
                           len(due), MAX_PER_CHECK)
        for tenant, session_id in due[:MAX_PER_CHECK]:
            await self.index(tenant.user, tenant.name, session_id)

    def _sync_user(self, user: str) -> None:
        """The source's user memory files become exactly the folder's. A missing folder (not
        mounted, say) changes nothing: it is no proof the files were removed."""
        root = _user_memory(user)
        slugs = _folder_slugs(root)
        if slugs is None:
            return
        with self._lock(user):
            sync_memory(self._store(), memory_source(user), MemoryFiles(root, slugs))

    def _due(self, user: str, tenant: str, idle_s: float) -> list[str]:
        source = memory_source(user)
        indexed = {doc["_id"]: doc.get("log_mtime", 0) for doc in self._store().sessions.find({"source": source})}
        idle_since = time.time() - idle_s
        due = []
        for meta in self.sessions.list_for_tenant(user, tenant):
            newest = _newest_write(meta["root"])
            if 0 < newest <= idle_since and newest > indexed.get(f"{source}/{_session_path(tenant, meta['session_id'])}", 0):
                due.append(meta["session_id"])
        return due

    def recall(self, user: str, tenant: str, session_id: str, text: str, skip_slugs) -> list[dict]:
        """The attachments of the graph's nodes nearest `text`: not the current session's (its
        turns are in context), not a memory file in `skip_slugs`. Nothing past RECALL_TIMEOUT_S or
        on an error: recall never holds up or fails an input."""
        exclude = [_session_path(tenant, session_id), *(f"{slug}.md" for slug in skip_slugs)]
        future = None
        try:
            future = self._recalls.submit(lambda: recall_nodes(
                self._store(), text, scope=memory_source(user), limit=MEMORY_RECALL_LIMIT,
                min_score=MEMORY_RECALL_MIN_SCORE, exclude_paths=exclude))
            return [_attachment(doc) for doc in future.result(timeout=RECALL_TIMEOUT_S)]
        except concurrent.futures.TimeoutError:
            future.cancel()   # still queued behind a slow store: never run it for nobody
            logger.warning("memory recall took over %.1fs; the input goes without it", RECALL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - recall is optional (closed, store down)
            logger.warning("memory recall failed: %s: %s", type(exc).__name__, exc)
        return []

    def close(self) -> None:
        """Stop the recall threads (agent-api shutdown); a recall still running is not waited for."""
        self._recalls.shutdown(wait=False, cancel_futures=True)

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
