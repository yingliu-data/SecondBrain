# ast-everything

A Python library that turns Python and Markdown files, and chat sessions, into
AST-style nodes and edges in a MongoDB Atlas store, versioned and add-only, and
searches them, so an agent finds code, documents and past conversations without
reading every file.

It is an independent package. It imports nothing from SecondBrain, needs only
`pymongo`, `markdown-it-py` and `python-dotenv`, and runs anywhere Python 3.12 and a
MongoDB Atlas cluster are available: as a command-line indexer, as a library, or as a
set of tools that any Python agent can offer its model. SecondBrain uses it as one
member of its uv workspace (see "Inside SecondBrain"), but nothing else in this README
depends on that.

## Install

From a checkout of this folder, or a copy of it in another project:

```bash
cd ast-everything && uv sync          # its own virtualenv, with the dev dependencies
pip install ./ast-everything          # or into an existing environment
uv build --wheel                      # or a wheel to ship (dist/ast_everything-*.whl)
```

Straight from GitHub, for those with access to the repo:

```bash
pip install "git+https://github.com/yingliu-data/SecondBrain#subdirectory=ast-everything"
```

The package carries the Atlas Search index definitions
([ast_everything/atlas/](ast_everything/atlas/)), so an installed wheel creates its
own indexes; nothing else needs copying.

## Credentials

Four variables, from the environment or else from a `.env` in the current folder or a
parent:

```bash
MONGODB_USERNAME=...
MONGODB_PASSWORD=...
MONGODB_HOST=cluster0.abcde.mongodb.net   # the part after `@` in Atlas's connection string
MONGODB_DB=secondbrain                    # optional; this is the default
```

`MONGODB_HOST` is the cluster host alone, as Atlas shows it under Clusters, Connect.
The `.env` is read, never loaded into the environment, so a process this one starts
does not inherit the credentials. Indexing needs a user with `readWrite` on the
database. Atlas must allow the machine's IP (Network Access): an IP it does not allow
fails with `TLSV1_ALERT_INTERNAL_ERROR`. A missing variable fails with an error that
names it.

## Quick start

From the command line, index a folder as one source:

```bash
uv run python -m ast_everything.indexer sync /path/to/project [--source NAME]
```

It prints the source name and its counts (files indexed, unchanged and skipped, nodes
inserted and removed). Running it again indexes only what changed.

From an agent, hand the model the tool definitions and run its calls:

```python
from ast_everything.tools import AstTools

tools = AstTools()                                    # MONGODB_* from the environment or .env
tools.definitions                                     # give these to the model
answer = tools.call("ast_search", {"query": "setup"})  # JSON text, or "Error: ..."
```

The definitions are in the OpenAI function format; for another API (Anthropic's
`input_schema`, for one) take each definition's `name`, `description` and
`parameters`. `call` blocks; an async agent runs it in a thread.

As a library, the modules can be used directly:

| Module | What it does |
|---|---|
| [store.py](ast_everything/store.py) | `Store.connect()`, the collections, `ensure_indexes`, `ensure_search_indexes` |
| [indexer.py](ast_everything/indexer.py) | `sync(store, source, folder)`, `index_files` (with a path `prefix`, so several folders share a source), `remove_files` |
| [search.py](ast_everything/search.py) | `search`, `recall`, `traverse`, `retrieve`, `by_time`, `sources` |
| [memory.py](ast_everything/memory.py) | `index_session`, `index_memory`, `sync_memory`: chat sessions and memory files as nodes |
| [tools.py](ast_everything/tools.py) | `AstTools`, the agent tools over all of the above |
| [parsers/](ast_everything/parsers/) | `parse`, `iter_files`, the Python and Markdown parsers (no database needed) |

The first index (from the CLI, an `ast_index` call or `index_session`) creates the
ordinary indexes and the three Atlas Search indexes. The vector index embeds every
new chunk with `voyage-4`, which Atlas bills per token.

## The agent's tools

`AstTools` takes:

- `connect`: a function that makes the store (default `Store.connect`, the `MONGODB_*`
  variables);
- `repos_dir`: where git clones go (default `AST_REPOS_DIR`, else
  `data/ast-everything/repos` under the current folder);
- `workspace`: a function that returns the folder `workspace_path` resolves in, or
  None (the default, which leaves `workspace_path` unusable);
- `hidden`: a function that takes the store and returns the sources the caller may not
  read (default none);
- `private_roots`: folders an `ast_index(path=...)` may neither be in nor hold, such as
  other users' data (default none).

The tools:

- `ast_index(path | workspace_path | git_url, name)`: indexes a folder or one file on
  this machine, in the agent's workspace, or a git repo cloned under `repos_dir`, as
  the source `name` (default: made from the folder or repo name; a name starting with
  `memory-` is refused). It runs in the background, in this process, and returns a
  `job_id`; one job per source runs at a time, and jobs are forgotten on restart
  (indexing again is cheap: unchanged files are skipped). A job's `target` keeps a git
  URL without its credentials (`https://***@host/...`), and so does its `error`.
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

`ast_index(path=...)` reads any folder the process can read that is neither in nor
above one of `private_roots`.
`hidden` hides sources from the read tools, and `private_roots` keeps folders out of
new sources, but neither is a sandbox: an agent that must not read the whole machine
needs a process that can read only what it may.

## The store

One database (`MONGODB_DB`) with five collections shared by every source. A source is
one indexed project, folder or document set, and its name heads every ID, so two
sources never share a node:

- `files`: one manifest row per indexed file (hash, size, mtime, versions,
  `removed_at`, `skipped_reason`);
- `nodes`: one document per version of a node;
- `edges`: `contains`, `imports`, `invokes`, `inherits`, `links_to`, `precedes`, and
  `supersedes` between versions, each with the source and file it was parsed from;
- `chunks`: the text Atlas embeds, one per version of a chunked node;
- `sessions`: one record per indexed chat session (see "Session memory").

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

## Session memory

[memory.py](ast_everything/memory.py) indexes one chat session at a time into a source
named `memory-<user>` (`memory_source(user)`; the `memory-` prefix is reserved, and an
index tool refuses it): `index_session(store, source, session, title, turns, memory=[...])`.
The caller reads the turns from whatever log its agent keeps and passes them as
`Turn(n, said_by, at, text)`.

| Node | ID | Text |
|---|---|---|
| `session` | `memory-<user>/sessions/<tenant>/<session_id>` | the session's title |
| `turn` | `<session>:turn-<n>` | what was said; `said_by` (`user` or `assistant`) and `at` |

Edges: `contains` (the session to each turn) and `precedes` (each turn to the next).
Each turn is chunked, split at paragraphs past about 500 words, each part headed by
the session title.

Memory files are Markdown files `<slug>.md` in folders, each folder named by a
`MemoryFiles(root, slugs, prefix)`: its files sit at `<prefix><slug>.md` in the source, so
the user's folder (prefix `""`: `memory-<user>/<slug>.md`) and a session's own (prefix
`sessions/<tenant>/<id>/memory/`, beside its turns) share one source. They are indexed
with the Markdown parser:
- `index_session(..., memory=[...])`: the files the session wrote or removed;
- `index_memory(store, source, files)`: some files of one folder, such as the one just
  written;
- `sync_memory(store, source, files)`: the folder's files at that prefix become exactly
  `files.slugs`, and the others are marked removed (for files no session names, such as
  ones written by hand).

A file on disk is indexed and one that is gone is marked removed (another session may
have written it again), and a symlink is never read.

`recall(store, query, scope=, limit=, min_score=, exclude_paths=)` in
[search.py](ast_everything/search.py) brings back the latest nodes nearest a text by
meaning, each with the `score` Atlas gives its best chunk (0 to 1). It uses vector search
alone, because keyword search matches common words, and it keeps only chunks scoring
`min_score` or more. It is for recall nobody asked for, such as an agent's context for a new
message, and `exclude_paths` leaves out a file or a folder (the current session).

The session's record in `sessions` holds the cursor `indexed_through_turn` (a run
indexes only the turns after it), `last_turn`, `indexed_at`, `status`, and a lease
`lock_until` (10 minutes) that one run takes with a conditional update: a second run
of the same session meanwhile skips it and gets None. Every write is an upsert on a
fixed ID, so a run that stops part-way is completed by the next one, and a second run
inserts nothing.

## Test

```bash
uv run pytest -n auto -q                                         # offline, on mongomock
AST_EVERYTHING_LIVE=1 uv run pytest -q tests/test_live_atlas.py  # against Atlas, in a throw-away database
```

Both run from this folder, inside SecondBrain or on their own.

## Inside SecondBrain

In SecondBrain the package is a member of the root uv workspace, and agent-api
depends on it (`ast-everything = { workspace = true }`). The credentials come from
`.env` at the repository root (see `.env.example`), and the local sandbox strips
`MONGODB_*` from the guest's environment.

**The tools.** agent-api offers them to its agents as the host tools of its
`ast_index` skill ([agent-api/app/skills/ast_index/](../agent-api/app/skills/ast_index/)),
so the credentials stay on the host and the guest never sees them. The skill gives
`AstTools` the session's workspace (as of the end of the last task) and hides the other
users' memory sources (those with session records): an unscoped search leaves them out,
`ast_sources` does not list them, and a `scope` or an ID in one of them is refused. Its
`private_roots` are `USERS_ROOT` and `SESSIONS_ROOT`, so `ast_index(path=...)` cannot
index any user's memory files or session workspace (the session's own workspace is
reached through `workspace_path`). In the agent-api container a host path is visible
only when mounted.

**The memory index.** agent-api runs `index_session` on the host
([memory_index.py](../agent-api/app/host/memory_index.py)), from the session's event
log, when `MEMORY_INDEX` is on (default off): when a session's Worker closes (idle
eviction, `HARNESS_WORKER_EVICT_S`), when its log has not changed for 30 minutes
(checked every 5), and at startup for every session changed since its last index (the
record's `log_mtime`: the newest log write read before the turns). A check indexes at
most 20 sessions and logs the size of a larger backlog, so the first start drains past
sessions over the next checks. The guest tenant is not indexed. A failed run is a
warning, and the next check tries again, after the sessions that did not fail. Every
turn goes to Atlas and is embedded there, billed per token; to stop, turn
`MEMORY_INDEX` off and restart agent-api (the nodes stay in the store).

Memory files are indexed from three places:
- **On write:** a file `remember` writes or `forget` removes is indexed straight away,
  in the background.
- **With its session:** the files the session wrote or removed, both the user's
  (`{USERS_ROOT}/<user>/memory/`) and the session's own (its `remember(scope=session)` files
  and compaction summaries).
- **By each check:** `sync_memory` over each user's memory folder, which covers files
  written by hand or before the index was on. A missing folder changes nothing.

A user's indexing runs under one lock in agent-api.

**Recall.** For each new message, after the memory files recalled by description, the
Worker adds up to `MEMORY_RECALL_LIMIT` (2) nodes of the user's graph that `recall` finds
with a score of `MEMORY_RECALL_MIN_SCORE` (0.8) or more. These are turns of other sessions
and memory files, never the current session's and never a file already recalled. Recall
gives up after 2 s or on any error, and the message goes without it. Tune the minimum
score against real queries: Atlas's score depends on the embedding model.

**Tests.** `uv sync --all-packages` at the repository root, then `uv run pytest -n auto`
in `ast-everything/`, a separate run from the other members, as in CI.
