from __future__ import annotations

import json

from code_index.graph import build_graph


def targets(graph: dict, kind: str, source: str) -> set[str]:
    return {dst for src, dst in graph["edges"][kind] if src == source}


def test_node_ids(repo):
    """Plan step 1: one node per directory, file, class and function, keyed by
    its qualified name; test files are still nodes (criterion 5)."""
    graph = build_graph(repo)
    nodes = graph["nodes"]
    assert nodes["."]["type"] == nodes["pkg/sub"]["type"] == "directory"
    assert nodes["pkg/utils.py"]["type"] == "file"
    assert nodes["pkg/utils.py:MathUtils"]["type"] == "class"
    method = nodes["pkg/utils.py:MathUtils.double"]
    assert (method["type"], method["name"], method["file"]) == ("function", "double", "pkg/utils.py")
    assert method["code"] == "    def double(self, a):\n        return self.calculate_sum(a, a)"
    assert method["end"] == method["start"] + 1
    decorated = nodes["pkg/utils.py:MathUtils.make"]
    assert decorated["code"] == "    @classmethod\n    def make(cls):\n        return cls.create()"
    assert decorated["end"] == decorated["start"] + 2
    assert nodes["pkg/shapes.py:Shape"]["code"] == "class Shape:\n    pass"   # after a form feed
    assert "tests/test_x.py:only_in_tests" in nodes and "foo_test.py:only_in_foo_test" in nodes
    assert not any(nid.split("/")[0] in (".venv", "venv") for nid in nodes)
    assert "env/settings.py" in nodes   # a package named env, not a virtualenv
    assert json.loads(json.dumps(graph)) == graph


def test_edges(repo):
    """Criterion 6: contains, imports, invokes and inherits edges, for each
    import and call shape of plan step 1; any other shape makes no edge."""
    graph = build_graph(repo)
    assert targets(graph, "contains", ".") == {"main.py", "foo_test.py"}
    assert targets(graph, "contains", "pkg/utils.py") == {
        "pkg/utils.py:calculate_sum", "pkg/utils.py:MathUtils", "pkg/utils.py:Base", "pkg/utils.py:Child"}
    assert targets(graph, "contains", "pkg/utils.py:MathUtils") == {
        f"pkg/utils.py:MathUtils.{m}" for m in ("calculate_sum", "double", "make", "create")}

    assert targets(graph, "imports", "main.py") == {
        "pkg/utils.py",                 # import a.b
        "pkg/shapes.py",                # import a.b as m
        "pkg/sub/__init__.py",          # from a import <package>
        "pkg/utils.py:calculate_sum",   # from a.b import <entity>
        "pkg/sub/tools.py",             # from a.b import <variable>: the file
        "pkg/sub/helpers.py",           # from a.b import *
    }
    assert targets(graph, "imports", "pkg/sub/helpers.py") == {"pkg/sub/tools.py", "pkg/utils.py:MathUtils"}

    calls = {
        "main.py:bare_local": {"main.py:local"},
        "main.py:bare_imported": {"pkg/utils.py:calculate_sum"},
        "main.py:module_attr": {"pkg/shapes.py:Shape"},
        "main.py:no_edges": set(),
        "pkg/utils.py:MathUtils.calculate_sum": {"pkg/utils.py:calculate_sum"},
        "pkg/utils.py:MathUtils.double": {"pkg/utils.py:MathUtils.calculate_sum"},
        "pkg/utils.py:MathUtils.make": {"pkg/utils.py:MathUtils.create"},
        "pkg/utils.py:MathUtils.create": set(),
        "pkg/sub/helpers.py:total": {"pkg/sub/tools.py:calculate_sum"},
    }
    assert {src: targets(graph, "invokes", src) for src in calls} == calls
    assert graph["edges"]["invokes"].count(["main.py:bare_local", "main.py:local"]) == 1

    assert targets(graph, "inherits", "pkg/utils.py:Child") == {"pkg/utils.py:Base"}
    assert targets(graph, "inherits", "pkg/sub/helpers.py:Fancy") == {"pkg/utils.py:MathUtils"}
    assert targets(graph, "inherits", "main.py:Square") == {"pkg/shapes.py:Shape"}


def test_syntax_error_skipped(repo):
    """Criterion 11: a file that fails to parse or decode is skipped with its
    error, and the other files are indexed."""
    (repo / "deep.py").write_text("1+" * 200000 + "1\n")
    (repo / "nested.py").write_text("x = " + "-" * 100000 + "1\n")
    graph = build_graph(repo)
    errors = {s["file"]: s["error"] for s in graph["skipped"]}
    assert errors.keys() == {"broken.py", "binary.py", "deep.py", "nested.py"}
    assert errors["broken.py"].startswith("SyntaxError")
    assert errors["binary.py"].startswith("UnicodeDecodeError")
    assert errors["deep.py"].startswith("RecursionError")
    assert errors["nested.py"].startswith("MemoryError")
    assert "broken.py" not in graph["nodes"] and "main.py" in graph["nodes"]
