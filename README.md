# Telegram -> Anki/Mochi Bot (Codex-first + Auditable Sources)

This bot captures study material from Telegram and stores it as structured `sources` + `notes` in SQLite.
It is designed for:

- easy card creation from different source types (text, photos, documents, URLs)
- complete source-to-card lineage for AI-assisted audits
- Codex-first automation (avoid paid API calls by default)

## Modes

Set `CARD_GENERATION_BACKEND` in `.env`:

- `codex-ui-queue` (default): queue sources, generate cards later with Codex workflows.
- `openai-api`: generate cards immediately from text/photos using OpenAI API.

## Features

- Source capture:
  - Telegram text messages
  - Telegram photos
  - Telegram documents (text extraction for text-like files)
  - URL-only messages
- Manual card commands:
  - `/addbasic`
  - `/addcloze`
  - `/propose` (LLM-generated, emoji-approved proposal flow)
  - `/propose_source` (draft from existing source row)
  - `/propose_pending` (batch draft from pending text sources)
- Exports:
  - Anki `.apkg`
  - CSV
  - audit JSONL (`/export_audit`) with source lineage
  - optional Mochi sync (push/pull/2-way)

## Quick start

1. Create a bot with BotFather and copy the token.
1. `cp .env.example .env`
1. Set at least:
   - `TELEGRAM_BOT_TOKEN`
   - `CARD_GENERATION_BACKEND=codex-ui-queue` (recommended)
   - `AUTO_PROPOSE_FROM_TEXT=1` (recommended; plain text messages auto-generate proposals)
1. Install deps:

```bash
pip install -r requirements.txt
```

5. Run:

```bash
python telegram_anki_mochi_bot.py
```

## Telegram commands

- `/start`, `/help`, `/status`
- `/setdeck <name>`
- `/addbasic <front> || <back> [|| <extra>] [|| <tag1 tag2>]`
- `/addcloze <cloze> [|| <extra>] [|| <tag1 tag2>]`
- `/propose <text>` (or reply to text with `/propose`)
- `/feedback <text>` (reply to a proposal message to regenerate proposals with your feedback)
- `/propose_source <source_id>`
- `/propose_pending [limit]`
- `/queue`
- `/source_done <source_id>`
- `/source_ignore <source_id>`
- `/export`
- `/export_csv`
- `/export_audit`
- `/clear`

Mochi (optional):
- `/mochi_setkey <api_key>`
- `/mochi_decks`
- `/mochi_createdeck <name>`
- `/mochi_setdeck <deck_id>`
- `/mochi_status`
- `/export_mochi` (sync-safe push alias)
- `/sync_mochi_push`
- `/sync_mochi_pull`
- `/sync_mochi`
- `/mochi_repair_cards`
- `/backup_db`

## Mochi 2-way sync + backups

- Each sync command creates a SQLite snapshot backup in `BACKUP_DIR` (default `backups/`).
- Sync state is persisted in `mochi_sync` so repeat pushes are idempotent.
- `/sync_mochi_push`: create missing Mochi cards for local notes and link them.
- `/sync_mochi_pull`: import new/changed Mochi cards into local notes.
- `/sync_mochi`: push then pull in one command.
- `/mochi_repair_cards`: recreate linked cards from local note content (useful if old template-linked cards render blank).
- If a linked local note changed after initial push, push reports `local_changed_not_pushed` (Mochi API does not expose card content update in this integration path).

## Emoji approval flow

Use `/propose <text>` to get 1-3 proposed cards as separate messages.
If `CARD_GENERATION_BACKEND=codex-ui-queue` and `AUTO_PROPOSE_FROM_TEXT=1`, plain text messages trigger the same proposal flow automatically (so `/propose` is optional).

- React with `👍` or `✅` on a proposal message: card is saved locally and pushed to Mochi.
- React with `👎` or `❌`: proposal is rejected.
- Reply to a proposal with `feedback ...` (or `/feedback ...`): proposals for that source are regenerated and replaced.
- Feedback and proposal versions are persisted in SQLite (`proposal_feedback` + `note_proposals` revision lineage) and included in `/export_audit`.
- Language behavior:
  - Polish input text -> Polish proposals
  - English input text -> English proposals
  - Explicit override via prefix in text or feedback, e.g. `lang:pl ...`, `lang:en ...`, `[lang=pl] ...`

Proposal generation backend is configurable:

- `PROPOSAL_GENERATION_BACKEND=auto` (default): try `codex-cli`, then `ollama-local-mac`, then heuristic fallback (no paid API calls).
- `PROPOSAL_GENERATION_BACKEND=codex-cli`: Codex CLI only.
- `PROPOSAL_GENERATION_BACKEND=ollama-local-mac`: local Ollama on this machine only.
- `PROPOSAL_GENERATION_BACKEND=openai-api`: OpenAI only.
- `PROPOSAL_GENERATION_BACKEND=heuristic`: non-LLM fallback.

Useful env settings for Codex proposals:
- `PROPOSAL_CODEX_CMD=codex`
- `PROPOSAL_CODEX_MODEL=` (optional override)
- `PROPOSAL_CODEX_TIMEOUT_SEC=90`
- `PROPOSAL_CODEX_CLI_JS=/opt/homebrew/lib/node_modules/@openai/codex/dist/cli.js` (legacy fallback path)
- `PROPOSAL_CODEX_STRICT_WORKSPACE_ONLY=1` (recommended; force Codex to run with project-only context and no extra sandbox permissions)
- `PROPOSAL_CODEX_ALLOW_LEGACY_FALLBACK=0` (recommended; avoids older less-safe fallback mode)

Hourly automation command (Codex UI):

```bash
. .venv/bin/activate && python scripts/process_pending_proposals.py --max-sources-per-user 5
```

## Codex UI vs Codex CLI

- Codex UI automation: scheduler/orchestrator. It decides **when** recurring work runs (for example, hourly queue processing).
- Codex CLI: generation engine used by this app when `PROPOSAL_GENERATION_BACKEND=codex-cli`.

In practice:
- Real-time Telegram proposal flow (`/propose` and auto-propose from plain text) calls Codex CLI directly.
- A Codex UI automation can run scripts on a schedule (for example `scripts/process_pending_proposals.py`), and those scripts then call Codex CLI for generation.

Provider naming in this repo:
- `codex-ui-queue`: queue-first mode intended to pair with Codex UI automations or manual Codex batch runs.
- `codex-cli`: local command invocation of Codex CLI from this bot process.
- `openai-api`: direct OpenAI API calls from this bot process.
- `ollama-local-mac`: direct local Ollama HTTP endpoint on this machine.

## Codex-first workflow

Use the documented workflow in:

- `docs/CODEX_WORKFLOW.md`

Helper scripts:

- `scripts/export_pending_sources.py`
- `scripts/import_codex_cards.py`
- `scripts/process_pending_proposals.py` (generate proposal messages from pending text sources)

Prompt template:

- `prompts/codex_source_to_cards_prompt.md`

## Where paid API credits help

Switch to `CARD_GENERATION_BACKEND=openai-api` when you want:

- immediate in-chat generation instead of batch workflows
- better OCR/understanding for difficult images
- faster throughput for high-volume ingestion

Keep `codex-ui-queue` mode for low-cost, auditable pipelines.

## Database

Default DB path: `anki_bot.db` (configurable with `DB_PATH`).

Main tables:

- `sources`: every ingested item, plus status (`pending`/`processed`/`ignored`)
- `notes`: cards, each with `source_id` and `origin`
- `mochi_sync`: local note <-> Mochi card mapping and sync hashes

This gives deterministic source lineage for audits and model-quality checks.

## Docker (optional)

```bash
docker build -t telegram-anki-mochi-bot .
docker run --env-file .env --name anki-bot --rm -it telegram-anki-mochi-bot
```

## Systemd (optional)

Edit `systemd/telegram-anki-mochi-bot.service` then:

```bash
sudo cp systemd/telegram-anki-mochi-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-anki-mochi-bot
```
