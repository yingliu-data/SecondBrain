# ast-everything

A library that turns Python and Markdown files into AST-style nodes and edges in a
MongoDB Atlas store, versioned and add-only, and searches them, so an agent finds
code and documents without reading every file. It is a member of the uv workspace;
agent-api offers it to agents as the host tools of its `ast_index` skill
([agent-api/app/skills/ast_index/](../agent-api/app/skills/ast_index/)), which hold
the Atlas credentials, so the guest never sees them.

## The store

One database (`MONGODB_DB`, default `secondbrain`) with four collections shared by
every source. A source is one indexed project, folder or document set, and its name
heads every ID, so two sources never share a node:

- `files`: one manifest row per indexed file (hash, size, mtime, versions,
  `removed_at`, `skipped_reason`);
- `nodes`: one document per version of a node;
- `edges`: `contains`, `imports`, `invokes`, `inherits`, `links_to`, and `supersedes`
  between versions, each with the source and file it was parsed from;
- `chunks`: the text Atlas embeds, one per version of a chunked node.

A version's ID is `<id>@v<n>`. Indexing is add-only: a changed node gets a new version
and a `supersedes` edge, a removed file or node gets `removed_at`, a restored file
continues its versions, and nothing is deleted. When a file gains or loses a node, or
is removed, the edges of the files that point into it are resolved again; a file
indexed before its target existed gets the edge when it next changes.

## The parsers

A parser per file type ([ast_everything/parsers/](ast_everything/parsers/)), chosen by
suffix. The walk skips hidden folders, virtualenvs and installed packages.

**Python** (`.py`, [code_graph.py](ast_everything/parsers/code_graph.py)), modeled on
LocAgent's dependency graph:

| Node | ID | Text |
|---|---|---|
| `directory` | `<source>/<folder>` | none |
| `file` | `<source>/<path>` | the module-level lines |
| `class`, `function` | `<source>/<path>:Qualified.name` | its own lines |

Edges: `contains`, `imports`, `invokes`, `inherits`. Classes and functions are
chunked.

**Markdown** (`.md`, `.markdown`, [markdown_tree.py](ast_everything/parsers/markdown_tree.py)),
parsed with markdown-it (CommonMark and tables), down to blocks:

| Node | ID | Text |
|---|---|---|
| `document` | `<source>/<path>` | front matter and the lines before the first heading |
| `section` | `<source>/<path>#setup/usage` (heading slugs, as GitHub makes anchors) | the heading and its blocks up to the next heading |
| `paragraph`, `list`, `table`, `code`, `quote`, `html` | `<section>:p1`, `:list2`, `:code1`, … | the block's lines |
| `list_item` | `<list>.item3` | the item's lines |

Edges: `contains` (document, sections, subsections, blocks, list items) and `links_to`
(from a block to a section of the same file by `#anchor`, or to another indexed file
by relative link). Documents and sections are chunked; blocks are not embedded and
are found by ID, by traversal, or through their section.

## Credentials

Four variables, from the environment or else from `.env` at the repository root (see
`.env.example`): `MONGODB_USERNAME`, `MONGODB_PASSWORD`, `MONGODB_HOST` (the cluster
host alone, the part after `@` in the connection string that Atlas shows under
Clusters, Connect) and `MONGODB_DB`. The `.env` is read, never loaded into the
environment, and the local sandbox strips `MONGODB_*` from the guest's environment.
Indexing needs a user with `readWrite` on the database. Atlas must allow the host's IP
(Network Access): an IP it does not allow fails with `TLSV1_ALERT_INTERNAL_ERROR`.

## The agent's tools (`ast_index` skill)

- `ast_index(path | workspace_path | git_url, name)`: indexes a folder or one file on
  the host, in the session's workspace (as of the end of the last task), or a git repo
  cloned under `AST_REPOS_DIR` (default `data/ast-everything/repos`), as the source
  `name` (default: made from the folder or repo name). It runs in the background and
  returns a `job_id`; one job per source runs at a time. In the agent-api container a
  host path is visible only when mounted.
- `ast_index_status(job_id)`: the job's state (`running`, `done`, `failed`), its counts
  or its error.
- `ast_sources()`: the indexed sources with their file counts.
- `ast_search(query, scope, node_types, path_prefix, time_range, history, detail, limit)`:
  four layers, exact ID, name, keyword over names (Atlas Search), then content by
  keyword and meaning (vector search over the chunks); the first layer with hits
  answers. Latest versions only unless `history`.
- `ast_traverse(start_ids, direction, hops, node_types, edge_types, max_fanout)`: the
  nodes within `hops` edges, one indexed query per hop.
- `ast_retrieve(ids, detail, context_lines, page)`: the text with its line range,
  200 lines a page; a superseded version names the newest one.
- `ast_search_by_time(time_range, scope, node_types, limit)`: what was added, changed
  or removed in the range.

`limit` is 1 to 100, `hops` 1 to 5, `max_fanout` 1 to 100 and `context_lines` 0 to 100;
a value outside its bounds is refused with an error that names the argument. When
MongoDB is down or the credentials are wrong, the tool answers with an error that says so.

The first index job creates the ordinary indexes and the three Atlas Search indexes of
[ast_everything/atlas/](ast_everything/atlas/). The vector index embeds every new chunk
with `voyage-4`, which Atlas bills per token.

## From the command line

```bash
uv run python -m ast_everything.indexer sync /path/to/project [--source NAME]
```

## Test

```bash
cd ast-everything && uv run pytest -n auto -q                        # offline, on mongomock
AST_EVERYTHING_LIVE=1 uv run pytest -q tests/test_live_atlas.py      # against Atlas, in a throw-away database
```
