"""Defensive JSON parsing for LLM-produced strings.

LLMs routinely wrap JSON in markdown fences, prepend ``json``, or surround the
payload with prose. This module tries a handful of cheap strategies before
giving up, so callers can treat "parse failed" as a real signal rather than
something to silently swallow.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("util.json_safe")


class DefensiveJSONError(ValueError):
    """Raised when every parsing strategy fails."""


def parse_json_defensive(raw: str, *, expect: type | None = None) -> Any:
    """Best-effort JSON parse of an LLM-produced string.

    Strategies, in order:
      1. ``json.loads`` on the stripped input.
      2. Strip surrounding ``` or ``` ```json ``` code fences.
      3. Strip a leading ``json`` prefix.
      4. Extract the first balanced ``{...}`` or ``[...]`` block.

    Args:
        raw: The string to parse.
        expect: Optional ``dict`` or ``list`` to require the parsed top-level type.

    Raises:
        DefensiveJSONError: If no strategy succeeds, or the parsed value fails ``expect``.
    """
    if not isinstance(raw, str):
        raise DefensiveJSONError(f"Expected str, got {type(raw).__name__}")

    text = raw.strip()
    if not text:
        raise DefensiveJSONError("Empty input")

    candidates: list[str] = [text]

    stripped = _strip_markdown_fences(text)
    if stripped and stripped != text:
        candidates.append(stripped)

    for base in list(candidates):
        if base.startswith("json\n") or base.startswith("json "):
            candidates.append(base[4:].lstrip())

    for candidate in candidates:
        try:
            return _validate_expect(json.loads(candidate), expect)
        except json.JSONDecodeError:
            continue

    extracted = _extract_balanced(text)
    if extracted is not None:
        try:
            return _validate_expect(json.loads(extracted), expect)
        except json.JSONDecodeError:
            pass

    raise DefensiveJSONError(f"Could not parse JSON (preview: {raw[:200]!r})")


def _strip_markdown_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_balanced(text: str) -> str | None:
    first_brace = text.find("{")
    first_bracket = text.find("[")

    if first_brace == -1 and first_bracket == -1:
        return None
    if first_brace == -1:
        start, opener, closer = first_bracket, "[", "]"
    elif first_bracket == -1:
        start, opener, closer = first_brace, "{", "}"
    elif first_brace < first_bracket:
        start, opener, closer = first_brace, "{", "}"
    else:
        start, opener, closer = first_bracket, "[", "]"

    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _validate_expect(result: Any, expect: type | None) -> Any:
    if expect is not None and not isinstance(result, expect):
        raise DefensiveJSONError(
            f"Expected {expect.__name__}, got {type(result).__name__}"
        )
    return result
