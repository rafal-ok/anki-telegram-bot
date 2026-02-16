#!/usr/bin/env python3
"""Export pending sources from SQLite into JSONL for Codex-assisted card drafting."""

import argparse
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List


def db_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def get_pending_sources(conn: sqlite3.Connection, user_id: int, limit: int) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, source_type, source_label, content_text, file_path, url, meta, status, created_at
        FROM sources
        WHERE user_id=? AND status='pending'
        ORDER BY id
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        meta_raw = row[6] or ""
        try:
            parsed_meta = json.loads(meta_raw) if meta_raw else {}
        except json.JSONDecodeError:
            parsed_meta = {}
        out.append(
            {
                "id": row[0],
                "source_type": row[1],
                "source_label": row[2],
                "content_text": row[3],
                "file_path": row[4],
                "url": row[5],
                "meta": parsed_meta,
                "status": row[7],
                "created_at": row[8],
            }
        )
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "anki_bot.db"), help="Path to SQLite DB.")
    parser.add_argument("--user-id", type=int, required=True, help="Telegram user id.")
    parser.add_argument("--limit", type=int, default=200, help="Max pending sources to export.")
    parser.add_argument("--output", default="", help="Output JSONL path (default: exports/...).")
    return parser.parse_args()


def main():
    args = parse_args()
    with db_conn(args.db_path) as conn:
        rows = get_pending_sources(conn, args.user_id, args.limit)

    if not args.output:
        os.makedirs("exports", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = os.path.join("exports", f"pending_sources_user_{args.user_id}_{ts}.jsonl")

    with open(args.output, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Exported {len(rows)} pending sources to {args.output}")


if __name__ == "__main__":
    main()
