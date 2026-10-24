"""Code graph of one Python repo, after LocAgent.

Nodes are directories, files, classes and functions, keyed by ID: the path
from the repo root for a directory (`.` for the root) or a file, and
`file.py:Qualified.name` for a class or function (a method is
`file.py:Class.method`). Each node keeps its type, name, file, start and end
lines, and its source text.

Edges are [source, target] pairs by type: contains, imports, invokes and
inherits. Only the import and call shapes that `_FileEdges` names make an
edge, and one statement or call makes at most one; any other shape
(`obj.method()`, a call on a call result, `getattr`, an import from outside
the repo) makes none. The graph is plain dicts, so it saves as JSON.
"""
from __future__ import annotations

import ast
import os
import posixpath
from pathlib import Path

EDGE_TYPES = ("contains", "imports", "invokes", "inherits")
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def build_graph(repo: Path) -> dict:
    """Parse every `.py` file under `repo`. A file that cannot be read,
    decoded or parsed goes into `skipped` with its error, and the walk goes on."""
    repo = repo.resolve()
    graph = {"nodes": {}, "edges": {t: set() for t in EDGE_TYPES}, "skipped": []}
    trees = {}
    for path in _py_files(repo):
        rel = path.relative_to(repo).as_posix()
        try:
            text = path.read_bytes().decode("utf-8")
            trees[rel] = ast.parse(text, filename=rel)
        except (OSError, SyntaxError, ValueError) as e:
            graph["skipped"].append({"file": rel, "error": f"{type(e).__name__}: {e}"})
            continue
        _add_file(graph, rel, text, trees[rel])
    for rel, tree in trees.items():
        _FileEdges(graph, rel).run(tree)
    graph["edges"] = {t: sorted(map(list, pairs)) for t, pairs in graph["edges"].items()}
    return graph


def _py_files(repo: Path):
    """`.py` files in a stable order; hidden folders such as `.git` or `.venv` are not walked."""
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(root, name)


def _node(kind: str, name: str, file: str, start=None, end=None, code="") -> dict:
    return {"type": kind, "name": name, "file": file, "start": start, "end": end, "code": code}


def _child_id(parent: str, name: str) -> str:
    """`a.py` + `f` -> `a.py:f`; `a.py:C` + `m` -> `a.py:C.m`."""
    return f"{parent}{'.' if ':' in parent else ':'}{name}"


def _add_file(graph: dict, rel: str, text: str, tree: ast.Module) -> None:
    lines = text.splitlines()
    folder = posixpath.dirname(rel) or "."
    graph["nodes"].setdefault(folder, _node("directory", posixpath.basename(folder), folder))
    graph["nodes"][rel] = _node("file", posixpath.basename(rel), rel, 1, len(lines), text)
    graph["edges"]["contains"].add((folder, rel))
    _add_defs(graph, rel, tree.body, lines)


def _add_defs(graph: dict, parent: str, body: list[ast.stmt], lines: list[str]) -> None:
    """Classes and functions defined directly in `body`, and the members of each class."""
    file = graph["nodes"][parent]["file"]
    for stmt in body:
        if not isinstance(stmt, DEFS):
            continue
        nid = _child_id(parent, stmt.name)
        kind = "class" if isinstance(stmt, ast.ClassDef) else "function"
        code = "\n".join(lines[stmt.lineno - 1:stmt.end_lineno])
        graph["nodes"][nid] = _node(kind, stmt.name, file, stmt.lineno, stmt.end_lineno, code)
        graph["edges"]["contains"].add((parent, nid))
        if kind == "class":
            _add_defs(graph, nid, stmt.body, lines)


class _FileEdges:
    """The imports, invokes and inherits edges of one file.

    A module `a.b.c` is `a/b/c.py` or `a/b/c/__init__.py` under the repo root.
    `import a.b` links to the module; `from a.b import x` links to the module
    `a/b/x`, else the top-level entity `x` of `a.b`, else the file of `a.b`;
    a relative import starts from the importing file's folder; `import *`
    links to the module only. A call or a base class links to a top-level
    entity of this file, else to an entity bound by `from ... import`;
    `self.m()` / `cls.m()` in a method link to `m` of its class; `mod.f`
    links to the top-level `f` of the module that `mod` is bound to.
    """

    def __init__(self, graph: dict, file: str):
        self.nodes, self.edges, self.file = graph["nodes"], graph["edges"], file
        self.bound: dict[str, str] = {}   # local name -> node ID that an import bound it to

    def run(self, tree: ast.Module) -> None:
        for stmt in ast.walk(tree):
            if isinstance(stmt, ast.Import):
                self._import(stmt)
            elif isinstance(stmt, ast.ImportFrom):
                self._import_from(stmt)
        self._defs(self.file, tree.body)

    def _import(self, stmt: ast.Import) -> None:
        for alias in stmt.names:
            parts = alias.name.split(".")
            target = self._module(parts)
            self._link("imports", self.file, target)
            # `import a.b` binds `a`; `import a.b as m` binds `m` to `a.b`.
            bound = target if alias.asname else self._module(parts[:1])
            if bound:
                self.bound[alias.asname or parts[0]] = bound

    def _import_from(self, stmt: ast.ImportFrom) -> None:
        base = self._base(stmt)
        if base is None:
            return
        for alias in stmt.names:
            target, binds = self._from_target(base, alias.name)
            self._link("imports", self.file, target)
            if binds:
                self.bound[alias.asname or alias.name] = target

    def _base(self, stmt: ast.ImportFrom) -> list[str] | None:
        """The module of `from <module> import ...` as path parts, or None above the repo root."""
        parts = stmt.module.split(".") if stmt.module else []
        if not stmt.level:
            return parts
        folder = self.file.split("/")[:-1]
        if stmt.level - 1 > len(folder):
            return None
        return folder[:len(folder) - stmt.level + 1] + parts

    def _from_target(self, base: list[str], name: str) -> tuple[str | None, bool]:
        """The edge target of `from base import name`, and whether `name` is bound to it."""
        module = self._module(base)
        if name == "*":
            return module, False
        submodule = self._module(base + [name])
        if submodule:
            return submodule, True
        if module and _child_id(module, name) in self.nodes:
            return _child_id(module, name), True
        return module, False

    def _module(self, parts: list[str]) -> str | None:
        path = "/".join(parts)
        files = (f"{path}.py", f"{path}/__init__.py") if path else ("__init__.py",)
        return next((f for f in files if f in self.nodes), None)

    def _defs(self, parent: str, body: list[ast.stmt]) -> None:
        cls = parent if ":" in parent else None
        for stmt in body:
            if not isinstance(stmt, DEFS):
                continue
            nid = _child_id(parent, stmt.name)
            if isinstance(stmt, ast.ClassDef):
                for base in stmt.bases:
                    self._link("inherits", nid, self._resolve(base, None))
                self._defs(nid, stmt.body)
                continue
            for part in stmt.body:
                for node in ast.walk(part):
                    if isinstance(node, ast.Call):
                        self._link("invokes", nid, self._resolve(node.func, cls))

    def _resolve(self, expr: ast.expr, cls: str | None) -> str | None:
        """The entity that `f`, `self.m`, `cls.m` or `mod.f` names, if it is in the graph."""
        if isinstance(expr, ast.Name):
            local = _child_id(self.file, expr.id)
            if local in self.nodes:
                return local
            bound = self.bound.get(expr.id)
            return bound if bound and self.nodes[bound]["type"] != "file" else None
        if not (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name)):
            return None
        owner, bound = expr.value.id, self.bound.get(expr.value.id)
        if cls and owner in ("self", "cls"):
            target = _child_id(cls, expr.attr)
        elif bound and self.nodes[bound]["type"] == "file":
            target = _child_id(bound, expr.attr)
        else:
            return None
        return target if target in self.nodes else None

    def _link(self, kind: str, source: str, target: str | None) -> None:
        if target:
            self.edges[kind].add((source, target))
