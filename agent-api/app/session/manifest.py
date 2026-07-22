"""SHA-256 file manifest for ticket outputs.

A manifest binds a ticket to the exact bytes it produced inside a session
directory, so the full chain from an operation to its artefacts stays
verifiable after the fact.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Manifest:
    files: list[str] = field(default_factory=list)
    sha256: dict[str, str] = field(default_factory=dict)

    @classmethod
    def compute(cls, root: Path | str, files: Iterable[str]) -> "Manifest":
        """Compute SHA-256 for each relative path under ``root``."""
        root = Path(root)
        file_list: list[str] = []
        hashes: dict[str, str] = {}
        for rel in files:
            p = root / rel
            if not p.exists():
                continue
            file_list.append(rel)
            hashes[rel] = _sha256_file(p)
        return cls(files=file_list, sha256=hashes)

    def verify(self, root: Path | str) -> list[str]:
        """Return the list of files whose bytes no longer match the manifest."""
        root = Path(root)
        mismatched: list[str] = []
        for rel in self.files:
            p = root / rel
            if not p.exists():
                mismatched.append(rel)
                continue
            if _sha256_file(p) != self.sha256.get(rel):
                mismatched.append(rel)
        return mismatched

    def to_dict(self) -> dict[str, Any]:
        return {"files": list(self.files), "sha256": dict(self.sha256)}
