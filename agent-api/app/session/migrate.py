"""One-time migration: conversations.db rows → session directories.

Usage: python -m app.session.migrate [--db data/conversations.db] [--root data/sessions]

Mapping:
  "abc"          (un-namespaced) → user "default", tenant "default"
  "wcc-event:abc" (namespaced)   → tenant "wcc-event" under its configured user
                                   (resolved via tenants.json; fallback: user = tenant)
Idempotent: existing session dirs are skipped. SQLite is left untouched.
"""
from __future__ import annotations

import argparse
import json
import sqlite3

from app.config import SESSION_DB_PATH, SESSIONS_ROOT
from app.session.dir_store import DirStore
from app.session.ids import is_safe_id


def _tenant_users() -> dict[str, str]:
    try:
        from app.tenants import create_tenant_registry
        return {t.name: t.user for t in create_tenant_registry().all_tenants()}
    except Exception:
        return {}


def migrate(db_path: str = SESSION_DB_PATH, root: str = SESSIONS_ROOT) -> dict:
    store = DirStore(root)
    users = _tenant_users()
    stats = {"migrated": 0, "skipped_existing": 0, "skipped_invalid": 0}

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT session_id, messages, updated_at FROM sessions").fetchall()
    conn.close()

    for store_key, messages_json, updated_at in rows:
        if ":" in store_key:
            tenant, _, session_id = store_key.partition(":")
        else:
            tenant, session_id = "default", store_key
        user = users.get(tenant, tenant)

        if not (is_safe_id(user) and is_safe_id(tenant) and is_safe_id(session_id)):
            print(f"  SKIP (unsafe id): {store_key!r}")
            stats["skipped_invalid"] += 1
            continue
        if store.get(user, tenant, session_id) is not None:
            stats["skipped_existing"] += 1
            continue

        session = store.get_or_create(user, tenant, session_id)
        try:
            messages = json.loads(messages_json)
        except json.JSONDecodeError:
            messages = []
        for msg in messages:
            role = msg.get("role", "")
            if role in ("user", "assistant"):
                session.append_history(role, msg.get("content") or "")
        session.update_meta(title=f"migrated from sqlite ({updated_at})")
        session.append_trace("migrated_from_sqlite", {"rows": len(messages)})
        print(f"  {store_key!r} -> {user}/{tenant}_{session_id} ({len(messages)} msgs)")
        stats["migrated"] += 1

    print(f"Done: {stats}")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=SESSION_DB_PATH)
    parser.add_argument("--root", default=SESSIONS_ROOT)
    args = parser.parse_args()
    migrate(args.db, args.root)
