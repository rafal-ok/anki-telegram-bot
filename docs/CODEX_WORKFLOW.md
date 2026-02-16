# Codex-First Workflow

This app now supports a source-first flow so you can avoid paid API calls for most work.

## 1) Run in Codex mode

In `.env`:

```env
CARD_GENERATION_BACKEND=codex-ui-queue
```

In this mode, Telegram inputs are stored as pending `sources`; cards are not auto-generated.

## 2) Ingest sources from Telegram

Send any of these:
- plain text
- photos
- documents
- URLs

Use `/queue` to see pending source IDs.
For text-only queue items, you can now draft in-bot proposals directly:

- `/propose_source <source_id>`
- `/propose_pending [limit]`

For unattended hourly proposal generation (Codex automation), run:

```bash
. .venv/bin/activate && python scripts/process_pending_proposals.py --max-sources-per-user 5
```

## 3) Export pending sources for Codex

```bash
python scripts/export_pending_sources.py --user-id <YOUR_TELEGRAM_USER_ID>
```

This produces `exports/pending_sources_user_<id>_<timestamp>.jsonl`.

## 4) Ask Codex to draft cards

Use `prompts/codex_source_to_cards_prompt.md` as the instruction template and give Codex the JSONL file.

Save the result as JSON, for example: `exports/codex_cards_user_<id>.json`.

Expected format:

```json
{
  "notes": [
    {
      "type": "basic",
      "front": "Question",
      "back": "Answer",
      "cloze": "",
      "extra": "source context",
      "tags": ["topic-tag"],
      "source_id": 123,
      "origin": "codex"
    }
  ]
}
```

## 5) Import cards into SQLite

```bash
python scripts/import_codex_cards.py --user-id <YOUR_TELEGRAM_USER_ID> --input exports/codex_cards_user_<id>.json
```

This inserts notes and marks linked sources as `processed`.

## 6) Export for review or study

- `/export` for `.apkg`
- `/export_csv` for CSV
- `/export_audit` for JSONL with source-to-note lineage

## 7) Optional Mochi sync

Configure key/deck once:

- `/mochi_setkey <api_key>` (or set `MOCHI_API_KEY` in `.env`)
- `/mochi_setdeck <deck_id>` (or set `MOCHI_DECK_ID` in `.env`)
- `/mochi_decks` to list available deck IDs

Run sync:

- `/sync_mochi_push` (local -> Mochi)
- `/sync_mochi_pull` (Mochi -> local)
- `/sync_mochi` (2-way: push then pull)

Each sync creates a DB snapshot in `BACKUP_DIR` first.

## Where paid API credits are beneficial

Use `CARD_GENERATION_BACKEND=openai-api` if you want:
- immediate card generation in chat (no batch step)
- stronger OCR/extraction for noisy images
- lower latency for high-volume ingestion

Keep `codex-ui-queue` mode for low-cost, auditable batch pipelines.
