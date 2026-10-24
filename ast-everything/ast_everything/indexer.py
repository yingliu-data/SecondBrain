"""Add-only indexing of a folder of Python and Markdown files into the store.

`index_files` hashes each file and skips it when the manifest in `files`
holds the same hash; otherwise it parses the file and compares its nodes, by
logical ID, with the newest version in `nodes`: an unchanged node keeps its
version (its lines follow the file), a changed one gets `@v(n+1)` and a
`supersedes` edge from the new version ID to the old one, and a node that is
gone gets `removed_at`. The file's edges are found by source and `src_path`: those no
longer parsed are deactivated, new ones inserted. Edges are resolved once
every node of the run is written. When the file's entities appeared or went
away, the files that ever had an edge into it (found by `dst`) get their edges
resolved again, so an unchanged importer follows a target that it gained,
lost or got back; an importer indexed before the target's file existed gets
the edge when it next changes. The edges into a removed file are deactivated.
Each new version of a node type that its parser chunks (a class or function;
a document or section) gets one chunk, which Atlas embeds once. Nothing is
ever deleted.

A source is one indexed project or folder, named by `valid_source` rules; its
IDs start with `<source>/`, so two sources never share a node.

`sync` walks a folder, indexes its files, and marks the files that are gone.
Run: `python -m ast_everything.indexer sync <folder> [--source NAME]`.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from collections import Counter
from pathlib import Path

from ast_everything.parsers import PARSE_ERRORS, iter_files, parse
from ast_everything.store import Store, chunk, edge, node, now

SOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def valid_source(name: str) -> str:
    """`name` when it can head an ID: letters, digits, `.`, `_` and `-`, at most 100."""
    if not SOURCE_NAME.fullmatch(name or ""):
        raise ValueError(f"source name {name!r}: use letters, digits, '.', '_' or '-' (at most 100)")
    return name


def source_name(text: str) -> str:
    """A source name made from a folder or repo name: other characters become `-`."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")[:100]
    return valid_source(name or "source")


def sync(store: Store, source: str, root: Path) -> Counter:
    """Index every file under `root` that a parser reads, and mark the files that are gone as removed."""
    root = root.resolve()
    paths = list(iter_files(root))
    counts = index_files(store, source, root, paths)
    present = {path.relative_to(root).as_posix() for path in paths}
    for row in store.files.find({"source": source, "removed_at": None}, {"path": 1}):
        if row["path"] not in present:
            counts["nodes_removed"] += _remove_file(store, source, row["path"])
    return counts


def index_files(store: Store, source: str, root: Path, paths: list[Path]) -> Counter:
    """Index the files at `paths` (under `root`) that changed since their manifest row was written."""
    valid_source(source)
    counts = Counter(files=0, unchanged=0, skipped=0, nodes_inserted=0, nodes_removed=0)
    batch = _parse_changed(store, source, root, paths, counts)
    types = {nid: fields["type"] for graph, _ in batch for nid, fields in graph.nodes.items()}
    reshaped: set[str] = set()
    for graph, manifest in batch:
        inserted, before = _write_nodes(store, source, graph)
        counts["nodes_inserted"] += len(inserted)
        counts["nodes_removed"] += len(before - graph.nodes.keys())
        entities = {nid for nid, fields in graph.nodes.items() if fields["type"] != "directory"}
        if before != entities:
            reshaped |= before | entities
        store.files.update_one({"source": source, "path": graph.rel}, {
            "$set": manifest,
            "$push": {"versions": {"hash": manifest["hash"], "indexed_at": now(), "node_ids": inserted}},
        }, upsert=True)
    for graph, _ in batch:
        graph.resolve_edges(lambda nid: types.get(nid) or _stored_type(store, nid))
        _write_edges(store, source, graph.rel, graph.edges)
    _resolve_importers(store, source, root, reshaped, {graph.rel for graph, _ in batch})
    return counts


def _stored_type(store: Store, nid: str) -> str | None:
    doc = store.nodes.find_one({"nid": nid, "is_latest": True}, {"type": 1})
    return doc["type"] if doc else None


def _resolve_importers(store: Store, source: str, root: Path, nids: set[str], indexed: set[str]) -> None:
    """Resolve again the edges of the files, other than `indexed`, that ever had an edge into
    `nids`: an entity that appeared or went away changes what their imports, calls and links reach."""
    importers = set(store.edges.distinct("src_path", {"source": source, "dst": {"$in": list(nids)}}))
    for rel in sorted(importers - indexed):
        try:
            graph = parse(source, rel, (root / rel).read_bytes())
        except (OSError, *PARSE_ERRORS):
            continue
        graph.resolve_edges(lambda nid: _stored_type(store, nid))
        _write_edges(store, source, rel, graph.edges)


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
            batch.append((parse(source, rel, data), manifest))
        except PARSE_ERRORS as e:
            manifest.update(hash=None, skipped_reason=f"{type(e).__name__}: {e}")
            store.files.update_one({"source": source, "path": rel}, {"$set": manifest}, upsert=True)
            counts["skipped"] += 1
    return batch


def _manifest(source: str, path: Path, rel: str, data: bytes) -> dict:
    stat = path.stat()
    return {"path": rel, "source": source, "hash": hashlib.sha256(data).hexdigest(), "size": stat.st_size,
            "mtime": stat.st_mtime, "removed_at": None, "lock_until": None, "skipped_reason": None}


def _write_nodes(store: Store, source: str, graph) -> tuple[list[str], set[str]]:
    """Version the parsed nodes against the store: the IDs inserted, and the nids that were latest before."""
    rel, nodes = graph.rel, graph.nodes
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
        if doc["type"] in graph.chunked and doc["text"].strip():
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
    store.edges.insert_one(edge("supersedes", doc["_id"], old["_id"], source=doc["source"], src_path=doc["path"]))


def _mark_removed(store: Store, nid: str) -> None:
    store.nodes.update_many({"nid": nid, "is_latest": True}, {"$set": {"is_latest": False, "removed_at": now()}})
    store.chunks.update_many({"nid": nid}, {"$set": {"is_latest": False}})


def _write_edges(store: Store, source: str, rel: str, edges: set[tuple[str, str, str]]) -> None:
    """Deactivate the file's parsed edges that are gone and insert the new ones; a
    `supersedes` edge is history and stays active."""
    active = {(doc["type"], doc["src"], doc["dst"]): doc["_id"]
              for doc in store.edges.find({"source": source, "src_path": rel, "active": True,
                                           "type": {"$ne": "supersedes"}})}
    stale = [active[key] for key in active.keys() - edges]
    if stale:
        store.edges.update_many({"_id": {"$in": stale}}, {"$set": {"active": False, "deactivated_at": now()}})
    new = [edge(kind, src, dst, source=source, src_path=rel) for kind, src, dst in sorted(edges - active.keys())]
    if new:
        store.edges.insert_many(new)


def _remove_file(store: Store, source: str, rel: str) -> int:
    """A file that is gone: its latest nodes, its parsed edges, the edges into it and its manifest
    row are marked removed."""
    latest = [doc["nid"] for doc in store.nodes.find({"source": source, "path": rel, "is_latest": True}, {"nid": 1})]
    for nid in latest:
        _mark_removed(store, nid)
    store.edges.update_many({"source": source, "$or": [{"src_path": rel}, {"dst": {"$in": latest}}], "active": True,
                             "type": {"$ne": "supersedes"}},
                            {"$set": {"active": False, "deactivated_at": now()}})
    store.files.update_one({"source": source, "path": rel}, {"$set": {"removed_at": now()}})
    return len(latest)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Index a folder into the MongoDB store.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("sync", help="index every Python and Markdown file under the folder, "
                                           "and mark the files that are gone")
    run.add_argument("folder", type=Path)
    run.add_argument("--source", help="the source name (default: made from the folder's name)")
    args = parser.parse_args(argv)
    source = valid_source(args.source) if args.source else source_name(args.folder.resolve().name)
    store = Store.connect()
    store.ensure_indexes()
    store.ensure_search_indexes()
    print(f"source: {source}")
    for name, count in sync(store, source, args.folder).items():
        print(f"{name.replace('_', ' ')}: {count}")


if __name__ == "__main__":
    main()
