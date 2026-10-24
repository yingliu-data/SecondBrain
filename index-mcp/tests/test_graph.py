from __future__ import annotations

import json
from pathlib import Path

from index_mcp.graph import PARSE_ERRORS, FileGraph, _py_files


def build(repo: Path) -> tuple[dict, set, dict]:
    """Every file of `repo` as one graph, the way the indexer does it: the nodes of every
    file first, then each file's edges with a lookup over all of them. Returns the nodes
    by ID, the (type, source, target) edges, and the parse error of each skipped file."""
    graphs, skipped = {}, {}
    for path in _py_files(repo):
        rel = path.relative_to(repo).as_posix()
        try:
            graphs[rel] = FileGraph(rel, path.read_bytes())
        except PARSE_ERRORS as e:
            skipped[rel] = f"{type(e).__name__}: {e}"
    nodes = {nid: fields for graph in graphs.values() for nid, fields in graph.nodes.items()}
    edges = set()
    for graph in graphs.values():
        graph.resolve_edges(lambda nid: nodes[nid]["type"] if nid in nodes else None)
        edges |= graph.edges
    return nodes, edges, skipped


def targets(edges: set, kind: str, source: str) -> set[str]:
    return {dst for t, src, dst in edges if t == kind and src == source}


def test_node_ids(repo):
    """One node per directory, file, class and function, keyed by `code/<path>[:<qualified name>]`
    in the shared schema; test files are nodes too, with no names for the name layers."""
    nodes, _, _ = build(repo)
    assert nodes["code"]["type"] == nodes["code/pkg/sub"]["type"] == "directory"
    assert nodes["code/pkg/utils.py"]["type"] == "file"
    assert nodes["code/pkg/utils.py:MathUtils"]["type"] == "class"
    method = nodes["code/pkg/utils.py:MathUtils.double"]
    assert (method["type"], method["title"], method["path"]) == ("function", "double", "pkg/utils.py")
    assert (method["heading_path"], method["names"]) == (["MathUtils", "double"], ["double", "mathutils.double"])
    assert method["text"] == "    def double(self, a):\n        return self.calculate_sum(a, a)"
    assert method["end_line"] == method["start_line"] + 1
    decorated = nodes["code/pkg/utils.py:MathUtils.make"]
    assert decorated["text"] == "    @classmethod\n    def make(cls):\n        return cls.create()"
    assert decorated["end_line"] == decorated["start_line"] + 2
    assert nodes["code/pkg/shapes.py:Shape"]["text"] == "class Shape:\n    pass"   # after a form feed
    assert "code/tests/test_x.py:only_in_tests" in nodes and "code/foo_test.py:only_in_foo_test" in nodes
    assert nodes["code/tests/test_x.py:only_in_tests"]["names"] == nodes["code/tests/test_x.py"]["names"] == []
    assert not any(nid.startswith(("code/.venv", "code/venv")) for nid in nodes)
    assert "code/env/settings.py" in nodes   # a package named env, not a virtualenv
    assert json.loads(json.dumps(nodes)) == nodes


def test_file_node_holds_its_module_level_text(repo):
    """A file node's text is the code outside its top-level definitions, so an edit
    inside a function changes that function's node only."""
    nodes, _, _ = build(repo)
    utils = nodes["code/pkg/utils.py"]
    assert (utils["names"], utils["start_line"], utils["end_line"]) == (["utils.py"], 1, 29)
    assert utils["text"] == "CONSTANT = 1" + "\n" * 8
    assert nodes["code/pkg/sub/tools.py"]["text"] == "LIMIT = 10\n\n"
    assert nodes["code"]["text"] == "" and nodes["code"]["start_line"] is None
    assert FileGraph("pkg/Config.py", b"X = 1\n").nodes["code/pkg/Config.py"]["names"] == ["config.py"]


def test_edges(repo):
    """contains, imports, invokes and inherits edges, for each import and call
    shape the module docstring names; any other shape makes no edge."""
    _, edges, _ = build(repo)
    assert targets(edges, "contains", "code") == {"code/main.py", "code/foo_test.py"}
    assert targets(edges, "contains", "code/pkg/utils.py") == {
        "code/pkg/utils.py:calculate_sum", "code/pkg/utils.py:MathUtils", "code/pkg/utils.py:Base",
        "code/pkg/utils.py:Child"}
    assert targets(edges, "contains", "code/pkg/utils.py:MathUtils") == {
        f"code/pkg/utils.py:MathUtils.{m}" for m in ("calculate_sum", "double", "make", "create")}

    assert targets(edges, "imports", "code/main.py") == {
        "code/pkg/utils.py",                 # import a.b
        "code/pkg/shapes.py",                # import a.b as m
        "code/pkg/sub/__init__.py",          # from a import <package>
        "code/pkg/utils.py:calculate_sum",   # from a.b import <entity>
        "code/pkg/sub/tools.py",             # from a.b import <variable>: the file
        "code/pkg/sub/helpers.py",           # from a.b import *
    }
    assert targets(edges, "imports", "code/pkg/sub/helpers.py") == {"code/pkg/sub/tools.py", "code/pkg/utils.py:MathUtils"}

    calls = {
        "code/main.py:bare_local": {"code/main.py:local"},
        "code/main.py:bare_imported": {"code/pkg/utils.py:calculate_sum"},
        "code/main.py:module_attr": {"code/pkg/shapes.py:Shape"},
        "code/main.py:no_edges": set(),
        "code/pkg/utils.py:MathUtils.calculate_sum": {"code/pkg/utils.py:calculate_sum"},
        "code/pkg/utils.py:MathUtils.double": {"code/pkg/utils.py:MathUtils.calculate_sum"},
        "code/pkg/utils.py:MathUtils.make": {"code/pkg/utils.py:MathUtils.create"},
        "code/pkg/utils.py:MathUtils.create": set(),
        "code/pkg/sub/helpers.py:total": {"code/pkg/sub/tools.py:calculate_sum"},
    }
    assert {src: targets(edges, "invokes", src) for src in calls} == calls

    assert targets(edges, "inherits", "code/pkg/utils.py:Child") == {"code/pkg/utils.py:Base"}
    assert targets(edges, "inherits", "code/pkg/sub/helpers.py:Fancy") == {"code/pkg/utils.py:MathUtils"}
    assert targets(edges, "inherits", "code/main.py:Square") == {"code/pkg/shapes.py:Shape"}


def test_syntax_error_skipped(repo):
    """A file that fails to parse or decode raises its error for the indexer to record,
    and the other files are indexed."""
    (repo / "deep.py").write_text("1+" * 200000 + "1\n")
    (repo / "nested.py").write_text("x = " + "-" * 100000 + "1\n")
    nodes, _, errors = build(repo)
    assert errors.keys() == {"broken.py", "binary.py", "deep.py", "nested.py"}
    assert errors["broken.py"].startswith("SyntaxError")
    assert errors["binary.py"].startswith("SyntaxError: invalid or missing encoding")
    assert errors["deep.py"].startswith("RecursionError")
    assert errors["nested.py"].startswith("MemoryError")
    assert "code/broken.py" not in nodes and "code/main.py" in nodes


def test_bom_and_coding_cookie(repo):
    """A file with a UTF-8 BOM or a latin-1 coding cookie is parsed, not skipped."""
    (repo / "bom.py").write_bytes(b"\xef\xbb\xbfdef with_bom():\n    pass\n")
    (repo / "latin.py").write_bytes(b"# -*- coding: latin-1 -*-\ndef with_cookie():\n    return '\xe9'\n")
    nodes, _, skipped = build(repo)
    assert {"bom.py", "latin.py"}.isdisjoint(skipped)
    assert nodes["code/bom.py:with_bom"]["text"] == "def with_bom():\n    pass"
    assert nodes["code/latin.py:with_cookie"]["text"] == "def with_cookie():\n    return '\xe9'"


def test_defs_under_if_try_with(repo):
    """Definitions under a module-level or class-level if, try or with get a node and edges."""
    (repo / "guarded.py").write_text(
        "if True:\n    def in_if():\n        pass\nelse:\n    def in_else():\n        pass\n"
        "try:\n    from pkg.utils import Base\nexcept ImportError:\n    class Base:\n        pass\n"
        "with open('x') as f:\n    class InWith(Base):\n        try:\n            def method(self):\n"
        "                in_if()\n        finally:\n            pass\n")
    nodes, edges, _ = build(repo)
    assert {"in_if", "in_else", "Base", "InWith", "InWith.method"} <= {
        nid.split(":")[1] for nid in nodes if nid.startswith("code/guarded.py:")}
    assert targets(edges, "inherits", "code/guarded.py:InWith") == {"code/guarded.py:Base"}
    assert targets(edges, "invokes", "code/guarded.py:InWith.method") == {"code/guarded.py:in_if"}


def test_repeated_name_keeps_every_definition(repo):
    """A property setter after its getter widens the one node to hold both."""
    (repo / "prop.py").write_text(
        "class P:\n    @property\n    def x(self):\n        return 1\n\n"
        "    @x.setter\n    def x(self, value):\n        pass\n")
    node = build(repo)[0]["code/prop.py:P.x"]
    assert (node["start_line"], node["end_line"]) == (2, 8)
    assert node["text"].startswith("    @property") and node["text"].endswith("        pass")
