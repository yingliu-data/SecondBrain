"""canonical — canonical JSON, hashing and derived ids (PLAN.md section 5).

Canonical form: sorted keys, no whitespace, every str NFC-normalised (keys
included), numbers as Python prints them: ints as digits, floats by their
shortest round-trip repr (json.dumps uses float.__repr__, which is the same
text on every platform for the same IEEE-754 double; 1.0 stays "1.0" so an int
and a float never collide), NaN and infinity rejected because they have no
JSON text. ensure_ascii=False so the bytes are the UTF-8 of the text.
Hash = sha256 of those bytes.

Derived ids are a pure function of (user, tenant, session_id, seq, kind), so a
replay, a restored snapshot and a second worker all mint the same id for the
same event. No randomness anywhere in this module.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata

_ID_HEX = 24


def _normalise(obj):
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, bool) or obj is None or isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise ValueError(f"non-finite float is not canonical: {obj!r}")
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(f"non-string key: {k!r}")
            out[unicodedata.normalize("NFC", k)] = _normalise(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_normalise(v) for v in obj]
    raise ValueError(f"not canonicalisable: {type(obj).__name__}")


def is_int(value, minimum: int | None = None) -> bool:
    """True for an int that is not a bool, optionally >= minimum. The one
    place the "bool is an int" rule lives; validators call this."""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return minimum is None or value >= minimum


def canonicalisable_error(obj, path: str = "body") -> str | None:
    """Why `obj` cannot be canonicalised, or None if it can. The same walk as
    canonical_json without building the copy or serialising: validators use
    it on every event body, so it must be cheap."""
    if isinstance(obj, str) or obj is None or isinstance(obj, bool) or isinstance(obj, int):
        return None
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return f"{path}: non-finite float {obj!r}"
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                return f"{path}: non-string key {k!r}"
            err = canonicalisable_error(v, f"{path}.{k}")
            if err:
                return err
        return None
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            err = canonicalisable_error(v, f"{path}[{i}]")
            if err:
                return err
        return None
    return f"{path}: {type(obj).__name__} is not canonicalisable"


def canonical_json(obj) -> str:
    """The one text for a value. Raises ValueError for non-finite floats and odd types."""
    return json.dumps(_normalise(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def sha256_hex(data) -> str:
    """sha256 of a str (UTF-8) or bytes, as lowercase hex."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_value(obj) -> str:
    """sha256 of the canonical JSON of a value."""
    return sha256_hex(canonical_json(obj))


def derive_id(user: str, tenant: str, session_id: str, seq: int, kind: str) -> str:
    """Deterministic id for `kind` ('turn', 'question', 'op', 'ticket', ...)
    minted at event `seq` of a session. Shape: '<kind>_<24 hex>'."""
    if not is_int(seq, 1):
        raise ValueError(f"seq must be a positive int, got {seq!r}")
    return _minted(kind, [user, tenant, session_id, seq, kind])


def derive_key_id(kind: str, *parts: str) -> str:
    """Deterministic id for `kind` keyed by stable strings instead of an event
    seq (e.g. a ticket per input: seqs restart with every guest run).
    Same shape as derive_id: '<kind>_<24 hex>'."""
    if not parts or not all(isinstance(p, str) for p in parts):
        raise ValueError("parts must be one or more str")
    return _minted(kind, [kind, *parts])


def _minted(kind: str, material: list) -> str:
    if not kind or "_" in kind:
        raise ValueError(f"kind must be a non-empty word without '_': {kind!r}")
    return f"{kind}_{hash_value(material)[:_ID_HEX]}"
