"""The MongoDB store the indexer writes and the server reads.

One database holds four collections shared by every source (`code`, and
`docs` later): `files` (one manifest row per indexed file), `nodes` (one
document per version of a directory, file, class, function or section),
`edges` (between logical IDs, or between version IDs for `supersedes`, with
the file they were parsed from) and `chunks` (the text MongoDB embeds, one
per node version). A node's logical ID `nid` is `<source>/<path>` for a file
or `<source>/<path>:<name>` for an entity, and its `_id` is `<nid>@v<n>`.

Credentials are `MONGODB_USERNAME`, `MONGODB_PASSWORD`, `MONGODB_HOST` and
`MONGODB_DB`, from the environment or from a `.env` in the current folder or
a parent. The three search methods need the Atlas Search indexes described in
`atlas/*.json`; `ensure_search_indexes` creates them on Atlas, and the tests
replace the methods with in-memory versions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import find_dotenv, load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

log = logging.getLogger(__name__)

ATLAS_DIR = Path(__file__).resolve().parent.parent / "atlas"
SERVER_SELECTION_TIMEOUT_MS = 3000
# The search stages over-fetch because only `_native_filter` can go into their own filter.
OVERFETCH = 5

INDEXES = [
    ("nodes", ["nid"], {}),
    ("nodes", ["names"], {}),
    ("nodes", ["is_latest", "source"], {}),
    ("nodes", ["path"], {}),
    ("nodes", ["created_at"], {}),
    ("nodes", ["superseded_at"], {}),
    ("nodes", ["removed_at"], {}),
    ("edges", ["src"], {}),
    ("edges", ["dst"], {}),
    ("edges", ["src_path"], {}),
    ("edges", ["type", "active"], {}),
    ("files", ["source", "path"], {"unique": True}),
    ("chunks", ["nid"], {}),
]


def settings() -> dict[str, str]:
    """The `MONGODB_*` variables; a missing one is an error that names it."""
    load_dotenv(find_dotenv(usecwd=True))
    values = {name: os.environ.get(name, "") for name in ("MONGODB_USERNAME", "MONGODB_PASSWORD", "MONGODB_HOST")}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"set {', '.join(missing)} in the environment or in .env")
    values["MONGODB_DB"] = os.environ.get("MONGODB_DB", "secondbrain")
    return values


def uri(values: dict[str, str]) -> str:
    user = quote_plus(values["MONGODB_USERNAME"])
    password = quote_plus(values["MONGODB_PASSWORD"])
    return f"mongodb+srv://{user}:{password}@{values['MONGODB_HOST']}/"


def now() -> datetime:
    return datetime.now(timezone.utc)


def node(nid: str, *, type: str, source: str, path: str, title: str, heading_path: list[str],
         names: list[str], start_line: int | None, end_line: int | None, text: str, version: int = 1) -> dict:
    """One node version. `names` is what layer 2 matches; `stem` is the file stem layer 3 matches."""
    return {
        "_id": f"{nid}@v{version}",
        "nid": nid,
        "type": type,
        "source": source,
        "path": path,
        "title": title,
        "heading_path": heading_path,
        "names": names,
        "stem": "" if type == "directory" else Path(path).stem,
        "start_line": start_line,
        "end_line": end_line,
        "text": text,
        "text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "version": version,
        "is_latest": True,
        "created_at": now(),
        "superseded_at": None,
        "superseded_by": None,
        "removed_at": None,
    }


def edge(edge_type: str, src: str, dst: str, *, src_path: str, ambiguous: bool = False) -> dict:
    """One edge between logical IDs (version IDs for `supersedes`), found again by the file it was parsed from."""
    return {
        "type": edge_type,
        "src": src,
        "dst": dst,
        "src_path": src_path,
        "active": True,
        "created_at": now(),
        "deactivated_at": None,
        "ambiguous": ambiguous,
    }


def chunk(node_doc: dict, text: str, n: int = 1) -> dict:
    """Chunk `n` of a node version: what Atlas embeds, found again by the node's `nid`."""
    return {
        "_id": f"{node_doc['_id']}#{n}",
        "nid": node_doc["nid"],
        "source": node_doc["source"],
        "path": node_doc["path"],
        "is_latest": True,
        "text": text,
    }


def query_filter(*, source: str | None = None, path_prefix: str | None = None, latest_only: bool = True) -> dict:
    """The ordinary query for the filters every search layer takes."""
    query: dict = {}
    if latest_only:
        query["is_latest"] = True
    if source:
        query["source"] = source
    if path_prefix:
        query["path"] = {"$regex": "^" + re.escape(path_prefix)}
    return query


def search_index_specs() -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted(ATLAS_DIR.glob("*.json"))]


class Store:
    def __init__(self, client: MongoClient, db_name: str):
        self.client = client
        self.db = client[db_name]
        self.files = self.db["files"]
        self.nodes = self.db["nodes"]
        self.edges = self.db["edges"]
        self.chunks = self.db["chunks"]

    @classmethod
    def connect(cls) -> Store:
        values = settings()
        client = MongoClient(uri(values), serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS, tz_aware=True)
        return cls(client, values["MONGODB_DB"])

    def ensure_indexes(self) -> None:
        for collection, keys, options in INDEXES:
            self.db[collection].create_index([(key, ASCENDING) for key in keys], **options)

    def ensure_search_indexes(self) -> None:
        """Create the Atlas Search indexes of `atlas/*.json` that do not exist yet; a warning off Atlas."""
        for spec in search_index_specs():
            collection = self.db[spec["collection"]]
            try:
                existing = {index["name"] for index in collection.list_search_indexes()}
            except OperationFailure as exc:
                log.warning("Atlas Search index %s not created: %s", spec["name"], exc)
                continue
            if spec["name"] not in existing:
                collection.create_search_index(SearchIndexModel(spec["definition"], name=spec["name"], type=spec["type"]))

    def keyword_names(self, query: str, match: dict, limit: int = 10) -> list[dict]:
        """Layer 3: Atlas Search over `names`, `heading_path` and the file stem, within `match`."""
        stage = _search_stage("nodes_search", query, ["names", "heading_path", "stem"], match)
        return self._run(self.nodes, stage, match, limit)

    def keyword_text(self, query: str, match: dict, limit: int = 10) -> list[dict]:
        """Layer 4, keyword half: Atlas Search over chunk text, within `match`."""
        stage = _search_stage("chunks_search", query, ["text"], match)
        return self._run(self.chunks, stage, match, limit)

    def vector(self, query: str, match: dict, limit: int = 10) -> list[dict]:
        """Layer 4, semantic half: `$vectorSearch` over chunk text, embedded by Atlas, within `match`."""
        stage = {"$vectorSearch": {
            "index": "chunks_vector",
            "path": "text",
            "query": query,
            "numCandidates": limit * OVERFETCH * 10,
            "limit": limit * OVERFETCH,
            "filter": _native_filter(match),
        }}
        return self._run(self.chunks, stage, match, limit)

    def _run(self, collection, stage: dict, match: dict, limit: int) -> list[dict]:
        return list(collection.aggregate([stage, {"$match": match}, {"$limit": limit}]))


def _native_filter(match: dict) -> dict:
    """The part of `match` a search stage filters on itself; the rest is matched after it."""
    return {key: match[key] for key in ("is_latest", "source") if key in match}


def _search_stage(index: str, query: str, paths: list[str], match: dict) -> dict:
    compound: dict = {"must": [{"text": {"query": query, "path": paths}}]}
    if filters := [{"equals": {"path": key, "value": value}} for key, value in _native_filter(match).items()]:
        compound["filter"] = filters
    return {"$search": {"index": index, "compound": compound}}
