#!/usr/bin/env python3
"""Generate Telegram proposal messages from pending text sources."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import telegram_anki_mochi_bot as app


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--max-sources-per-user", type=int, default=5, help="Maximum pending text sources per user.")
    p.add_argument(
        "--allow-no-message-id",
        action="store_true",
        help="Keep proposals even when Telegram sendMessage fails.",
    )
    p.add_argument("--dry-run", action="store_true", help="Do not write to DB or send Telegram messages.")
    return p.parse_args()


def pending_proposal_count(conn, user_id: int, source_id: int) -> int:
    cur = conn.execute(
        """
        SELECT COUNT(*)
        FROM note_proposals
        WHERE user_id=? AND source_id=? AND status='pending'
        """,
        (user_id, source_id),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def user_ids_with_pending_text_sources(conn) -> List[int]:
    cur = conn.execute(
        """
        SELECT DISTINCT user_id
        FROM sources
        WHERE status='pending' AND TRIM(content_text) <> ''
        ORDER BY user_id
        """
    )
    return [int(r[0]) for r in cur.fetchall()]


def send_proposal_message(chat_id: int, text: str) -> int:
    url = f"https://api.telegram.org/bot{app.TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {payload}")
    result = payload.get("result") or {}
    message_id = int(result.get("message_id") or 0)
    if message_id <= 0:
        raise RuntimeError(f"Telegram sendMessage returned no message_id: {payload}")
    return message_id


def main() -> int:
    args = parse_args()
    max_per_user = max(1, args.max_sources_per_user)
    summary: Dict[str, int] = {
        "users_scanned": 0,
        "sources_scanned": 0,
        "sources_skipped_existing_pending": 0,
        "sources_with_no_proposals": 0,
        "sources_with_proposals": 0,
        "proposals_inserted": 0,
        "messages_sent": 0,
        "message_failures": 0,
    }
    engines: Dict[str, int] = {}

    with app.db_conn() as conn:
        user_ids = user_ids_with_pending_text_sources(conn)
        summary["users_scanned"] = len(user_ids)
        for user_id in user_ids:
            sources = app.get_pending_text_sources(conn, user_id=user_id, limit=max_per_user)
            for source in sources:
                source_id = int(source["id"])
                summary["sources_scanned"] += 1
                if pending_proposal_count(conn, user_id=user_id, source_id=source_id) > 0:
                    summary["sources_skipped_existing_pending"] += 1
                    continue

                text = (source.get("content_text") or "").strip()
                if not text:
                    summary["sources_with_no_proposals"] += 1
                    continue

                notes, engine = app.generate_proposal_notes(text, app.ANKI_LANG, app.PROPOSAL_MAX_NOTES)
                engines[engine] = engines.get(engine, 0) + 1
                if not notes:
                    summary["sources_with_no_proposals"] += 1
                    continue

                proposal_ids: List[int] = []
                if not args.dry_run:
                    for note in notes[: max(1, app.PROPOSAL_MAX_NOTES)]:
                        proposal_ids.append(
                            app.add_note_proposal(conn, user_id=user_id, source_id=source_id, note=note, telegram_message_id=0)
                        )
                    app.set_source_status(conn, user_id=user_id, source_id=source_id, status="processed")

                summary["sources_with_proposals"] += 1
                summary["proposals_inserted"] += len(proposal_ids) if not args.dry_run else len(notes)

                if args.dry_run:
                    continue

                for pid, note in zip(proposal_ids, notes[: len(proposal_ids)]):
                    try:
                        msg_id = send_proposal_message(chat_id=user_id, text=app.format_proposal_message(pid, note))
                        app.set_note_proposal_message_id(conn, user_id=user_id, proposal_id=pid, message_id=msg_id)
                        summary["messages_sent"] += 1
                    except Exception:
                        summary["message_failures"] += 1
                        if not args.allow_no_message_id:
                            app.set_proposal_decision(conn, user_id=user_id, proposal_id=pid, status="expired")

    print(json.dumps({"summary": summary, "engines": engines}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
