# ast-everything

A library that turns Python and Markdown files, and the agent's chat sessions, into
AST-style nodes and edges in a MongoDB Atlas store, versioned and add-only, and
searches them, so an agent finds code, documents and past conversations without
reading every file. It is a member of the uv workspace, and it stands alone: copy the
folder into another project and any Python agent can use its tools (see "The agent's
tools"). agent-api offers them to its agents as the host tools of its `ast_index` skill
([agent-api/app/skills/ast_index/](../agent-api/app/skills/ast_index/)), which hold
the Atlas credentials, so the guest never sees them.

## The store

One database (`MONGODB_DB`, default `secondbrain`) with five collections shared by
every source. A source is one indexed project, folder or document set, and its name
heads every ID, so two sources never share a node:

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

[memory.py](ast_everything/memory.py) indexes one chat session at a time into the source
`memory-<user>`: `index_session(store, source, session, title, turns, memory_root=, memory_files=)`.

| Node | ID | Text |
|---|---|---|
| `session` | `memory-<user>/sessions/<tenant>/<session_id>` | the session's title |
| `turn` | `<session>:turn-<n>` | what was said; `said_by` (`user` or `assistant`) and `at` |

Edges: `contains` (the session to each turn) and `precedes` (each turn to the next).
Each turn is chunked, split at paragraphs past about 500 words, each part headed by
the session title. The user memory files the session wrote or removed are indexed
with the Markdown parser, as `memory-<user>/<slug>.md`, when they are on disk, and
marked removed when they are gone (another session may have written one again); no
other file is read.

The session's record in `sessions` holds the cursor `indexed_through_turn` (a run
indexes only the turns after it), `last_turn`, `indexed_at`, `status`, and a lease
`lock_until` (10 minutes) that one run takes with a conditional update: a second run
of the same session meanwhile skips it. Every write is an upsert on a fixed ID, so a
run that stops part-way is completed by the next one, and a second run inserts nothing.

agent-api runs it on the host ([memory_index.py](../agent-api/app/host/memory_index.py)),
from the session's event log, when `MEMORY_INDEX` is on (default off): when a session's
Worker closes (idle eviction, `HARNESS_WORKER_EVICT_S`), when its log has not changed for
30 minutes (checked every 5), and at startup for every session changed since its last
index (the record's `log_mtime`: the newest log write read before the turns). A check
indexes at most 20 sessions and logs the size of a larger backlog, so the first start
drains past sessions over the next checks. The guest tenant is not indexed. A failed run
is a warning, and the next check tries again, after the sessions that did not fail. Every
turn goes to Atlas and is embedded there, billed per token; to stop, turn `MEMORY_INDEX` off and restart agent-api (the
nodes stay in the store).

The agent's tools show a user only their own memory source: an unscoped search leaves
out the other users' `memory-*` sources (those with session records), `ast_sources` does
not list them, and a `scope` or an ID in one of them is refused.

## Credentials

Four variables, from the environment or else from `.env` at the repository root (see
`.env.example`): `MONGODB_USERNAME`, `MONGODB_PASSWORD`, `MONGODB_HOST` (the cluster
host alone, the part after `@` in the connection string that Atlas shows under
Clusters, Connect) and `MONGODB_DB`. The `.env` is read, never loaded into the
environment, and the local sandbox strips `MONGODB_*` from the guest's environment.
Indexing needs a user with `readWrite` on the database. Atlas must allow the host's IP
(Network Access): an IP it does not allow fails with `TLSV1_ALERT_INTERNAL_ERROR`.

## The agent's tools

[tools.py](ast_everything/tools.py) holds the tool definitions (OpenAI function format)
and runs them, so any Python agent can offer them once the `MONGODB_*` variables are set:

```python
from ast_everything.tools import AstTools

tools = AstTools()                                    # MONGODB_* from the environment or .env
tools.definitions                                     # give these to the model
answer = tools.call("ast_search", {"query": "setup"})  # JSON text, or "Error: ..."
```

`AstTools` also takes `repos_dir` (where git clones go; default `AST_REPOS_DIR`, else
`data/ast-everything/repos`), `workspace` (a function that returns the folder
`workspace_path` resolves in, or None) and `hidden` (a function that takes the store and
returns the sources the caller may not read). `call` blocks; an async agent runs it in a
thread. agent-api's `ast_index` skill gives the session's workspace and hides the other
users' memory sources.

- `ast_index(path | workspace_path | git_url, name)`: indexes a folder or one file on
  the host, in the session's workspace (as of the end of the last task), or a git repo
  cloned under `repos_dir`, as the source `name` (default: made from the folder or repo
  name; a name starting with `memory-` is refused). It runs in the background and
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
