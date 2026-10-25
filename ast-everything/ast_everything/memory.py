"""Add-only indexing of a chat session's turns into the store.

`index_session` writes a `session` node, one `turn` node per turn the
session's record has not indexed yet, one chunk per turn (split at
paragraphs past `CHUNK_WORDS`, each part headed by the session title),
`contains` edges from the session to its turns and `precedes` edges from
each turn to the next. The session's record in `sessions` holds the cursor
`indexed_through_turn` and a lease `lock_until`: a run takes the lease with
one conditional update and skips the session when another run holds it.

Every write is an upsert on a fixed ID (`<source>/<session>` for the session,
`<session ID>:turn-<n>` for a turn) or on an edge's type and ends, so a run
that stops part-way leaves the cursor where it was, and the next run writes
the same turns again without inserting anything twice; a write that raises
releases the lease at once. The memory files the session wrote or removed are
indexed when on disk, or marked removed when gone, by the indexer's own
versioning, and only those. The cursor moves and the lease is released last.

Memory files live in folders (`MemoryFiles`: a folder of `<slug>.md` files and the
path prefix they get in the source, so a user's folder and each session's share
one source). `index_memory` indexes some of a folder's files, as one write or a
session's index does; `sync_memory` makes the source hold exactly the folder's
files, for files no session's events name (written by hand, or before indexing).
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from pymongo import ReturnDocument, UpdateOne

from ast_everything.indexer import index_files, remove_files, source_name, valid_source
from ast_everything.store import Store, chunk, edge, node, now

MEMORY_PREFIX = "memory-"   # reserved for `memory_source`: an index tool's source never has it
LEASE_S = 10 * 60
CHUNK_WORDS = 500


def memory_source(user: str) -> str:
    """The source of a user's sessions and memory files."""
    return source_name(f"{MEMORY_PREFIX}{user}")


class Turn(NamedTuple):
    n: int          # 1 for the session's first turn
    said_by: str    # "user" or "assistant"
    at: datetime
    text: str


class MemoryFiles(NamedTuple):
    """The `<slug>.md` files of `slugs` in the folder `root`, at `prefix<slug>.md` in the source."""
    root: Path
    slugs: Iterable[str]
    prefix: str = ""


def index_session(store: Store, source: str, session: str, title: str, turns: list[Turn],
                  lease_s: float = LEASE_S, memory: Iterable[MemoryFiles] = ()) -> Counter | None:
    """Index the turns of `session` (its path in `source`, as `sessions/<tenant>/<id>`) that come
    after its cursor, and the memory files the session wrote or removed (`memory`, one entry per
    folder). None when another run holds the session's lease."""
    sid = f"{valid_source(source)}/{session}"
    record = _take_lease(store, source, sid, lease_s)
    if record is None:
        return None
    cursor = record["indexed_through_turn"]
    new = [turn for turn in turns if turn.n > cursor]
    session_node = node(sid, type="session", source=source, path=session, title=title, heading_path=[],
                        names=[Path(session).name.lower()], start_line=None, end_line=None, text=title)
    turn_nodes = [_turn_node(sid, source, session, title, turn) for turn in new]
    chunks = [chunk(doc, f"{title}\n{part}", n) for doc in turn_nodes for n, part in enumerate(_parts(doc["text"]), 1)]
    try:
        counts = Counter(turns=len(new), nodes_inserted=_upsert(store.nodes, [session_node, *turn_nodes], ("_id",)),
                         chunks_inserted=_upsert(store.chunks, chunks, ("_id",)),
                         edges_inserted=_upsert(store.edges, _edges(sid, source, session, cursor, turn_nodes),
                                                ("type", "src", "dst")))
        for files in memory:
            counts.update(index_memory(store, source, files))
    except Exception:
        store.sessions.update_one({"_id": sid}, {"$set": {"status": "idle", "lock_until": None}})
        raise
    _commit(store, sid, max([cursor, *(turn.n for turn in new)]))
    return counts


def _take_lease(store: Store, source: str, sid: str, lease_s: float) -> dict | None:
    """The session's record, with the lease taken; None when the lease is held."""
    store.sessions.update_one({"_id": sid}, {"$setOnInsert": {
        "source": source, "indexed_through_turn": 0, "last_turn": 0, "indexed_at": None,
        "status": "idle", "lock_until": None}}, upsert=True)
    start = now()
    return store.sessions.find_one_and_update(
        {"_id": sid, "lock_until": {"$not": {"$gt": start}}},
        {"$set": {"status": "indexing", "lock_until": start + timedelta(seconds=lease_s)}},
        return_document=ReturnDocument.AFTER)


def _turn_node(sid: str, source: str, session: str, title: str, turn: Turn) -> dict:
    doc = node(f"{sid}:turn-{turn.n}", type="turn", source=source, path=session, title=title, heading_path=[],
               names=[], start_line=None, end_line=None, text=turn.text)
    return {**doc, "said_by": turn.said_by, "at": turn.at}


def _edges(sid: str, source: str, session: str, cursor: int, turn_nodes: list[dict]) -> list[dict]:
    """`contains` from the session to each new turn, and `precedes` along the turns, from the
    last turn indexed before."""
    turn_ids = [doc["nid"] for doc in turn_nodes]
    edges = [edge("contains", sid, dst, source=source, src_path=session) for dst in turn_ids]
    if turn_ids and cursor:
        turn_ids.insert(0, f"{sid}:turn-{cursor}")
    return edges + [edge("precedes", src, dst, source=source, src_path=session)
                    for src, dst in zip(turn_ids, turn_ids[1:])]


def _parts(text: str) -> list[str]:
    """`text` in parts of about `CHUNK_WORDS` words, split at blank lines; a longer paragraph stays whole."""
    parts: list[list[str]] = []
    words = 0
    for paragraph in text.split("\n\n"):
        size = len(paragraph.split())
        if not parts or words + size > CHUNK_WORDS:
            parts.append([])
            words = 0
        parts[-1].append(paragraph)
        words += size
    return ["\n\n".join(part) for part in parts if "".join(part).strip()]


def _upsert(collection, docs: list[dict], keys: tuple[str, ...]) -> int:
    """Insert each of `docs` that no document matches on `keys`; the number inserted."""
    if not docs:
        return 0
    ops = [UpdateOne({key: doc[key] for key in keys},
                     {"$setOnInsert": {k: v for k, v in doc.items() if k != "_id"}}, upsert=True)
           for doc in docs]
    return collection.bulk_write(ops, ordered=False).upserted_count


def index_memory(store: Store, source: str, files: MemoryFiles) -> Counter:
    """Index the files that exist, and mark the others removed: the disk decides, since a later
    session may have written again a file this one removed. A symlink is not read: a memory
    folder holds the files its owner wrote."""
    paths = [files.root / f"{slug}.md" for slug in files.slugs]
    present = [path for path in paths if path.is_file() and not path.is_symlink()]
    counts = index_files(store, source, files.root, present, prefix=files.prefix)
    counts["nodes_removed"] += remove_files(store, source,
                                            [files.prefix + path.name for path in paths if path not in present])
    return counts


def sync_memory(store: Store, source: str, files: MemoryFiles) -> Counter:
    """Make the source's files at `prefix<name>.md` (no folder between) exactly `files.slugs`: index
    those, and mark removed the indexed ones that are not among them."""
    slugs = list(files.slugs)
    counts = index_memory(store, source, files._replace(slugs=slugs))
    wanted = {f"{files.prefix}{slug}.md" for slug in slugs}
    pattern = "^" + re.escape(files.prefix) + r"[^/]+\.md$"
    indexed = store.files.distinct("path", {"source": source, "path": {"$regex": pattern}, "removed_at": None})
    counts["nodes_removed"] += remove_files(store, source, sorted(set(indexed) - wanted))
    return counts


def _commit(store: Store, sid: str, last: int) -> None:
    store.sessions.update_one({"_id": sid}, {"$set": {
        "indexed_through_turn": last, "last_turn": last, "indexed_at": now(), "status": "idle", "lock_until": None}})
