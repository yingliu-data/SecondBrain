"""Add-only indexing of a folder of Python files into the store.

`index_files` hashes each file and skips it when the manifest in `files`
holds the same hash; otherwise it parses the file and compares its nodes, by
logical ID, with the newest version in `nodes`: an unchanged node keeps its
version (its lines follow the file), a changed one gets `@v(n+1)` and a
`supersedes` edge from the new version ID to the old one, and a node that is
gone gets `removed_at`. The file's edges are found by `src_path`: those no
longer parsed are deactivated, new ones inserted. Edges are resolved once
every node of the run is written. When the file's entities appeared or went
away, the files that ever had an edge into it (found by `dst`) get their edges
resolved again, so an unchanged importer follows a target that it gained,
lost or got back; an importer indexed before the target's file existed gets
the edge when it next changes. The edges into a removed file are deactivated.
Each new class or function version gets one chunk, which Atlas embeds once.
Nothing is ever deleted.

`sync` walks a folder, indexes its files, and marks the files that are gone.
Run: `python -m index_mcp.indexer sync <folder> --source code`.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from collections import Counter
from pathlib import Path

from index_mcp.graph import PARSE_ERRORS, SOURCE, FileGraph, _py_files
from index_mcp.store import Store, chunk, edge, node, now

CHUNKED_TYPES = ("class", "function")


def sync(store: Store, source: str, root: Path) -> Counter:
    """Index every `.py` file under `root`, and mark the files that are gone as removed."""
    root = root.resolve()
    paths = list(_py_files(root))
    counts = index_files(store, source, root, paths)
    present = {path.relative_to(root).as_posix() for path in paths}
    for row in store.files.find({"source": source, "removed_at": None}, {"path": 1}):
        if row["path"] not in present:
            counts["nodes_removed"] += _remove_file(store, source, row["path"])
    return counts


def index_files(store: Store, source: str, root: Path, paths: list[Path]) -> Counter:
    """Index the files at `paths` (under `root`) that changed since their manifest row was written."""
    if source != SOURCE:
        raise ValueError(f"only source {SOURCE!r} is indexed here, not {source!r}")
    counts = Counter(files=0, unchanged=0, skipped=0, nodes_inserted=0, nodes_removed=0)
    batch = _parse_changed(store, source, root, paths, counts)
    types = {nid: fields["type"] for graph, _ in batch for nid, fields in graph.nodes.items()}
    reshaped: set[str] = set()
    for graph, manifest in batch:
        inserted, before = _write_nodes(store, source, graph.rel, graph.nodes)
        counts["nodes_inserted"] += len(inserted)
        counts["nodes_removed"] += len(before - graph.nodes.keys())
        if before != graph.nodes.keys():
            reshaped |= before | graph.nodes.keys()
        store.files.update_one({"source": source, "path": graph.rel}, {
            "$set": manifest,
            "$push": {"versions": {"hash": manifest["hash"], "indexed_at": now(), "node_ids": inserted}},
        }, upsert=True)
    for graph, _ in batch:
        graph.resolve_edges(lambda nid: types.get(nid) or _stored_type(store, nid))
        _write_edges(store, graph.rel, graph.edges)
    _resolve_importers(store, root, reshaped, {graph.rel for graph, _ in batch})
    return counts


def _stored_type(store: Store, nid: str) -> str | None:
    doc = store.nodes.find_one({"nid": nid, "is_latest": True}, {"type": 1})
    return doc["type"] if doc else None


def _resolve_importers(store: Store, root: Path, nids: set[str], indexed: set[str]) -> None:
    """Resolve again the edges of the files, other than `indexed`, that ever had an edge into
    `nids`: an entity that appeared or went away changes what their imports and calls reach."""
    importers = set(store.edges.distinct("src_path", {"dst": {"$in": list(nids)}}))
    for rel in sorted(importers - indexed):
        try:
            graph = FileGraph(rel, (root / rel).read_bytes())
        except (OSError, *PARSE_ERRORS):
            continue
        graph.resolve_edges(lambda nid: _stored_type(store, nid))
        _write_edges(store, rel, graph.edges)


def _parse_changed(store: Store, source: str, root: Path, paths: list[Path], counts: Counter) -> list:
    """The parsed graph and the manifest row of each file whose hash is not the stored one.
    A file that does not parse gets a row with `skipped_reason` instead."""
    batch = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        counts["files"] += 1
        data = path.read_bytes()
        manifest = _manifest(source, path, rel, data)
        row = store.files.find_one({"source": source, "path": rel})
        if row and row["hash"] == manifest["hash"] and row["removed_at"] is None:
            store.files.update_one({"_id": row["_id"]}, {"$set": {"size": manifest["size"], "mtime": manifest["mtime"]}})
            counts["unchanged"] += 1
            continue
        try:
            batch.append((FileGraph(rel, data), manifest))
        except PARSE_ERRORS as e:
            manifest.update(hash=None, skipped_reason=f"{type(e).__name__}: {e}")
            store.files.update_one({"source": source, "path": rel}, {"$set": manifest}, upsert=True)
            counts["skipped"] += 1
    return batch


def _manifest(source: str, path: Path, rel: str, data: bytes) -> dict:
    stat = path.stat()
    return {"path": rel, "source": source, "hash": hashlib.sha256(data).hexdigest(), "size": stat.st_size,
            "mtime": stat.st_mtime, "removed_at": None, "lock_until": None, "skipped_reason": None}


def _write_nodes(store: Store, source: str, rel: str, nodes: dict[str, dict]) -> tuple[list[str], set[str]]:
    """Version the parsed nodes against the store: the IDs inserted, and the nids that were latest before."""
    before = {doc["nid"] for doc in store.nodes.find({"source": source, "path": rel, "is_latest": True}, {"nid": 1})}
    inserted = []
    for nid, fields in nodes.items():
        old = store.nodes.find_one({"nid": nid}, sort=[("version", -1)])
        doc = node(nid, source=source, version=old["version"] + 1 if old else 1, **fields)
        if old and old["text_hash"] == doc["text_hash"]:
            _keep(store, old, doc)
            continue
        store.nodes.insert_one(doc)
        if old:
            _supersede(store, old, doc)
        if doc["type"] in CHUNKED_TYPES:
            store.chunks.insert_one(chunk(doc, f"{nid}\n{doc['text']}"))
        inserted.append(doc["_id"])
    for nid in before - nodes.keys():
        _mark_removed(store, nid)
    return inserted, before


def _keep(store: Store, old: dict, doc: dict) -> None:
    """The node did not change: its version stays, its lines follow the file, and a
    version removed with its file comes back as latest."""
    store.nodes.update_one({"_id": old["_id"]}, {"$set": {
        "start_line": doc["start_line"], "end_line": doc["end_line"], "is_latest": True, "removed_at": None}})
    store.chunks.update_many({"_id": {"$regex": f"^{re.escape(old['_id'])}#"}}, {"$set": {"is_latest": True}})


def _supersede(store: Store, old: dict, doc: dict) -> None:
    store.nodes.update_one({"_id": old["_id"]}, {"$set": {
        "is_latest": False, "superseded_at": doc["created_at"], "superseded_by": doc["_id"]}})
    store.chunks.update_many({"nid": old["nid"]}, {"$set": {"is_latest": False}})
    store.edges.insert_one(edge("supersedes", doc["_id"], old["_id"], src_path=doc["path"]))


def _mark_removed(store: Store, nid: str) -> None:
    store.nodes.update_many({"nid": nid, "is_latest": True}, {"$set": {"is_latest": False, "removed_at": now()}})
    store.chunks.update_many({"nid": nid}, {"$set": {"is_latest": False}})


def _write_edges(store: Store, rel: str, edges: set[tuple[str, str, str]]) -> None:
    """Deactivate the file's parsed edges that are gone and insert the new ones; a
    `supersedes` edge is history and stays active."""
    active = {(doc["type"], doc["src"], doc["dst"]): doc["_id"]
              for doc in store.edges.find({"src_path": rel, "active": True, "type": {"$ne": "supersedes"}})}
    stale = [active[key] for key in active.keys() - edges]
    if stale:
        store.edges.update_many({"_id": {"$in": stale}}, {"$set": {"active": False, "deactivated_at": now()}})
    new = [edge(kind, src, dst, src_path=rel) for kind, src, dst in sorted(edges - active.keys())]
    if new:
        store.edges.insert_many(new)


def _remove_file(store: Store, source: str, rel: str) -> int:
    """A file that is gone: its latest nodes, its parsed edges, the edges into it and its manifest
    row are marked removed."""
    latest = [doc["nid"] for doc in store.nodes.find({"source": source, "path": rel, "is_latest": True}, {"nid": 1})]
    for nid in latest:
        _mark_removed(store, nid)
    store.edges.update_many({"$or": [{"src_path": rel}, {"dst": {"$in": latest}}], "active": True, "type": {"$ne": "supersedes"}},
                            {"$set": {"active": False, "deactivated_at": now()}})
    store.files.update_one({"source": source, "path": rel}, {"$set": {"removed_at": now()}})
    return len(latest)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Index a folder into the MongoDB store.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("sync", help="index every .py file under the folder, and mark the files that are gone")
    run.add_argument("folder", type=Path)
    run.add_argument("--source", default=SOURCE, choices=[SOURCE])
    args = parser.parse_args(argv)
    store = Store.connect()
    store.ensure_indexes()
    store.ensure_search_indexes()
    for name, count in sync(store, args.source, args.folder).items():
        print(f"{name.replace('_', ' ')}: {count}")


if __name__ == "__main__":
    main()
