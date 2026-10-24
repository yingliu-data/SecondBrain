"""The Markdown parser on its own, and Markdown files indexed beside Python ones."""
from __future__ import annotations

import textwrap

import pytest

from ast_everything.indexer import sync
from ast_everything.parsers import PARSE_ERRORS, iter_files
from ast_everything.parsers.markdown_tree import MarkdownTree, slug
from ast_everything.search import retrieve, search, sources, traverse

GUIDE = textwrap.dedent("""\
    ---
    title: Guide
    ---
    Read [the notes](notes.md) first.

    # Setup

    Install it:

    - one
    - two, see [usage](#usage)
      - nested

    ```bash
    uv sync
    ```

    ## Usage

    | a | b |
    |---|---|
    | 1 | 2 |

    > quoted

    # Setup

    <div>raw</div>

    ***
    """)
DOC = "notes/docs/guide.md"


def parse(text: str = GUIDE, rel: str = "docs/guide.md", lookup=lambda nid: None) -> MarkdownTree:
    graph = MarkdownTree("notes", rel, text.encode())
    graph.resolve_edges(lookup)
    return graph


def span(graph, nid):
    node = graph.nodes[nid]
    return node["type"], node["start_line"], node["end_line"]


def test_nodes_are_document_sections_and_blocks():
    """A document, a section per heading nested by level, and its blocks by kind and position,
    each with its own lines; a thematic break makes no node."""
    graph = parse()
    assert set(graph.nodes) == {
        "notes/docs", DOC, f"{DOC}:p1",
        f"{DOC}#setup", f"{DOC}#setup:p1", f"{DOC}#setup:list1", f"{DOC}#setup:list1.item1",
        f"{DOC}#setup:list1.item2", f"{DOC}#setup:code1",
        f"{DOC}#setup/usage", f"{DOC}#setup/usage:table1", f"{DOC}#setup/usage:quote1",
        f"{DOC}#setup-1", f"{DOC}#setup-1:html1",
    }
    assert span(graph, f"{DOC}#setup") == ("section", 6, 16)          # up to the next heading
    assert span(graph, f"{DOC}#setup/usage") == ("section", 18, 24)
    assert span(graph, f"{DOC}#setup:list1") == ("list", 10, 12)
    assert span(graph, f"{DOC}#setup:list1.item2") == ("list_item", 11, 12)   # with its nested list
    assert span(graph, f"{DOC}#setup/usage:table1") == ("table", 20, 22)
    code = graph.nodes[f"{DOC}#setup:code1"]
    assert (code["title"], code["text"]) == ("bash", "```bash\nuv sync\n```")
    usage = graph.nodes[f"{DOC}#setup/usage"]
    assert (usage["heading_path"], usage["names"]) == (["Setup", "Usage"], ["usage"])
    assert graph.nodes[f"{DOC}#setup:p1"]["names"] == [] and graph.nodes[DOC]["names"] == ["guide.md"]


def test_a_section_holds_its_own_text_only():
    """The heading and its blocks up to the next heading, so an edit in a subsection leaves the
    section above it as it was; the document holds the front matter and the preamble."""
    graph = parse()
    assert graph.nodes[f"{DOC}#setup"]["text"].endswith("```bash\nuv sync\n```")
    assert "Usage" not in graph.nodes[f"{DOC}#setup"]["text"]
    assert graph.nodes[DOC]["text"] == "---\ntitle: Guide\n---\nRead [the notes](notes.md) first."
    assert not any(n["type"] == "paragraph" and "title:" in n["text"] for n in graph.nodes.values())


def test_contains_edges_follow_the_tree():
    graph = parse()
    contains = {(src, dst) for kind, src, dst in graph.edges if kind == "contains"}
    assert {("notes/docs", DOC), (DOC, f"{DOC}:p1"), (DOC, f"{DOC}#setup"), (DOC, f"{DOC}#setup-1"),
            (f"{DOC}#setup", f"{DOC}#setup/usage"), (f"{DOC}#setup", f"{DOC}#setup:list1"),
            (f"{DOC}#setup:list1", f"{DOC}#setup:list1.item2")} <= contains
    assert (DOC, f"{DOC}#setup/usage") not in contains


def test_links_to_an_anchor_and_to_an_indexed_file():
    """A same-file anchor links to its section (a repeated heading's anchor gets `-1`); a
    relative link to another file links only when that file is known; outside links make none."""
    graph = parse(lookup=lambda nid: "document" if nid == "notes/docs/notes.md" else None)
    links = {(src, dst) for kind, src, dst in graph.edges if kind == "links_to"}
    assert links == {(f"{DOC}#setup:list1.item2", f"{DOC}#setup/usage"), (f"{DOC}:p1", "notes/docs/notes.md")}
    assert graph.anchors["setup-1"] == f"{DOC}#setup-1"
    assert not any(kind == "links_to" for kind, *_ in parse().edges if _[1] == "notes/docs/notes.md")
    outside = parse("[a](https://example.com) [b](../../up.md) [c](/top.md)\n", lookup=lambda nid: "document")
    assert {dst for kind, _, dst in outside.edges if kind == "links_to"} == {"notes/top.md"}


def test_slug_is_the_github_anchor():
    assert slug("Hello, World! (v2)") == "hello-world-v2"
    assert slug("???") == "section"


def test_no_heading_keeps_the_whole_file_in_the_document():
    graph = parse("just text\n\nmore\n", rel="a.md")
    assert graph.nodes["notes/a.md"]["text"] == "just text\n\nmore"
    assert {n["type"] for n in graph.nodes.values()} == {"directory", "document", "paragraph"}


def test_text_that_is_not_utf8_is_a_parse_error():
    with pytest.raises(PARSE_ERRORS):
        MarkdownTree("notes", "bad.md", b"\xff\xfe# x\n")


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text(GUIDE)
    (root / "README.markdown").write_text("# Readme\n\nSee [guide](docs/guide.md) and [code](app.py).\n")
    (root / "app.py").write_text("def main():\n    return 1\n")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "skip.md").write_text("# skipped\n")
    (root / "notes.txt").write_text("not parsed\n")
    return root


def test_walk_finds_python_and_markdown_files(folder):
    assert [p.relative_to(folder).as_posix() for p in iter_files(folder)] == ["README.markdown", "app.py", "docs/guide.md"]


def test_sync_indexes_markdown_and_python_together(store, folder):
    counts = sync(store, "proj", folder)
    assert (counts["files"], counts["skipped"]) == (3, 0)
    readme = "proj/README.markdown"
    links = {e["dst"] for e in store.edges.find({"type": "links_to", "src": f"{readme}#readme:p1", "active": True})}
    assert links == {"proj/docs/guide.md", "proj/app.py"}
    chunked = {c["nid"] for c in store.chunks.find()}
    assert {"proj/docs/guide.md", "proj/docs/guide.md#setup", "proj/app.py:main"} <= chunked
    assert not any("#setup:p1" in nid or nid.endswith(":code1") for nid in chunked)   # blocks are not embedded
    assert sources(store) == [{"source": "proj", "files": 3, "skipped": 0}]


def test_an_edited_section_gets_a_new_version_and_its_parent_does_not(store, folder):
    sync(store, "proj", folder)
    path = folder / "docs" / "guide.md"
    path.write_text(GUIDE.replace("> quoted", "> quoted again"))
    sync(store, "proj", folder)
    usage = store.nodes.find_one({"nid": "proj/docs/guide.md#setup/usage", "is_latest": True})
    assert usage["version"] == 2
    assert store.nodes.find_one({"_id": "proj/docs/guide.md#setup/usage@v1"})["superseded_by"] == usage["_id"]
    assert store.edges.find_one({"type": "supersedes", "src": usage["_id"]})["source"] == "proj"
    assert store.nodes.find_one({"nid": "proj/docs/guide.md#setup", "is_latest": True})["version"] == 1


def test_a_removed_section_is_marked_removed(store, folder):
    sync(store, "proj", folder)
    path = folder / "docs" / "guide.md"
    path.write_text(GUIDE.split("# Setup\n\n<div>")[0])
    sync(store, "proj", folder)
    gone = store.nodes.find_one({"nid": "proj/docs/guide.md#setup-1"})
    assert gone["removed_at"] is not None and not gone["is_latest"]


def test_a_dangling_link_links_when_its_document_next_changes(store, folder):
    """A link to a file that is not indexed yet makes no edge. As with a Python import, a file
    linked before its target existed gets the edge when it next changes."""
    (folder / "README.markdown").write_text("# Readme\n\nSee [later](later.md).\n")
    sync(store, "proj", folder)
    assert store.edges.count_documents({"type": "links_to", "src_path": "README.markdown"}) == 0
    (folder / "later.md").write_text("# Later\n")
    sync(store, "proj", folder)
    (folder / "README.markdown").write_text("# Readme\n\nSee [later](later.md) now.\n")
    sync(store, "proj", folder)
    assert store.edges.find_one({"type": "links_to", "dst": "proj/later.md", "active": True})


def test_two_sources_stay_apart(store, folder, tmp_path):
    other = tmp_path / "other"
    (other / "docs").mkdir(parents=True)
    (other / "docs" / "guide.md").write_text("# Other\n")
    sync(store, "proj", folder)
    sync(store, "other", other)
    sync(store, "proj", folder)          # an unchanged re-sync touches neither
    assert store.nodes.find_one({"nid": "proj/docs/guide.md", "is_latest": True})["version"] == 1
    assert store.nodes.find_one({"nid": "other/docs/guide.md#other", "is_latest": True})
    assert store.edges.count_documents({"source": "other", "src_path": "docs/guide.md", "active": True}) == 2
    assert store.edges.find_one({"source": "proj", "src_path": "docs/guide.md", "dst": "other/docs/guide.md"}) is None
    assert [s["source"] for s in sources(store)] == ["other", "proj"]


def test_search_finds_a_section_by_heading_and_traverses_into_its_blocks(store, folder):
    sync(store, "proj", folder)
    found = search(store, "usage", scope="proj")
    assert found["layer"] == 2 and found["results"][0]["id"] == "proj/docs/guide.md#setup/usage@v1"
    walked = traverse(store, ["proj/docs/guide.md#setup/usage"], hops=1)
    assert {n["id"] for n in walked["nodes"]} >= {"proj/docs/guide.md#setup/usage:table1@v1",
                                                   "proj/docs/guide.md#setup/usage:quote1@v1"}
    [table] = retrieve(store, ["proj/docs/guide.md#setup/usage:table1"])
    assert (table["lines"], table["text"]) == ([20, 22], "| a | b |\n|---|---|\n| 1 | 2 |")
