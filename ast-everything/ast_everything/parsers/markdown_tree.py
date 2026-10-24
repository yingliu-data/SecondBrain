"""The block tree of one Markdown file, parsed with markdown-it (CommonMark and tables).

Nodes, keyed by logical ID:
- the folder, `directory`, as the Python parser makes it, and the file,
  `document` (`<source>/<path>`), whose text is the front matter and the lines
  before the first heading (the whole file when it has no heading);
- `section`, one per heading, nested by level: `<source>/<path>#intro/setup`,
  the slugs of the heading path (a repeated slug gets `-1`, `-2`, as GitHub
  numbers anchors). A section's text and lines are its own: the heading and
  the blocks up to the next heading, so an edit in a subsection does not make
  a new version of the section above it;
- the blocks of a section or of the document, by kind and position:
  `paragraph` (`:p1`), `list` (`:list1`) and its `list_item`s (`:list1.item2`),
  `table`, `code` (a fence or an indented block; a fence's info string is the
  title), `quote` and `html`. A thematic break makes no node.

`names` (what search layer 2 matches) are the file name of a document and the
heading of a section; blocks have none and are reached by ID or by traversal.

Edges: `contains` from the folder to the document, the document to its
top-level sections and blocks, a section to its subsections and blocks, and a
list to its items. `links_to`, added by `resolve_edges`, from a block to the
indexed file a relative link names (`#anchor` in the same file: the section
with that slug), when `lookup` knows it.
"""
from __future__ import annotations

import posixpath
import re
from collections import Counter
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from ast_everything.parsers.code_graph import Lookup, nid_of

BLOCKS = {
    "paragraph_open": ("paragraph", "p"),
    "bullet_list_open": ("list", "list"),
    "ordered_list_open": ("list", "list"),
    "table_open": ("table", "table"),
    "blockquote_open": ("quote", "quote"),
    "fence": ("code", "code"),
    "code_block": ("code", "code"),
    "html_block": ("html", "html"),
}
FRONT_MATTER_ENDS = ("---", "...")

_md = MarkdownIt("commonmark").enable("table")


def slug(title: str) -> str:
    """A heading's anchor as GitHub makes it: lower-cased, punctuation dropped, spaces as `-`."""
    text = re.sub(r"[^\w\- ]", "", title.strip().lower())
    return text.replace(" ", "-") or "section"


class MarkdownTree:
    """The nodes of one Markdown file, parsed from its bytes, and its `links_to` edges
    once `resolve_edges` has a lookup for the other files. A file that is not UTF-8
    raises UnicodeDecodeError, one of PARSE_ERRORS."""

    chunked = ("document", "section")

    def __init__(self, source: str, rel: str, data: bytes):
        self.source, self.rel = source, rel
        self.nodes: dict[str, dict] = {}
        self.edges: set[tuple[str, str, str]] = set()
        self.links: list[tuple[str, str]] = []      # (block ID, href), resolved by resolve_edges
        self.anchors: dict[str, str] = {}           # a heading's flat slug -> its section ID
        _Builder(self, data.decode("utf-8-sig")).run()

    def resolve_edges(self, lookup: Lookup) -> None:
        for block, href in self.links:
            target = self._target(href, lookup)
            if target:
                self.edges.add(("links_to", block, target))

    def _target(self, href: str, lookup: Lookup) -> str | None:
        """The node a relative link names: a section of this file by its anchor, or an indexed file."""
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            return None
        path = unquote(parts.path)
        if not path:
            return self.anchors.get(parts.fragment.lower())
        rel = path.lstrip("/") if path.startswith("/") else posixpath.join(posixpath.dirname(self.rel), path)
        rel = posixpath.normpath(rel)
        if rel == ".." or rel.startswith("../"):
            return None
        target = nid_of(self.source, rel)
        if rel == self.rel:
            return self.anchors.get(parts.fragment.lower(), target)
        return target if lookup(target) else None


class _Builder:
    """One pass over markdown-it's block tokens: sections from the headings, a node for
    each top-level block, and the items of each top-level list."""

    def __init__(self, graph: MarkdownTree, text: str):
        self.graph = graph
        lines = re.split(r"\r\n|\r|\n", text)
        if lines and not lines[-1]:
            lines.pop()
        self.lines = lines
        self.front = _front_matter_end(lines)
        self.doc = nid_of(graph.source, graph.rel)
        self.stack: list[tuple[int, str, list[str]]] = []   # (level, section ID, heading path)
        self.counts: dict[str, Counter] = {}
        self.sections: list[tuple[str, int]] = []            # (section ID, heading line), in order

    def run(self) -> None:
        graph, rel = self.graph, self.graph.rel
        folder = posixpath.dirname(rel) or "."
        parent = nid_of(graph.source, folder)
        graph.nodes[parent] = _node("directory", posixpath.basename(folder), folder)
        name = posixpath.basename(rel)
        graph.nodes[self.doc] = _node("document", name, rel, [], [name.lower()], 1, len(self.lines), "")
        graph.edges.add(("contains", parent, self.doc))
        # Front matter is not Markdown: blank it so markdown-it keeps the line numbers.
        body = [""] * self.front + self.lines[self.front:]
        tokens = _md.parse("\n".join(body))
        index = 0
        while index < len(tokens):
            index = self._block(tokens, index)
        self._own_texts()

    def _block(self, tokens: list, index: int) -> int:
        """Add the node of the top-level block that starts at `index`; the index after it."""
        token = tokens[index]
        end = _close(tokens, index)
        if token.type == "heading_open":
            self._section(token, tokens[index + 1].content)
        elif token.type in BLOCKS:
            kind, prefix = BLOCKS[token.type]
            block = self._add_block(self._parent(), kind, prefix, token.map, token.info.strip() if kind == "code" else "")
            if kind == "list":
                self._items(block, tokens, index, end)
            else:
                self._collect_links(block, tokens[index:end + 1])
        return end + 1

    def _parent(self) -> str:
        return self.stack[-1][1] if self.stack else self.doc

    def _section(self, token, title: str) -> None:
        level = int(token.tag[1])
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        parent = self._parent()
        heading_path = [*(self.stack[-1][2] if self.stack else []), title]
        base = f"{self.doc}#" + "/".join(slug(t) for t in heading_path)
        nid, n = base, 0
        while nid in self.graph.nodes:
            n += 1
            nid = f"{base}-{n}"
        start = token.map[0] + 1
        self.graph.nodes[nid] = _node("section", title, self.graph.rel, heading_path, [title.lower()], start, start, "")
        self.graph.edges.add(("contains", parent, nid))
        anchor = base_anchor = slug(title)
        n = 0
        while anchor in self.graph.anchors:
            n += 1
            anchor = f"{base_anchor}-{n}"
        self.graph.anchors[anchor] = nid
        self.stack.append((level, nid, heading_path))
        self.sections.append((nid, start))

    def _add_block(self, parent: str, kind: str, prefix: str, span, title: str = "") -> str:
        counts = self.counts.setdefault(parent, Counter())
        counts[prefix] += 1
        sep = "." if prefix == "item" else ":"
        nid = f"{parent}{sep}{prefix}{counts[prefix]}"
        start, end = _trimmed(self.lines, span[0], span[1])
        text = "\n".join(self.lines[start - 1:end])
        self.graph.nodes[nid] = _node(kind, title or kind, self.graph.rel, [], [], start, end, text)
        self.graph.edges.add(("contains", parent, nid))
        return nid

    def _items(self, block: str, tokens: list, start: int, end: int) -> None:
        """A node for each item of the list at `tokens[start]`, its direct children only;
        the links in an item start from the item."""
        level = tokens[start].level + 1
        for i in range(start + 1, end):
            if tokens[i].type == "list_item_open" and tokens[i].level == level:
                item = self._add_block(block, "list_item", "item", tokens[i].map)
                self._collect_links(item, tokens[i:_close(tokens, i) + 1])

    def _collect_links(self, block: str, tokens: list) -> None:
        for token in tokens:
            for child in token.children or ():
                if child.type == "link_open" and child.attrs.get("href"):
                    self.graph.links.append((block, str(child.attrs["href"])))

    def _own_texts(self) -> None:
        """The document's own text (front matter and the lines before the first heading) and
        each section's: its heading and the lines up to the next heading, trailing blanks trimmed."""
        first = self.sections[0][1] if self.sections else len(self.lines) + 1
        self.graph.nodes[self.doc]["text"] = "\n".join(self.lines[:first - 1]).rstrip("\n")
        for i, (nid, start) in enumerate(self.sections):
            stop = self.sections[i + 1][1] - 1 if i + 1 < len(self.sections) else len(self.lines)
            start, end = _trimmed(self.lines, start - 1, stop)
            self.graph.nodes[nid].update(start_line=start, end_line=end,
                                         text="\n".join(self.lines[start - 1:end]))


def _node(kind: str, title: str, path: str, heading_path: list[str] | None = None, names: list[str] | None = None,
          start: int | None = None, end: int | None = None, text: str = "") -> dict:
    """The fields of `store.node` for one node."""
    return {"type": kind, "title": title, "path": path, "heading_path": heading_path or [],
            "names": names or [], "start_line": start, "end_line": end, "text": text}


def _close(tokens: list, index: int) -> int:
    """The index of the token that closes the block opened at `index` (itself for a leaf)."""
    token = tokens[index]
    if token.nesting != 1:
        return index
    depth = 0
    for i in range(index, len(tokens)):
        depth += tokens[i].nesting
        if depth == 0:
            return i
    return len(tokens) - 1


def _trimmed(lines: list[str], start: int, stop: int) -> tuple[int, int]:
    """1-based first and last line of `lines[start:stop]` without its trailing blank lines."""
    end = stop
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start + 1, max(end, start + 1)


def _front_matter_end(lines: list[str]) -> int:
    """The number of lines a YAML front matter block takes at the top, or 0."""
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() in FRONT_MATTER_ENDS:
            return i + 1
    return 0
