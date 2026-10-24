"""Search layers over a saved code graph, after LocAgent.

`CodeIndex.search` tries the exact ID, then the short name, then BM25 over the
ID strings, and stops at the first layer that finds something. Only files,
classes and functions outside test files are searched; directories and test
files stay in the graph.

Each repo has one index folder under $CODE_INDEX_HOME (default
~/.cache/secondbrain/code-index): `graph.json`, `bm25/`, and `manifest.json`,
which is written last, so a build that stopped half way loads as damaged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import bm25s

from code_index.graph import build_graph

ENTITY_TYPES = ("file", "class", "function")
TOP_K = 10
PREVIEW_LINES = 5


class CodeIndexError(Exception):
    """A plain message for the caller: a bad repo path, or an index to build again."""


def split_words(text: str) -> list[str]:
    """`src/MathUtils.calculate_sum` -> ['src', 'math', 'utils', 'calculate', 'sum']."""
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", text)]


def is_test_file(path: str) -> bool:
    """A file under `test/` or `tests/`, or named `test_*.py` or `*_test.py`."""
    *folders, name = path.split("/")
    return bool({"test", "tests"} & set(folders)) or name.startswith("test_") or name.endswith("_test.py")


def index_dir(repo: Path) -> Path:
    home = Path(os.environ.get("CODE_INDEX_HOME", "~/.cache/secondbrain/code-index")).expanduser()
    return home / hashlib.sha256(str(repo).encode()).hexdigest()[:16]


def build_index(repo_path: str) -> dict:
    """Build the graph and the search index of one repo, save them, and return the manifest."""
    repo = _check_repo(repo_path)
    graph = build_graph(repo)
    ids = _searchable(graph["nodes"])
    if not ids:
        raise CodeIndexError(
            f"no Python files to index in {repo} (test files and files that do not parse are left out)")
    folder = index_dir(repo)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    (folder / "graph.json").write_text(json.dumps(graph))
    bm25 = bm25s.BM25()
    bm25.index([split_words(nid) for nid in ids], show_progress=False)
    bm25.save(folder / "bm25", show_progress=False)
    nodes = graph["nodes"].values()
    manifest = {
        "repo": str(repo),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": sum(n["type"] == "file" for n in nodes),
        "entities": sum(n["type"] in ("class", "function") for n in nodes),
        "edges": sum(map(len, graph["edges"].values())),
        "skipped": graph["skipped"],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def load_index(repo_path: str) -> CodeIndex:
    folder = index_dir(Path(repo_path).expanduser().resolve())
    if not folder.is_dir():
        raise CodeIndexError("not indexed, run build_index first")
    try:
        manifest = json.loads((folder / "manifest.json").read_text())
        graph = json.loads((folder / "graph.json").read_text())
        bm25 = bm25s.BM25.load(folder / "bm25", show_progress=False)
    except (OSError, ValueError) as e:
        raise CodeIndexError("index damaged, run build_index again") from e
    return CodeIndex(manifest, graph, bm25)


def _check_repo(repo_path: str) -> Path:
    repo = Path(repo_path).expanduser().resolve()
    if not repo.exists():
        raise CodeIndexError(f"repo path does not exist: {repo}")
    if not repo.is_dir():
        raise CodeIndexError(f"repo path is not a folder: {repo}")
    return repo


def _searchable(nodes: dict) -> list[str]:
    return sorted(nid for nid, n in nodes.items() if n["type"] in ENTITY_TYPES and not is_test_file(n["file"]))


class CodeIndex:
    """A loaded index: the graph's nodes, and the three search layers over them."""

    def __init__(self, manifest: dict, graph: dict, bm25: bm25s.BM25):
        self.manifest, self.nodes, self.bm25 = manifest, graph["nodes"], bm25
        self.ids = _searchable(self.nodes)   # in the order the BM25 index was built
        self.searchable = set(self.ids)
        self.by_name: dict[str, list[str]] = {}
        for nid in self.ids:
            self.by_name.setdefault(self.nodes[nid]["name"], []).append(nid)

    def search(self, query: str, entity_type: str = "all", detail: str = "preview") -> dict:
        """The results of the first layer that finds something; `layer` is None when none does.
        `entity_type` (file, class, function or all) filters layer 3."""
        layers = (
            lambda: [query] if query in self.searchable else [],
            lambda: self.by_name.get(query, []),
            lambda: self._bm25(query, entity_type),
        )
        for layer, find in enumerate(layers, 1):
            hits = find()
            if hits:
                return self._result(layer, hits, detail)
        return self._result(None, [], detail)

    def _result(self, layer: int | None, hits: list[str], detail: str) -> dict:
        return {"layer": layer, "built_at": self.manifest["built_at"],
                "results": [self.render(nid, detail) for nid in hits]}

    def render(self, nid: str, detail: str) -> dict:
        """fold: the ID; preview: the def or class line, or a file's first lines; full: the source."""
        code = self.nodes[nid]["code"]
        if detail == "fold":
            return {"id": nid}
        if detail == "full":
            return {"id": nid, "code": code}
        lines = code.splitlines()[:PREVIEW_LINES if self.nodes[nid]["type"] == "file" else 1]
        return {"id": nid, "preview": "\n".join(lines)}

    def _bm25(self, query: str, entity_type: str) -> list[str]:
        tokens = split_words(query)
        if not tokens:
            return []
        docs, scores = self.bm25.retrieve([tokens], k=len(self.ids), show_progress=False)
        hits = (self.ids[d] for d, score in zip(docs[0], scores[0]) if score > 0)
        return [nid for nid in hits if entity_type in ("all", self.nodes[nid]["type"])][:TOP_K]
