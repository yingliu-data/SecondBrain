"""Code graph of one Python file, modeled on LocAgent's dependency graph (its node and edge
types; the code is this package's own).

Nodes are directories, files, classes and functions, keyed by logical ID:
`<source>/<path>` for a directory (`<source>` for the indexed root) or a file,
and `<source>/<path>:Qualified.name` for a class or function (a method is
`<source>/file.py:Class.method`). Each node holds the fields of the shared schema
(`store.node`): its type, title, path, heading path, the names layer 2
matches, start and end lines, and its source text.

Edges are (type, source, target) triples: contains, imports, invokes and
inherits. Only the import and call shapes that `_FileEdges` names make an
edge, and one statement or call makes at most one; any other shape
(`obj.method()`, a call on a call result, `getattr`, an import from outside
the indexed folder) makes none. A target in another file is checked through
`lookup(nid)`, which the indexer answers from the store.
"""
from __future__ import annotations

import ast
import io
import posixpath
import re
import tokenize
from collections.abc import Callable

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
BLOCKS = (ast.If, ast.Try, ast.TryStar, ast.ExceptHandler, ast.With, ast.AsyncWith)
PARSE_ERRORS = (SyntaxError, ValueError, RecursionError, MemoryError)

Lookup = Callable[[str], str | None]


def nid_of(source: str, rel: str) -> str:
    """`code`, `pkg/utils.py` -> `code/pkg/utils.py`; the root folder `.` -> `code`."""
    return posixpath.normpath(f"{source}/{rel}")


def is_test_file(path: str) -> bool:
    """A file under `test/` or `tests/`, or named `test_*.py` or `*_test.py`."""
    *folders, name = path.split("/")
    return bool({"test", "tests"} & set(folders)) or name.startswith("test_") or name.endswith("_test.py")


class CodeGraph:
    """The nodes of one file, parsed from its bytes, and its edges once
    `resolve_edges` has a lookup for the other files. A file that cannot be
    decoded or parsed raises one of PARSE_ERRORS."""

    chunked = ("class", "function")   # the node types that get a chunk for Atlas to embed

    def __init__(self, source: str, rel: str, data: bytes):
        self.source, self.rel = source, rel
        self.nodes: dict[str, dict] = {}
        self.edges: set[tuple[str, str, str]] = set()
        text = data.decode(tokenize.detect_encoding(io.BytesIO(data).readline)[0])
        self.tree = ast.parse(text, filename=rel)
        _add_file(self, rel, text)

    def resolve_edges(self, lookup: Lookup) -> None:
        """Add the imports, invokes and inherits edges. `lookup(nid)` is the type of
        the latest node with that ID, or None; this file's own nodes are checked first."""
        def found(nid: str) -> str | None:
            return self.nodes[nid]["type"] if nid in self.nodes else lookup(nid)
        _FileEdges(self, found).run(self.tree)


def _node(kind: str, title: str, path: str, qualified: str | None = None,
          start=None, end=None, text="") -> dict:
    """The fields of `store.node` for one node. `names` are lower-cased for layer 2;
    a test file's entities get none, so the name layers skip them."""
    if qualified is None:
        names = [title.lower()] if kind == "file" else []
    else:
        names = [title.lower(), qualified.lower()]
    return {"type": kind, "title": title, "path": path,
            "heading_path": qualified.split(".") if qualified else [],
            "names": [] if is_test_file(path) else names,
            "start_line": start, "end_line": end, "text": text}


def _child_id(parent: str, name: str) -> str:
    """`a.py` + `f` -> `a.py:f`; `a.py:C` + `m` -> `a.py:C.m`."""
    return f"{parent}{'.' if ':' in parent else ':'}{name}"


def split_lines(text: str) -> list[str]:
    """The file's lines, at the line breaks that ast counts (str.splitlines also splits on
    \\f, \\x85 and others), without the empty one after a final break."""
    lines = re.split(r"\r\n|\r|\n", text)
    if not lines[-1]:
        lines.pop()
    return lines


def add_folder(graph, rel: str, child: str) -> None:
    """The directory node of the file at `rel` and its `contains` edge to the file's node `child`."""
    folder = posixpath.dirname(rel) or "."
    parent = nid_of(graph.source, folder)
    graph.nodes[parent] = _node("directory", posixpath.basename(folder), folder)
    graph.edges.add(("contains", parent, child))


def _add_file(graph: CodeGraph, rel: str, text: str) -> None:
    lines = split_lines(text)
    file = nid_of(graph.source, rel)
    add_folder(graph, rel, file)
    graph.nodes[file] = _node("file", posixpath.basename(rel), rel, None, 1, len(lines))
    _add_defs(graph, file, graph.tree.body, lines)
    graph.nodes[file]["text"] = _own_text(graph, file, lines)


def _own_text(graph: CodeGraph, file: str, lines: list[str]) -> str:
    """The module-level lines of a file: those outside its top-level classes and functions,
    so that an edit inside a function does not make a new version of the file node."""
    covered: set[int] = set()
    for kind, src, dst in graph.edges:
        if src == file:
            covered.update(range(graph.nodes[dst]["start_line"], graph.nodes[dst]["end_line"] + 1))
    return "\n".join(line for number, line in enumerate(lines, 1) if number not in covered)


def _defs_in(body):
    """The classes and functions in `body`, also those under an if, try or with block."""
    for stmt in body:
        if isinstance(stmt, DEFS):
            yield stmt
        elif isinstance(stmt, BLOCKS):
            yield from _defs_in(ast.iter_child_nodes(stmt))


def _add_defs(graph: CodeGraph, parent: str, body: list[ast.stmt], lines: list[str]) -> None:
    """Classes and functions defined in `body`, and the members of each class. A repeat
    of a name, such as a property setter, widens the node to hold every definition."""
    for stmt in _defs_in(body):
        nid = _child_id(parent, stmt.name)
        kind = "class" if isinstance(stmt, ast.ClassDef) else "function"
        start = min([stmt.lineno, *(d.lineno for d in stmt.decorator_list)])
        end = stmt.end_lineno
        if old := graph.nodes.get(nid):
            start, end = min(start, old["start_line"]), max(end, old["end_line"])
        text = "\n".join(lines[start - 1:end])
        graph.nodes[nid] = _node(kind, stmt.name, graph.rel, nid.split(":", 1)[1], start, end, text)
        graph.edges.add(("contains", parent, nid))
        if kind == "class":
            _add_defs(graph, nid, stmt.body, lines)


class _FileEdges:
    """The imports, invokes and inherits edges of one file.

    A module `a.b.c` is `a/b/c.py` or `a/b/c/__init__.py` under the indexed root.
    `import a.b` links to the module; `from a.b import x` links to the module
    `a/b/x`, else the top-level entity `x` of `a.b`, else the file of `a.b`;
    a relative import starts from the importing file's folder; `import *`
    links to the module only. A call or a base class links to a top-level
    entity of this file, else to an entity bound by `from ... import`;
    `self.m()` / `cls.m()` in a method link to `m` of its class; `mod.f`
    links to the top-level `f` of the module that `mod` is bound to.
    """

    def __init__(self, graph: CodeGraph, lookup: Lookup):
        self.edges, self.source, self.rel, self.lookup = graph.edges, graph.source, graph.rel, lookup
        self.file = nid_of(graph.source, graph.rel)
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
        """The module of `from <module> import ...` as path parts, or None above the indexed root."""
        parts = stmt.module.split(".") if stmt.module else []
        if not stmt.level:
            return parts
        folder = self.rel.split("/")[:-1]
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
        if module and self.lookup(_child_id(module, name)):
            return _child_id(module, name), True
        return module, False

    def _module(self, parts: list[str]) -> str | None:
        path = "/".join(parts)
        files = (f"{path}.py", f"{path}/__init__.py") if path else ("__init__.py",)
        return next((nid_of(self.source, f) for f in files if self.lookup(nid_of(self.source, f))), None)

    def _defs(self, parent: str, body: list[ast.stmt]) -> None:
        cls = parent if ":" in parent else None
        for stmt in _defs_in(body):
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
            if self.lookup(local):
                return local
            bound = self.bound.get(expr.id)
            return bound if bound and self.lookup(bound) != "file" else None
        if not (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name)):
            return None
        owner, bound = expr.value.id, self.bound.get(expr.value.id)
        if cls and owner in ("self", "cls"):
            target = _child_id(cls, expr.attr)
        elif bound and self.lookup(bound) == "file":
            target = _child_id(bound, expr.attr)
        else:
            return None
        return target if self.lookup(target) else None

    def _link(self, kind: str, source: str, target: str | None) -> None:
        if target:
            self.edges.add((kind, source, target))
