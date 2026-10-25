"""The read side of the store: the indexed sources, the four search layers, a graph walk, the text
of an entity, and what changed in a time range.

`search` tries the exact ID, the name, Atlas Search over names, and chunk
content (keyword and vector, fused by reciprocal rank), and stops at the
first layer that finds something. Every layer takes the same filters:
`scope` (the source), `node_types`, `path_prefix`, `time_range` on
`created_at`, `history`, without which only the latest versions are
seen, and `hide`, sources an unscoped search leaves out. Each layer is one indexed query; nothing is built in memory.

Every answer renders nodes in one shape for code and docs: ID, type,
location, status (latest, superseded or removed) and, by `detail`, nothing
more (fold), a preview, or the full text.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from ast_everything.store import Store, query_filter

log = logging.getLogger(__name__)

PREVIEW_LINES = 5
PAGE_LINES = 200
RRF_K = 60   # the usual constant of reciprocal rank fusion
OVERFETCH = 5   # chunks fetched per wanted node in layer 4: the node filters apply after the fusion
DEF_LINE = re.compile(r"\s*(async\s+)?(def|class)\s")
TIMESTAMPS = ("created_at", "superseded_at", "removed_at")
CHUNK_FIELDS = ("is_latest", "source", "path")


def search(store: Store, query: str, *, scope: str | None = None, node_types: list[str] | None = None,
           path_prefix: str | None = None, time_range: list[str | None] | None = None, history: bool = False,
           detail: str = "preview", limit: int = 10, hide: list[str] | None = None) -> dict:
    """The results of the first layer that finds something; `layer` is None when none does."""
    match = node_filter(scope=scope, node_types=node_types, path_prefix=path_prefix,
                        time_range=time_range, history=history, hide=hide)
    layers = (
        lambda: store.nodes.find({**match, "$or": [{"_id": query}, {"nid": query}]}).sort("version", -1).limit(limit),
        lambda: store.nodes.find({**match, "names": query.strip().lower()}).sort("_id").limit(limit),
        lambda: store.keyword_names(query, match, limit),
        lambda: _content(store, query, match, limit),
    )
    for layer, find in enumerate(layers, 1):
        hits = list(find())
        if hits:
            return {"layer": layer, "results": [render(node, detail) for node in hits]}
    return {"layer": None, "results": []}


def node_filter(*, scope: str | None = None, node_types: list[str] | None = None, path_prefix: str | None = None,
                time_range: list[str | None] | None = None, history: bool = False,
                hide: list[str] | None = None) -> dict:
    """The filters every layer takes; `hide` names sources an unscoped search leaves out."""
    match = query_filter(source=scope, path_prefix=path_prefix, latest_only=not history)
    if hide and not scope:
        match["source"] = {"$nin": hide}
    if node_types:
        match["type"] = {"$in": list(node_types)}
    if time_range and any(time_range):
        match["created_at"] = between(time_range)
    return match


def between(time_range: list[str | None]) -> dict:
    """`[start, end]` as ISO 8601 strings, one of them may be empty, as a MongoDB range."""
    start, end = time_range
    bounds = {"$gte": _moment(start), "$lte": _moment(end)}
    if not any(bounds.values()):
        raise ValueError("a time range needs a start or an end")
    return {op: value for op, value in bounds.items() if value}


def _moment(text: str | None) -> datetime | None:
    if not text:
        return None
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _content(store: Store, query: str, match: dict, limit: int) -> list[dict]:
    """Layer 4: chunks by keyword and by vector, fused by reciprocal rank, mapped to their
    node versions; keyword results alone when vector search fails. Chunks carry only
    CHUNK_FIELDS, so both halves over-fetch and the other filters apply to the nodes."""
    chunk_match = {key: match[key] for key in CHUNK_FIELDS if key in match}
    rankings = [store.keyword_text(query, chunk_match, limit * OVERFETCH)]
    try:
        rankings.append(store.vector(query, chunk_match, limit * OVERFETCH))
    except Exception as exc:
        log.warning("vector search failed, keyword results only: %s", exc)
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, 1):
            node_id = chunk["_id"].rsplit("#", 1)[0]
            scores[node_id] = scores.get(node_id, 0) + 1 / (RRF_K + rank)
    ordered = sorted(scores, key=scores.__getitem__, reverse=True)
    nodes = {doc["_id"]: doc for doc in store.nodes.find({**match, "_id": {"$in": ordered}})}
    return [nodes[node_id] for node_id in ordered if node_id in nodes][:limit]


def recall(store: Store, query: str, *, scope: str, limit: int = 3, min_score: float = 0.0,
           exclude_paths: list[str] | None = None) -> list[dict]:
    """The latest nodes of `scope` nearest `query` by meaning, best first, each with `score`, its best
    chunk's: what to bring back unasked, so by vector alone (a keyword search matches common words)
    and only a chunk scoring `min_score` or more. A node at or under one of `exclude_paths` (a file,
    or a folder such as a session's) is left out, the exact paths before the vector search's own
    limit (a session's turns are at its path), so they cannot crowd out the rest. Vector search
    failing raises."""
    excluded = [path.rstrip("/") for path in exclude_paths or []]
    match = node_filter(scope=scope)
    chunk_match = {key: match[key] for key in CHUNK_FIELDS if key in match}
    if excluded:
        chunk_match["path"] = {"$nin": excluded}
    best: dict[str, float] = {}
    for found in store.vector(query, chunk_match, limit * OVERFETCH):
        node_id, score = found["_id"].rsplit("#", 1)[0], found.get("score", 0.0)
        if score >= min_score and score > best.get(node_id, -1.0):
            best[node_id] = score
    nodes = [doc for doc in store.nodes.find({**match, "_id": {"$in": list(best)}})
             if not any(doc["path"] == path or doc["path"].startswith(path + "/") for path in excluded)]
    nodes.sort(key=lambda doc: (-best[doc["_id"]], doc["_id"]))
    return [{**doc, "score": best[doc["_id"]]} for doc in nodes[:limit]]


def render(node: dict, detail: str = "preview") -> dict:
    out = {"id": node["_id"], "type": node["type"], "location": _location(node), "status": _status(node)}
    if node["heading_path"]:
        out["heading_path"] = node["heading_path"]
    if detail == "preview":
        out["preview"] = _preview(node)
    elif detail == "full":
        out["text"] = node["text"]
    return out


def _location(node: dict) -> str:
    if node["start_line"] is None:
        return node["path"]
    return f"{node['path']}:{node['start_line']}-{node['end_line']}"


def _status(node: dict) -> str:
    if node["superseded_by"]:
        return "superseded"
    if node["removed_at"]:
        return "removed"
    return "latest"


def _preview(node: dict) -> str:
    """The def or class line of an entity; the first lines of anything else."""
    lines = node["text"].split("\n")
    if node["type"] in ("class", "function"):
        return next((line for line in lines if DEF_LINE.match(line)), lines[0])
    return "\n".join(lines[:PREVIEW_LINES])


def traverse(store: Store, start_ids: list[str], *, direction: str = "out", hops: int = 1,
             node_types: list[str] | None = None, edge_types: list[str] | None = None,
             max_fanout: int = 20) -> dict:
    """The nodes within `hops` active edges of `start_ids`, one indexed query per hop and at
    most `max_fanout` edges per node. `direction` is out, in or both. An ID is a logical one
    (its latest version) or a version ID; a `supersedes` edge joins two version IDs."""
    nodes = _nodes_by_id(store, set(start_ids), None)
    known = frontier = _keys(nodes)
    edges: list[dict] = []
    for _ in range(hops):
        found = [(edge, _ends(edge, frontier, direction)[1]) for edge in _hop(store, frontier, direction, edge_types, max_fanout)]
        reached = _nodes_by_id(store, {other for _, other in found} - known, node_types)
        nodes.update(reached)
        known |= _keys(reached)
        edges += [{"type": edge["type"], "src": edge["src"], "dst": edge["dst"]} for edge, other in found if other in known]
        frontier = _keys(reached)
        if not frontier:
            break
    return {"nodes": [render(doc, "fold") for doc in nodes.values()], "edges": edges}


def _nodes_by_id(store: Store, ids: set[str], node_types: list[str] | None) -> dict[str, dict]:
    """The nodes the IDs name, by version ID: a version ID as it was, a logical ID its latest version.
    Without their text: a walk renders them folded."""
    query: dict = {"$or": [{"_id": {"$in": list(ids)}}, {"nid": {"$in": list(ids)}, "is_latest": True}]}
    if node_types:
        query["type"] = {"$in": list(node_types)}
    return {doc["_id"]: doc for doc in store.nodes.find(query, {"text": 0})}


def _keys(nodes: dict[str, dict]) -> set[str]:
    """Both IDs of each node, which is what an edge end may hold."""
    return {key for doc in nodes.values() for key in (doc["_id"], doc["nid"])}


def _hop(store: Store, frontier: set[str], direction: str, edge_types: list[str] | None, max_fanout: int) -> list[dict]:
    """The active edges leaving (out), entering (in) or touching (both) the frontier, at most
    `max_fanout` per frontier node, in a stable order."""
    ids = list(frontier)
    query: dict = {"active": True}
    if edge_types:
        query["type"] = {"$in": list(edge_types)}
    if direction == "both":
        query["$or"] = [{"src": {"$in": ids}}, {"dst": {"$in": ids}}]
    else:
        query["src" if direction == "out" else "dst"] = {"$in": ids}
    taken: dict[str, int] = {}
    kept = []
    for edge in store.edges.find(query).sort([("src", 1), ("dst", 1)]):
        anchor, _ = _ends(edge, frontier, direction)
        if taken.get(anchor, 0) < max_fanout:
            taken[anchor] = taken.get(anchor, 0) + 1
            kept.append(edge)
    return kept


def _ends(edge: dict, frontier: set[str], direction: str) -> tuple[str, str]:
    """(the end of `edge` in the frontier, the other end) for the walk's direction."""
    if direction != "out" and edge["dst"] in frontier:
        return edge["dst"], edge["src"]
    return edge["src"], edge["dst"]


def retrieve(store: Store, ids: list[str], *, detail: str = "full", context_lines: int = 0,
             page: int = 1) -> list[dict]:
    """The text of each ID (a version ID, or a logical ID for its latest version) with its line
    range when the text is contiguous, `PAGE_LINES` lines a page, and the newest version's ID
    for a superseded node."""
    out = []
    for node_id in ids:
        node = (store.nodes.find_one({"_id": node_id})
                or store.nodes.find_one({"nid": node_id}, sort=[("is_latest", -1), ("version", -1)]))
        if node is None:
            out.append({"id": node_id, "error": "unknown ID"})
            continue
        item = render(node, detail)
        if detail == "full":
            lines, first = _with_context(store, node, context_lines)
            item.update(_page(lines, first if _contiguous(node) else None, page))
        if node["superseded_by"]:
            item["newest"] = store.nodes.find_one({"nid": node["nid"]}, sort=[("version", -1)])["_id"]
        out.append(item)
    return out


def _with_context(store: Store, node: dict, context_lines: int) -> tuple[list[str], int]:
    """The node's lines and its first line number, widened by `context_lines` on each side
    from the node that contains it when that node's text is contiguous (a class is; a file
    node is not, so a top-level entity gets no context)."""
    lines, first = node["text"].split("\n"), node["start_line"] or 1
    if not context_lines:
        return lines, first
    edge = store.edges.find_one({"type": "contains", "dst": node["nid"], "active": True})
    parent = edge and store.nodes.find_one({"nid": edge["src"], "is_latest": True})
    if not parent or parent["start_line"] is None or not _contiguous(parent):
        return lines, first
    parent_lines = parent["text"].split("\n")
    start = max(parent["start_line"], first - context_lines)
    end = min(parent["end_line"], node["end_line"] + context_lines)
    return parent_lines[start - parent["start_line"]:end - parent["start_line"] + 1], start


def _contiguous(node: dict) -> bool:
    """Whether the text is the lines from `start_line` to `end_line`; a file node's text is
    only its module-level lines, so its line range would not match it."""
    return node["start_line"] is None or node["text"].count("\n") == node["end_line"] - node["start_line"]


def _page(lines: list[str], first: int | None, page: int) -> dict:
    """Page `page` of `lines`, with its line range when `first` is known."""
    pages = max(1, -(-len(lines) // PAGE_LINES))
    page = min(max(page, 1), pages)
    offset = (page - 1) * PAGE_LINES
    shown = lines[offset:offset + PAGE_LINES]
    out = {"text": "\n".join(shown)}
    if first is not None:
        out["lines"] = [first + offset, first + offset + len(shown) - 1]
    if pages > 1:
        out.update(page=page, pages=pages)
    return out


def by_time(store: Store, time_range: list[str | None], *, scope: str | None = None,
            node_types: list[str] | None = None, limit: int = 50, hide: list[str] | None = None) -> list[dict]:
    """The nodes added, superseded or removed in `time_range`, newest change first: the top
    `limit` of each timestamp's indexed query, merged, hold the top `limit` overall."""
    window = between(time_range)
    match = node_filter(scope=scope, node_types=node_types, history=True, hide=hide)
    changed: dict[str, dict] = {}
    for field in TIMESTAMPS:
        for node in store.nodes.find({**match, field: window}, {"text": 0}).sort(field, -1).limit(limit):
            changed[node["_id"]] = node
    newest = sorted(changed.values(), key=lambda node: _changed_at(node, window), reverse=True)[:limit]
    return [{**render(node, "fold"), "changed_at": _changed_at(node, window).isoformat(timespec="seconds")}
            for node in newest]


def _changed_at(node: dict, window: dict) -> datetime:
    """The latest of the node's timestamps inside `window`."""
    moments = (node[field] for field in TIMESTAMPS)
    return max(moment for moment in moments if moment and window.get("$gte", moment) <= moment <= window.get("$lte", moment))


def sources(store: Store) -> list[dict]:
    """Each indexed source with its count of present files and of files skipped for a parse error."""
    rows = store.files.aggregate([
        {"$match": {"removed_at": None}},
        {"$group": {"_id": "$source", "files": {"$sum": 1},
                    "skipped": {"$sum": {"$cond": [{"$eq": ["$hash", None]}, 1, 0]}}}},
        {"$sort": {"_id": 1}},
    ])
    return [{"source": row["_id"], "files": row["files"], "skipped": row["skipped"]} for row in rows]
