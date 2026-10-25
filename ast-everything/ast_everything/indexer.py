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

A file's manifest row (its hash and versions) is written last, once its nodes
and edges are in, so a run that stops part-way leaves those files unindexed
and the next run parses them again; their unchanged nodes are kept, so
nothing is inserted twice. The store is read in bulk: the manifest of the
source once per run, a file's stored nodes in one query, and the types of
the source's latest nodes once for the edge pass; a file's writes go out as
a few batches.

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

from pymongo import InsertOne, UpdateMany, UpdateOne

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
    rows = _manifest_rows(store, source)
    counts = index_files(store, source, root, paths, rows=rows)
    present = {path.relative_to(root).as_posix() for path in paths}
    for rel, row in rows.items():
        if row["removed_at"] is None and rel not in present:
            counts["nodes_removed"] += _remove_file(store, source, rel)
    return counts


def index_files(store: Store, source: str, root: Path, paths: list[Path], *, rows: dict | None = None) -> Counter:
    """Index the files at `paths` (under `root`) that changed since their manifest row was written.
    `rows` is the source's manifest by path, when the caller has read it already."""
    valid_source(source)
    counts = Counter(files=0, unchanged=0, skipped=0, nodes_inserted=0, nodes_removed=0)
    if rows is None:
        rows = _manifest_rows(store, source)
    batch = _parse_changed(store, source, root, paths, rows, counts)
    reshaped: set[str] = set()
    manifests = []
    for graph, manifest in batch:
        inserted, before = _write_nodes(store, source, graph)
        counts["nodes_inserted"] += len(inserted)
        counts["nodes_removed"] += len(before - graph.nodes.keys())
        entities = {nid for nid, fields in graph.nodes.items() if fields["type"] != "directory"}
        if before != entities:
            reshaped |= before | entities
        manifests.append(UpdateOne({"source": source, "path": graph.rel}, {
            "$set": manifest,
            "$push": {"versions": {"hash": manifest["hash"], "indexed_at": now(), "node_ids": inserted}},
        }, upsert=True))
    if batch:
        types = _latest_types(store, source)     # after the node writes: a node removed now links nowhere
        for graph, _ in batch:
            graph.resolve_edges(types.get)
            _write_edges(store, source, graph.rel, graph.edges)
        _resolve_importers(store, source, root, reshaped, {graph.rel for graph, _ in batch}, types)
        store.files.bulk_write(manifests, ordered=False)
    return counts


def _manifest_rows(store: Store, source: str) -> dict[str, dict]:
    return {row["path"]: row for row in store.files.find({"source": source}, {"versions": 0})}


def _latest_types(store: Store, source: str) -> dict[str, str]:
    """The type of each latest node of the source, by logical ID: what `resolve_edges` looks up."""
    return {doc["nid"]: doc["type"] for doc in store.nodes.find({"source": source, "is_latest": True}, {"nid": 1, "type": 1})}


def _resolve_importers(store: Store, source: str, root: Path, nids: set[str], indexed: set[str],
                       types: dict[str, str]) -> None:
    """Resolve again the edges of the files, other than `indexed`, that ever had an edge into
    `nids`: an entity that appeared or went away changes what their imports, calls and links reach."""
    if not nids:
        return
    importers = set(store.edges.distinct("src_path", {"source": source, "dst": {"$in": list(nids)}}))
    for rel in sorted(importers - indexed):
        try:
            graph = parse(source, rel, (root / rel).read_bytes())
        except (OSError, *PARSE_ERRORS):
            continue
        graph.resolve_edges(types.get)
        _write_edges(store, source, rel, graph.edges)


def _parse_changed(store: Store, source: str, root: Path, paths: list[Path], rows: dict[str, dict],
                   counts: Counter) -> list:
    """The parsed graph and the manifest row of each file whose hash is not the stored one.
    A file that does not parse gets a row with `skipped_reason` instead. An unchanged file's
    row is written only when its size or mtime moved."""
    batch, writes = [], []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        counts["files"] += 1
        data = path.read_bytes()
        manifest = _manifest(source, path, rel, data)
        row = rows.get(rel)
        if row and row["hash"] == manifest["hash"] and row["removed_at"] is None:
            if (row.get("size"), row.get("mtime")) != (manifest["size"], manifest["mtime"]):
                writes.append(UpdateOne({"_id": row["_id"]}, {"$set": {"size": manifest["size"], "mtime": manifest["mtime"]}}))
            counts["unchanged"] += 1
            continue
        try:
            batch.append((parse(source, rel, data), manifest))
        except PARSE_ERRORS as e:
            manifest.update(hash=None, skipped_reason=f"{type(e).__name__}: {e}")
            writes.append(UpdateOne({"source": source, "path": rel}, {"$set": manifest}, upsert=True))
            counts["skipped"] += 1
    if writes:
        store.files.bulk_write(writes, ordered=False)
    return batch


def _manifest(source: str, path: Path, rel: str, data: bytes) -> dict:
    stat = path.stat()
    return {"path": rel, "source": source, "hash": hashlib.sha256(data).hexdigest(), "size": stat.st_size,
            "mtime": stat.st_mtime, "removed_at": None, "skipped_reason": None}


NODE_STATE = {"text": 0, "names": 0, "heading_path": 0}   # what `_write_nodes` needs of a stored node


class _Writes:
    """One file's writes, sent as a few batches in an order that keeps them right: new nodes,
    then the updates of old ones, then chunk updates before the new chunks (superseding a
    node marks its chunks old, which must not reach the new version's chunk), then edges."""

    def __init__(self):
        self.nodes, self.node_updates, self.chunk_updates, self.chunks, self.edges = [], [], [], [], []

    def send(self, store: Store) -> None:
        for collection, ops in ((store.nodes, self.nodes), (store.nodes, self.node_updates),
                                (store.chunks, self.chunk_updates), (store.chunks, self.chunks),
                                (store.edges, self.edges)):
            if ops:
                collection.bulk_write(ops, ordered=True)


def _write_nodes(store: Store, source: str, graph) -> tuple[list[str], set[str]]:
    """Version the parsed nodes against the store: the IDs inserted, and the nids that were latest before.
    The stored versions of the file's nodes come in one query, newest first."""
    rel, nodes = graph.rel, graph.nodes
    newest: dict[str, dict] = {}
    before: set[str] = set()
    query = {"$or": [{"nid": {"$in": list(nodes)}}, {"source": source, "path": rel, "is_latest": True}]}
    for doc in store.nodes.find(query, NODE_STATE).sort("version", -1):
        newest.setdefault(doc["nid"], doc)
        if doc["is_latest"] and doc["source"] == source and doc["path"] == rel:
            before.add(doc["nid"])
    writes, inserted = _Writes(), []
    for nid, fields in nodes.items():
        old = newest.get(nid)
        doc = node(nid, source=source, version=old["version"] + 1 if old else 1, **fields)
        if old and old["text_hash"] == doc["text_hash"]:
            _keep(writes, old, doc)
            continue
        writes.nodes.append(InsertOne(doc))
        if old:
            _supersede(writes, old, doc)
        if doc["type"] in graph.chunked and doc["text"].strip():
            writes.chunks.append(InsertOne(chunk(doc, f"{nid}\n{doc['text']}")))
        inserted.append(doc["_id"])
    for nid in before - nodes.keys():
        _mark_removed(writes, nid)
    writes.send(store)
    return inserted, before


def _keep(writes: _Writes, old: dict, doc: dict) -> None:
    """The node did not change: its version stays, its lines follow the file, and a
    version removed with its file comes back as latest. A latest node whose lines
    did not move needs no write."""
    restored = not old["is_latest"] or old["removed_at"] is not None
    if restored or (old["start_line"], old["end_line"]) != (doc["start_line"], doc["end_line"]):
        writes.node_updates.append(UpdateOne({"_id": old["_id"]}, {"$set": {
            "start_line": doc["start_line"], "end_line": doc["end_line"], "is_latest": True, "removed_at": None}}))
    if restored:
        writes.chunk_updates.append(UpdateMany({"_id": {"$regex": f"^{re.escape(old['_id'])}#"}},
                                               {"$set": {"is_latest": True}}))


def _supersede(writes: _Writes, old: dict, doc: dict) -> None:
    writes.node_updates.append(UpdateOne({"_id": old["_id"]}, {"$set": {
        "is_latest": False, "superseded_at": doc["created_at"], "superseded_by": doc["_id"]}}))
    writes.chunk_updates.append(UpdateMany({"nid": old["nid"]}, {"$set": {"is_latest": False}}))
    writes.edges.append(InsertOne(edge("supersedes", doc["_id"], old["_id"], source=doc["source"], src_path=doc["path"])))


def _mark_removed(writes: _Writes, nid: str) -> None:
    writes.node_updates.append(UpdateMany({"nid": nid, "is_latest": True}, {"$set": {"is_latest": False, "removed_at": now()}}))
    writes.chunk_updates.append(UpdateMany({"nid": nid}, {"$set": {"is_latest": False}}))


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


def remove_files(store: Store, source: str, rels: list[str]) -> int:
    """Mark each of the files at `rels` that is indexed and not removed yet as removed; the nodes removed."""
    present = store.files.distinct("path", {"source": source, "path": {"$in": rels}, "removed_at": None})
    return sum(_remove_file(store, source, rel) for rel in present)


def _remove_file(store: Store, source: str, rel: str) -> int:
    """A file that is gone: its latest nodes, its parsed edges, the edges into it and its manifest
    row are marked removed."""
    latest = [doc["nid"] for doc in store.nodes.find({"source": source, "path": rel, "is_latest": True}, {"nid": 1})]
    writes = _Writes()
    for nid in latest:
        _mark_removed(writes, nid)
    writes.send(store)
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
    store.prepare()
    print(f"source: {source}")
    for name, count in sync(store, source, args.folder).items():
        print(f"{name.replace('_', ' ')}: {count}")


if __name__ == "__main__":
    main()
