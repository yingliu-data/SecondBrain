"""MemoryStore — file-based durable memory with YAML-ish frontmatter.

Each memory is one markdown file under a root directory::

    {slug}.md:
        ---
        name: <human-readable title>
        description: <one-line hook used for recall>
        type: user|feedback|project|reference
        ---

        <body markdown>

An index file ``MEMORY.md`` holds one-line pointers and is loaded into the
system prompt on every turn; bodies are inlined selectively when recall finds
a description match.

Used in two tiers:
- user memory (durable): MemoryStore({USERS_ROOT}/{user_id}/memory)
- session memory (scratch): MemoryStore(SessionDir.memory)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VALID_SLUG = re.compile(r"^[a-z0-9_\-]{1,64}$")
_FM_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)
_VALID_TYPES = {"user", "feedback", "project", "reference"}


@dataclass
class MemoryRecord:
    slug: str
    name: str
    description: str
    type: str
    body: str
    path: Path


class MemoryStore:
    INDEX_NAME = "MEMORY.md"

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def index(self) -> Path:
        return self.root / self.INDEX_NAME

    def write(
        self,
        *,
        slug: str,
        name: str,
        description: str,
        type: str,
        body: str,
    ) -> MemoryRecord:
        if not _VALID_SLUG.match(slug):
            raise ValueError(
                f"invalid slug {slug!r}: use lowercase a-z, 0-9, _ or -, max 64 chars"
            )
        if type not in _VALID_TYPES:
            raise ValueError(
                f"invalid memory type {type!r}: must be one of {sorted(_VALID_TYPES)}"
            )
        name_clean = " ".join(name.split())
        desc_clean = " ".join(description.split())
        path = self.root / f"{slug}.md"
        header = (
            f"---\nname: {name_clean}\n"
            f"description: {desc_clean}\n"
            f"type: {type}\n---\n\n"
        )
        path.write_text(header + body.strip() + "\n", encoding="utf-8")
        self._rewrite_index()
        return MemoryRecord(slug, name_clean, desc_clean, type, body, path)

    def list(self) -> list[MemoryRecord]:
        out: list[MemoryRecord] = []
        for p in sorted(self.root.glob("*.md")):
            if p.name == self.INDEX_NAME:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            m = _FM_RE.match(text)
            if not m:
                continue
            header: dict[str, str] = {}
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    header[k.strip()] = v.strip()
            out.append(
                MemoryRecord(
                    slug=p.stem,
                    name=header.get("name", p.stem),
                    description=header.get("description", ""),
                    type=header.get("type", "user"),
                    body=m.group(2),
                    path=p,
                )
            )
        return out

    def recall(self, query: str, *, limit: int = 5) -> list[MemoryRecord]:
        """Keyword overlap on description. Swap in embeddings later behind same signature."""
        tokens = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) >= 3]
        if not tokens:
            return []
        scored: list[tuple[int, MemoryRecord]] = []
        for r in self.list():
            hay = f"{r.name} {r.description}".lower()
            score = sum(1 for t in tokens if t in hay)
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], x[1].slug))
        return [r for _, r in scored[:limit]]

    def remove(self, slug: str) -> bool:
        path = self.root / f"{slug}.md"
        if not path.exists():
            return False
        path.unlink()
        self._rewrite_index()
        return True

    def _rewrite_index(self) -> None:
        entries = self.list()
        if not entries:
            try:
                self.index.unlink()
            except FileNotFoundError:
                pass
            return
        lines = [
            f"- [{r.name}]({r.slug}.md) — {r.description}"
            if r.description
            else f"- [{r.name}]({r.slug}.md)"
            for r in entries
        ]
        self.index.write_text("\n".join(lines) + "\n", encoding="utf-8")
