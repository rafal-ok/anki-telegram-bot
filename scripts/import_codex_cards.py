#!/usr/bin/env python3
"""Import Codex-generated cards JSON into SQLite and optionally mark sources processed."""

import argparse
import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Tuple


def db_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def ensure_col(conn: sqlite3.Connection, table: str, name: str, ddl: str):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
          user_id INTEGER PRIMARY KEY,
          deck_name TEXT NOT NULL DEFAULT '',
          mochi_api_key TEXT NOT NULL DEFAULT '',
          mochi_deck_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS notes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          type TEXT NOT NULL CHECK(type in ('basic','cloze')),
          front TEXT NOT NULL DEFAULT '',
          back TEXT NOT NULL DEFAULT '',
          cloze TEXT NOT NULL DEFAULT '',
          extra TEXT NOT NULL DEFAULT '',
          tags TEXT NOT NULL DEFAULT '',
          source_id INTEGER NOT NULL DEFAULT 0,
          origin TEXT NOT NULL DEFAULT 'unknown',
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sources (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          source_type TEXT NOT NULL,
          source_label TEXT NOT NULL DEFAULT '',
          content_text TEXT NOT NULL DEFAULT '',
          file_path TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL DEFAULT '',
          meta TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processed','ignored')),
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    ensure_col(conn, "notes", "source_id", "INTEGER NOT NULL DEFAULT 0")
    ensure_col(conn, "notes", "origin", "TEXT NOT NULL DEFAULT 'unknown'")


def ensure_user(conn: sqlite3.Connection, user_id: int):
    cur = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if not cur.fetchone():
        conn.execute("INSERT INTO users (user_id, deck_name) VALUES (?, ?)", (user_id, "Telegram Imports"))
        conn.commit()


def normalize_tags(tags: Any) -> List[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags.strip())
    if not isinstance(tags, list):
        return []
    out: List[str] = []
    seen = set()
    for tag in tags:
        if not isinstance(tag, str):
            continue
        cleaned = re.sub(r"\s+", "-", tag.strip())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def load_notes(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, dict):
        notes = payload.get("notes", [])
    elif isinstance(payload, list):
        notes = payload
    else:
        raise ValueError("Input JSON must be a list or an object with a 'notes' array.")
    if not isinstance(notes, list):
        raise ValueError("'notes' must be a JSON array.")
    return notes


def validate_note(raw: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    ntype = str(raw.get("type", "basic")).lower()
    if ntype not in {"basic", "cloze"}:
        return False, {}
    front = str(raw.get("front", "")).strip()
    back = str(raw.get("back", "")).strip()
    cloze = str(raw.get("cloze", "")).strip()
    extra = str(raw.get("extra", "")).strip()
    tags = normalize_tags(raw.get("tags", []))
    source_id = 0
    try:
        source_id = int(raw.get("source_id", 0) or 0)
    except (TypeError, ValueError):
        source_id = 0
    origin = str(raw.get("origin", "codex")).strip() or "codex"

    if ntype == "basic" and (not front or not back):
        return False, {}
    if ntype == "cloze" and not cloze:
        return False, {}

    return True, {
        "type": ntype,
        "front": front,
        "back": back,
        "cloze": cloze,
        "extra": extra,
        "tags": tags,
        "source_id": source_id,
        "origin": origin,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "anki_bot.db"), help="Path to SQLite DB.")
    parser.add_argument("--user-id", type=int, required=True, help="Telegram user id.")
    parser.add_argument("--input", required=True, help="JSON file with notes.")
    parser.add_argument(
        "--mark-processed",
        dest="mark_processed",
        action="store_true",
        help="Mark linked source_ids as processed (default).",
    )
    parser.add_argument(
        "--no-mark-processed",
        dest="mark_processed",
        action="store_false",
        help="Do not update source status.",
    )
    parser.set_defaults(mark_processed=True)
    return parser.parse_args()


def main():
    args = parse_args()
    candidate_notes = load_notes(args.input)

    inserted = 0
    skipped = 0
    processed_source_ids = set()
    with db_conn(args.db_path) as conn:
        ensure_schema(conn)
        ensure_user(conn, args.user_id)
        for idx, raw in enumerate(candidate_notes, start=1):
            if not isinstance(raw, dict):
                skipped += 1
                continue
            ok, note = validate_note(raw)
            if not ok:
                skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO notes (user_id, type, front, back, cloze, extra, tags, source_id, origin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    args.user_id,
                    note["type"],
                    note["front"],
                    note["back"],
                    note["cloze"],
                    note["extra"],
                    json.dumps(note["tags"], ensure_ascii=False),
                    note["source_id"],
                    note["origin"],
                ),
            )
            inserted += 1
            if note["source_id"] > 0:
                processed_source_ids.add(note["source_id"])

        if args.mark_processed and processed_source_ids:
            for source_id in processed_source_ids:
                conn.execute(
                    "UPDATE sources SET status='processed' WHERE id=? AND user_id=?",
                    (source_id, args.user_id),
                )
        conn.commit()

    print(f"Inserted {inserted} notes; skipped {skipped}.")
    if args.mark_processed:
        print(f"Marked {len(processed_source_ids)} source(s) as processed.")


if __name__ == "__main__":
    main()
