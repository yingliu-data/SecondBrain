"""The parsers, one per file type, and the walk that finds the files they read.

A parser is a class built as `Parser(source, rel, data)` from one file's bytes:
it holds `rel`, `nodes` (logical ID -> the fields of `store.node`) and the
`contains` edges, adds its other edges in `resolve_edges(lookup)` once the
indexer can tell which IDs exist, and names in `chunked` the node types that
get a chunk for Atlas to embed. A file that cannot be decoded or parsed raises
one of PARSE_ERRORS.
"""
from __future__ import annotations

import os
from pathlib import Path

from ast_everything.parsers.code_graph import PARSE_ERRORS, CodeGraph
from ast_everything.parsers.markdown_tree import MarkdownTree

PARSERS = {".py": CodeGraph, ".md": MarkdownTree, ".markdown": MarkdownTree}
SKIP_DIRS = {"site-packages", "node_modules"}

__all__ = ["PARSERS", "PARSE_ERRORS", "iter_files", "parse", "parser_for"]


def parser_for(rel: str):
    """The parser class for a file by its suffix, or None for a type no parser reads."""
    return PARSERS.get(Path(rel).suffix.lower())


def parse(source: str, rel: str, data: bytes):
    parser = parser_for(rel)
    if parser is None:
        raise ValueError(f"no parser for {rel}")
    return parser(source, rel, data)


def iter_files(root: Path):
    """The files under `root` that a parser reads, in a stable order, without the folders
    that `_skip_dir` names. A symlink is read only when its target is under `root` too: one
    out of it (into another user's data, say) would read past the check on the root alone.
    A symlinked folder is not walked (os.walk follows none)."""
    resolved = Path(root).resolve()
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not _skip_dir(Path(folder, d)))
        for name in sorted(files):
            path = Path(folder, name)
            if parser_for(name) and path.resolve().is_relative_to(resolved):
                yield path


def _skip_dir(path: Path) -> bool:
    """A hidden folder such as `.git`, a virtualenv (it holds `pyvenv.cfg`), or a
    folder of installed packages (SKIP_DIRS)."""
    return path.name.startswith(".") or path.name in SKIP_DIRS or (path / "pyvenv.cfg").is_file()
