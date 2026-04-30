"""Extractor — end-of-turn LLM pass that harvests durable facts.

After each turn, reads the last user/assistant exchange and asks the LLM
to propose:
- new **memories** (auto-applied, auditable via MEMORY.md)
- a **profile patch** (queued to PendingStore for user approval)

This runs **after** the SSE stream closes — it adds no user-visible
latency. It's best-effort: any failure is logged and the turn is
considered successful regardless.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import USERS_ROOT
from app.user.memory import MemoryStore
from app.user.pending import ALLOWED_PROFILE_FIELDS, PendingStore
from app.util.json_safe import DefensiveJSONError, parse_json_defensive

logger = logging.getLogger("extractor")

EXTRACTOR_SYSTEM = """You are a MEMORY EXTRACTOR. Given the last user/assistant exchange,
propose NEW durable facts worth saving for future conversations.

Output ONE valid JSON object — no prose, no code fences:

{
  "memories": [
    {"slug": "snake_case_id",
     "name": "Human-readable title",
     "description": "one-line recall hook",
     "type": "user" | "feedback" | "project" | "reference",
     "body": "1-3 sentences of content"}
  ],
  "profile_patch": {"display_name": "...", "timezone": "...", "email": "...", "tone": "..."},
  "rationale": "one-line explanation of the profile patch, or empty string"
}

Memory types:
- user       → stable identity / role / expertise
- feedback   → explicit correction or confirmed preference
- project    → ongoing initiative, deadline, stakeholder
- reference  → external system the user named (Slack channel, Linear project, etc.)

Rules:
- Be conservative. If nothing new, return {"memories": [], "profile_patch": null, "rationale": ""}.
- Max 2 memories per turn.
- Only include profile fields the user EXPLICITLY stated in this turn.
  Do NOT guess based on context or code.
- Slugs: lowercase a-z, 0-9, _, -, max 64 chars.
- Skip memories that duplicate the existing memory index shown below.
"""

_SLUG_RE = re.compile(r"^[a-z0-9_\-]{1,64}$")
_MAX_MEMORIES_PER_TURN = 2


@dataclass
class ExtractionResult:
    memories_written: list[str]           # slugs of memories actually saved
    profile_change_id: str | None         # id of the queued PendingChange, if any
    skipped: list[str]                    # human-readable reasons


def _recent_exchange(history: list[dict]) -> list[dict]:
    """Pick the trailing user→assistant pair for extraction.

    Returns up to 4 trailing messages so tool responses mid-turn aren't cut
    off awkwardly. The extractor prompt handles multi-message exchanges.
    """
    return history[-4:] if history else []


def _build_user_prompt(exchange: list[dict], existing_index: str) -> str:
    lines = ["Last exchange:"]
    for msg in exchange:
        role = msg.get("role", "?")
        content = (msg.get("content") or "")[:800]
        lines.append(f"[{role}] {content}")
    if existing_index.strip():
        lines.extend(["", "Existing memory index (do NOT duplicate):", existing_index.strip()])
    else:
        lines.extend(["", "Existing memory index: (empty)"])
    lines.append("")
    lines.append("Now produce the JSON object.")
    return "\n".join(lines)


def _coerce_memory(raw: dict) -> dict | None:
    """Return a cleaned memory dict, or None if invalid."""
    if not isinstance(raw, dict):
        return None
    slug = str(raw.get("slug", "")).strip()
    if not _SLUG_RE.match(slug):
        return None
    mtype = raw.get("type")
    if mtype not in ("user", "feedback", "project", "reference"):
        return None
    name = str(raw.get("name", "")).strip()
    if not name:
        return None
    body = str(raw.get("body", "")).strip()
    if not body:
        return None
    return {
        "slug": slug,
        "name": name[:200],
        "description": str(raw.get("description", "")).strip()[:300],
        "type": mtype,
        "body": body[:2000],
    }


async def run_extraction(
    *,
    user_id: str,
    history: list[dict],
    llm,
) -> ExtractionResult:
    """Run the extractor against a freshly-closed turn.

    Pure-enough: reads history (already on disk), calls LLM, writes to
    user's memory store and pending store. Returns what was (and wasn't)
    applied for logging / testing.
    """
    user_dir = Path(USERS_ROOT) / user_id
    mem_store = MemoryStore(user_dir / "memory")
    pending_store = PendingStore(user_dir / "pending")

    result = ExtractionResult(memories_written=[], profile_change_id=None, skipped=[])

    exchange = _recent_exchange(history)
    if not exchange:
        result.skipped.append("no history")
        return result

    existing_index = ""
    if mem_store.index.exists():
        try:
            existing_index = mem_store.index.read_text(encoding="utf-8")
        except OSError:
            pass

    messages = [
        {"role": "system", "content": EXTRACTOR_SYSTEM},
        {"role": "user", "content": _build_user_prompt(exchange, existing_index)},
    ]

    try:
        resp = await llm.chat_completion(messages, tools=None)
    except Exception as e:
        logger.warning(f"Extractor LLM call failed: {e}")
        result.skipped.append(f"llm_error: {type(e).__name__}")
        return result

    raw = (resp["choices"][0]["message"].get("content") or "").strip()
    try:
        data = parse_json_defensive(raw, expect=dict)
    except DefensiveJSONError as e:
        logger.warning(f"Extractor produced invalid JSON: {e}; raw={raw[:200]!r}")
        result.skipped.append("bad_json")
        return result

    existing_slugs = {r.slug for r in mem_store.list()}
    mem_proposals = data.get("memories")
    if isinstance(mem_proposals, list):
        for raw_mem in mem_proposals[:_MAX_MEMORIES_PER_TURN]:
            clean = _coerce_memory(raw_mem)
            if clean is None:
                result.skipped.append("memory invalid")
                continue
            if clean["slug"] in existing_slugs:
                result.skipped.append(f"memory duplicate: {clean['slug']}")
                continue
            try:
                mem_store.write(**clean)
                result.memories_written.append(clean["slug"])
                existing_slugs.add(clean["slug"])
            except ValueError as e:
                logger.warning(f"Extractor memory write failed: {e}")
                result.skipped.append(f"memory write error: {e}")

    patch_raw = data.get("profile_patch")
    if isinstance(patch_raw, dict):
        fields: dict[str, str] = {}
        for k in ALLOWED_PROFILE_FIELDS:
            v = patch_raw.get(k)
            if isinstance(v, str) and v.strip():
                fields[k] = v.strip()
        if fields:
            rationale = str(data.get("rationale", ""))
            change = pending_store.propose(
                user_id=user_id,
                fields=fields,
                proposed_by="extractor",
                rationale=rationale,
            )
            if change is not None:
                result.profile_change_id = change.id

    logger.info(
        f"Extractor for {user_id}: {len(result.memories_written)} memories, "
        f"profile_change={result.profile_change_id}, skipped={len(result.skipped)}"
    )
    return result


# ── Fire-and-forget scheduling ──────────────────────────────────────────

# Hold references so tasks aren't garbage-collected mid-run.
_inflight: set[asyncio.Task] = set()


def schedule_extraction(*, user_id: str, history: list[dict], llm) -> asyncio.Task:
    """Spawn the extractor as a background task that survives the SSE close.

    Call this from chat.py AFTER the response stream has ended. Exceptions
    are logged, not raised. Returns the Task so callers can await it in tests.
    """
    async def _run():
        try:
            await run_extraction(user_id=user_id, history=history, llm=llm)
        except Exception as e:
            logger.exception(f"Background extractor failed for {user_id}: {e}")

    task = asyncio.create_task(_run(), name=f"extractor_{user_id}")
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return task
