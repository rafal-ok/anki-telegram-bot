#!/usr/bin/env python3
"""
Telegram -> Anki/Mochi Bot (Codex-first ingestion + optional OpenAI generation)
===============================================================================

This bot can ingest study sources from Telegram (text, photo, document, URL),
store them in SQLite for traceable audits, and export cards to Anki/Mochi.

Generation modes:
- CARD_GENERATION_BACKEND=codex-ui-queue (default): queue-first workflow (Codex UI automation friendly).
- CARD_GENERATION_BACKEND=openai-api: generate cards immediately from text/photos using OpenAI API.
"""

import base64
import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import genanki
import requests
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image
from telegram import InputFile, ReactionTypeEmoji, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, MessageReactionHandler, filters

# -------------------------
# Config
# -------------------------
load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "1" if default else "0") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _canonical_env_choice(name: str, aliases: Dict[str, str], default: str) -> str:
    raw = (os.getenv(name, default) or "").strip().lower()
    if not raw:
        return default
    return aliases.get(raw, default)


CARD_BACKEND_ALIASES = {
    "codex": "codex-ui-queue",
    "codex-ui": "codex-ui-queue",
    "codex-ui-queue": "codex-ui-queue",
    "openai": "openai-api",
    "openai-api": "openai-api",
}

PROPOSAL_BACKEND_ALIASES = {
    "auto": "auto",
    "codex": "codex-cli",
    "codex-cli": "codex-cli",
    "ollama": "ollama-local-mac",
    "ollama-local": "ollama-local-mac",
    "ollama-local-mac": "ollama-local-mac",
    "openai": "openai-api",
    "openai-api": "openai-api",
    "heuristic": "heuristic",
}


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")
ANKI_LANG = os.getenv("ANKI_LANG", "en")
DEFAULT_DECK_NAME = os.getenv("DEFAULT_DECK_NAME", "Telegram Imports")
DB_PATH = os.getenv("DB_PATH", "anki_bot.db")
EXPORT_DIR = os.getenv("EXPORT_DIR", "exports")
SOURCE_DIR = os.getenv("SOURCE_DIR", "sources")
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
CARD_GENERATION_BACKEND = _canonical_env_choice("CARD_GENERATION_BACKEND", CARD_BACKEND_ALIASES, "codex-ui-queue")
PROPOSAL_GENERATION_BACKEND = _canonical_env_choice(
    "PROPOSAL_GENERATION_BACKEND",
    PROPOSAL_BACKEND_ALIASES,
    "auto",
)
PROPOSAL_MAX_NOTES = _int_env("PROPOSAL_MAX_NOTES", 3)
PROPOSAL_CODEX_CMD = (os.getenv("PROPOSAL_CODEX_CMD", "codex") or "codex").strip()
PROPOSAL_CODEX_MODEL = (os.getenv("PROPOSAL_CODEX_MODEL", "") or "").strip()
PROPOSAL_CODEX_TIMEOUT_SEC = _int_env("PROPOSAL_CODEX_TIMEOUT_SEC", 90)
PROPOSAL_CODEX_CLI_JS = (
    os.getenv("PROPOSAL_CODEX_CLI_JS", "/opt/homebrew/lib/node_modules/@openai/codex/dist/cli.js") or ""
).strip()
PROPOSAL_CODEX_STRICT_WORKSPACE_ONLY = (os.getenv("PROPOSAL_CODEX_STRICT_WORKSPACE_ONLY", "1").strip() != "0")
PROPOSAL_CODEX_ALLOW_LEGACY_FALLBACK = (os.getenv("PROPOSAL_CODEX_ALLOW_LEGACY_FALLBACK", "0").strip() == "1")
PROPOSAL_OLLAMA_MODEL = os.getenv("PROPOSAL_OLLAMA_MODEL", "llama3.2:3b")
PROPOSAL_OLLAMA_BASE_URL = os.getenv("PROPOSAL_OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
PROPOSAL_OLLAMA_TIMEOUT_SEC = _int_env("PROPOSAL_OLLAMA_TIMEOUT_SEC", 120)
AUTO_PROPOSE_FROM_TEXT = _bool_env("AUTO_PROPOSE_FROM_TEXT", True)
MAX_SOURCE_TEXT_CHARS = _int_env("MAX_SOURCE_TEXT_CHARS", 12000)
MAX_DOCUMENT_BYTES = _int_env("MAX_DOCUMENT_BYTES", 8_000_000)
MOCHI_SYNC_FETCH_LIMIT = _int_env("MOCHI_SYNC_FETCH_LIMIT", 100)
MOCHI_DEFAULT_API_KEY = (os.getenv("MOCHI_API_KEY", "") or "").strip()
MOCHI_DEFAULT_DECK_ID = (os.getenv("MOCHI_DECK_ID", "") or "").strip()

if not TELEGRAM_BOT_TOKEN:
    print("[ERROR] Missing TELEGRAM_BOT_TOKEN.")
    sys.exit(1)

if CARD_GENERATION_BACKEND not in {"codex-ui-queue", "openai-api"}:
    CARD_GENERATION_BACKEND = "codex-ui-queue"

if CARD_GENERATION_BACKEND == "openai-api" and not OPENAI_API_KEY:
    print("[WARN] OPENAI_API_KEY missing; falling back to CARD_GENERATION_BACKEND=codex-ui-queue.")
    CARD_GENERATION_BACKEND = "codex-ui-queue"

if PROPOSAL_GENERATION_BACKEND not in {"auto", "codex-cli", "ollama-local-mac", "openai-api", "heuristic"}:
    PROPOSAL_GENERATION_BACKEND = "auto"

client: Optional[OpenAI] = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("telegram-anki-mochi-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(SOURCE_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
PROJECT_ROOT = str(Path(__file__).resolve().parent)

# -------------------------
# Database (SQLite)
# -------------------------
SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  deck_name TEXT NOT NULL DEFAULT '',
  mochi_api_key TEXT NOT NULL DEFAULT '',
  mochi_deck_id TEXT NOT NULL DEFAULT ''
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
CREATE TABLE IF NOT EXISTS note_proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  source_id INTEGER NOT NULL DEFAULT 0,
  parent_proposal_id INTEGER NOT NULL DEFAULT 0,
  root_proposal_id INTEGER NOT NULL DEFAULT 0,
  revision_index INTEGER NOT NULL DEFAULT 0,
  feedback_id INTEGER NOT NULL DEFAULT 0,
  type TEXT NOT NULL CHECK(type in ('basic','cloze')),
  front TEXT NOT NULL DEFAULT '',
  back TEXT NOT NULL DEFAULT '',
  cloze TEXT NOT NULL DEFAULT '',
  extra TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  telegram_message_id INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','expired')),
  note_id INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  decided_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS proposal_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  source_id INTEGER NOT NULL DEFAULT 0,
  target_proposal_id INTEGER NOT NULL DEFAULT 0,
  feedback_text TEXT NOT NULL DEFAULT '',
  requested_lang TEXT NOT NULL DEFAULT '',
  telegram_message_id INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS mochi_sync (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  note_id INTEGER NOT NULL,
  mochi_card_id TEXT NOT NULL,
  mochi_deck_id TEXT NOT NULL DEFAULT '',
  local_hash TEXT NOT NULL DEFAULT '',
  remote_hash TEXT NOT NULL DEFAULT '',
  remote_updated_at TEXT NOT NULL DEFAULT '',
  last_synced_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(user_id, note_id),
  UNIQUE(user_id, mochi_card_id)
);
CREATE INDEX IF NOT EXISTS idx_notes_user_id ON notes(user_id);
CREATE INDEX IF NOT EXISTS idx_sources_user_status_id ON sources(user_id, status, id);
CREATE INDEX IF NOT EXISTS idx_note_proposals_user_status_id ON note_proposals(user_id, status, id);
CREATE INDEX IF NOT EXISTS idx_note_proposals_user_msg ON note_proposals(user_id, telegram_message_id);
CREATE INDEX IF NOT EXISTS idx_proposal_feedback_user_source_id ON proposal_feedback(user_id, source_id, id);
CREATE INDEX IF NOT EXISTS idx_proposal_feedback_user_target_id ON proposal_feedback(user_id, target_proposal_id, id);
CREATE INDEX IF NOT EXISTS idx_mochi_sync_user_note ON mochi_sync(user_id, note_id);
CREATE INDEX IF NOT EXISTS idx_mochi_sync_user_card ON mochi_sync(user_id, mochi_card_id);
"""


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def ensure_col(conn: sqlite3.Connection, table: str, name: str, ddl: str):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        conn.commit()
    except sqlite3.OperationalError:
        pass


with db_conn() as conn:
    conn.executescript(SCHEMA_SQL)
    ensure_col(conn, "users", "mochi_api_key", "TEXT NOT NULL DEFAULT ''")
    ensure_col(conn, "users", "mochi_deck_id", "TEXT NOT NULL DEFAULT ''")
    ensure_col(conn, "notes", "source_id", "INTEGER NOT NULL DEFAULT 0")
    ensure_col(conn, "notes", "origin", "TEXT NOT NULL DEFAULT 'unknown'")
    ensure_col(conn, "note_proposals", "parent_proposal_id", "INTEGER NOT NULL DEFAULT 0")
    ensure_col(conn, "note_proposals", "root_proposal_id", "INTEGER NOT NULL DEFAULT 0")
    ensure_col(conn, "note_proposals", "revision_index", "INTEGER NOT NULL DEFAULT 0")
    ensure_col(conn, "note_proposals", "feedback_id", "INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_note_proposals_user_root ON note_proposals(user_id, root_proposal_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_note_proposals_user_source ON note_proposals(user_id, source_id, id)")
    conn.commit()

# -------------------------
# Utilities
# -------------------------
URL_ONLY_RE = re.compile(r"^\s*(https?://\S+)\s*$", re.IGNORECASE)
SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._-]+")
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".rst"}
TEXT_MIME_TYPES = {
    "application/json",
    "application/x-yaml",
    "application/yaml",
    "application/csv",
    "application/xml",
}
PROPOSAL_APPROVE_EMOJIS = {"👍", "✅", "🔥", "💚", "🟢"}
PROPOSAL_REJECT_EMOJIS = {"👎", "❌", "🗑️"}
PROPOSAL_FEEDBACK_RE = re.compile(r"^\s*feedback\b[:\s-]*(.*)$", re.IGNORECASE | re.DOTALL)
LANGUAGE_ALIASES = {
    "pl": "pl",
    "polish": "pl",
    "polski": "pl",
    "polsku": "pl",
    "en": "en",
    "english": "en",
    "angielski": "en",
    "angielsku": "en",
}
POLISH_STOPWORDS = {
    "i",
    "oraz",
    "albo",
    "to",
    "jest",
    "czy",
    "jak",
    "jaki",
    "jaka",
    "jakie",
    "co",
    "kto",
    "gdzie",
    "kiedy",
    "dlaczego",
    "w",
    "na",
    "do",
    "z",
    "od",
    "roku",
    "ktory",
    "która",
    "ktore",
    "który",
    "która",
    "które",
    "się",
}
ENGLISH_STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "what",
    "which",
    "when",
    "where",
    "why",
    "how",
    "who",
    "in",
    "on",
    "at",
    "to",
    "of",
    "for",
    "from",
    "did",
    "does",
    "year",
    "start",
    "end",
}
LANG_DIRECTIVE_BRACKET_RE = re.compile(r"^\s*\[(?:lang|language)\s*[:=]\s*([A-Za-z-]{2,20})\]\s*(.*)$", re.IGNORECASE | re.DOTALL)
LANG_DIRECTIVE_PREFIX_RE = re.compile(
    r"^\s*(?:lang|language)\s*[:=]\s*([A-Za-z-]{2,20})\s*(?:[,;:-]\s*|\s+)(.*)$",
    re.IGNORECASE | re.DOTALL,
)
LANG_WORD_PREFIX_RE = re.compile(
    r"^\s*(pl|en|polish|english|polski|angielski)\s*(?:[,;:-]\s*|\s+)(.*)$",
    re.IGNORECASE | re.DOTALL,
)
LANG_PHRASE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:in\s+(english|polish))|(?:po\s+(angielsku|polsku)))\s*(?:[,;:-]\s*|\s+)?(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def card_backend_label(mode: str) -> str:
    if mode == "codex-ui-queue":
        return "codex-ui-queue (queue/orchestrator mode)"
    if mode == "openai-api":
        return "openai-api (immediate generation mode)"
    return mode


def proposal_backend_label(mode: str) -> str:
    labels = {
        "auto": "auto (codex-cli -> ollama-local-mac -> heuristic)",
        "codex-cli": "codex-cli",
        "ollama-local-mac": "ollama-local-mac",
        "openai-api": "openai-api",
        "heuristic": "heuristic",
    }
    return labels.get(mode, mode)


def normalize_tags(tags: List[str]) -> List[str]:
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


def guess_mime(img: Image.Image) -> str:
    fmt = (img.format or "").upper()
    if fmt == "PNG":
        return "image/png"
    if fmt == "WEBP":
        return "image/webp"
    return "image/jpeg"


def image_to_data_url(image_bytes: bytes) -> str:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        mime = guess_mime(img)
    except Exception:
        mime = "image/jpeg"
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def safe_file_name(name: str) -> str:
    clean = SAFE_FILE_RE.sub("_", (name or "").strip())
    clean = clean.strip("._")
    return clean or "source"


def persist_source_bytes(user_id: int, suggested_name: str, payload: bytes) -> str:
    user_dir = os.path.join(SOURCE_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    base_name = safe_file_name(suggested_name)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.abspath(os.path.join(user_dir, f"{timestamp}_{base_name}"))
    with open(path, "wb") as f:
        f.write(payload)
    return path


def decode_text_bytes(payload: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return payload.decode(enc)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="ignore")


def extract_text_from_document(file_name: str, mime_type: str, payload: bytes) -> str:
    ext = Path(file_name or "").suffix.lower()
    mime = (mime_type or "").lower()
    looks_text = ext in TEXT_EXTENSIONS or mime.startswith("text/") or mime in TEXT_MIME_TYPES
    if not looks_text:
        return ""
    text = decode_text_bytes(payload).strip()
    if not text:
        return ""
    return text[:MAX_SOURCE_TEXT_CHARS]


def parse_tags_segment(raw: str) -> List[str]:
    if not raw:
        return []
    parts = re.split(r"[,\s]+", raw.strip())
    return normalize_tags([p for p in parts if p])


def split_pipe_args(raw: str) -> List[str]:
    parts = [p.strip() for p in raw.split("||")]
    while parts and not parts[-1]:
        parts.pop()
    return parts


def normalize_lang_code(raw: str) -> str:
    token = re.sub(r"\s+", " ", (raw or "").strip().lower()).replace("_", "-")
    if not token:
        return ""
    if token in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[token]
    if re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", token):
        return token[:2]
    return ""


def extract_language_directive(text: str) -> Tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""

    m = LANG_DIRECTIVE_BRACKET_RE.match(raw)
    if m:
        lang = normalize_lang_code(m.group(1))
        if lang:
            return lang, (m.group(2) or "").strip()

    m = LANG_DIRECTIVE_PREFIX_RE.match(raw)
    if m:
        lang = normalize_lang_code(m.group(1))
        if lang:
            return lang, (m.group(2) or "").strip()

    m = LANG_WORD_PREFIX_RE.match(raw)
    if m:
        lang = normalize_lang_code(m.group(1))
        if lang:
            return lang, (m.group(2) or "").strip()

    m = LANG_PHRASE_PREFIX_RE.match(raw)
    if m:
        lang = normalize_lang_code(m.group(1) or m.group(2) or "")
        if lang:
            return lang, (m.group(3) or "").strip()

    return "", raw


def detect_language_from_text(text: str, default_lang: str = ANKI_LANG) -> str:
    raw = (text or "").strip()
    if not raw:
        return default_lang
    t = raw.lower()
    pl_score = 0
    en_score = 0

    if any(ch in "ąćęłńóśżź" for ch in t):
        pl_score += 3

    words = re.findall(r"[a-zA-Ząćęłńóśżź]+", t)
    for w in words:
        if w in POLISH_STOPWORDS:
            pl_score += 1
        if w in ENGLISH_STOPWORDS:
            en_score += 1

    if pl_score == 0 and en_score == 0:
        return default_lang
    if pl_score > en_score:
        return "pl"
    if en_score > pl_score:
        return "en"
    return default_lang


def resolve_proposal_language(text: str, feedback: str = "", default_lang: str = ANKI_LANG) -> Tuple[str, str, str]:
    feedback_lang, cleaned_feedback = extract_language_directive(feedback)
    text_lang, cleaned_text = extract_language_directive(text)
    candidate_text = (cleaned_text or text or "").strip()
    candidate_feedback = (cleaned_feedback or feedback or "").strip()

    if feedback_lang:
        return feedback_lang, candidate_text, candidate_feedback
    if text_lang:
        return text_lang, candidate_text, candidate_feedback
    return detect_language_from_text(candidate_text, default_lang), candidate_text, candidate_feedback


def format_note_previews(notes: List[Dict[str, Any]], limit: int = 4) -> str:
    lines: List[str] = []
    for n in notes[:limit]:
        if n["type"] == "cloze":
            lines.append(f"- CLOZE: {n.get('cloze', '')[:220]}")
        else:
            lines.append(f"- BASIC: {n.get('front', '')[:120]} -> {n.get('back', '')[:120]}")
    if len(notes) > limit:
        lines.append(f"...and {len(notes) - limit} more")
    return "\n".join(lines)


def _line_to_basic_candidate(line: str) -> Optional[Dict[str, Any]]:
    s = line.strip()
    if not s:
        return None
    q_idx = s.find("?")
    if q_idx != -1:
        front = s[: q_idx + 1].strip()
        back = s[q_idx + 1 :].strip(" -:;\t")
        if front and back:
            return {"type": "basic", "front": front, "back": back, "cloze": "", "extra": "", "tags": []}
    m = re.match(r"^(.+?)\s*(?:->|=>|:|-)\s*(.+)$", s)
    if m:
        front = m.group(1).strip()
        back = m.group(2).strip()
        if front and back:
            return {"type": "basic", "front": front, "back": back, "cloze": "", "extra": "", "tags": []}
    return None


def heuristic_propose_notes_from_text(text: str, lang: str = ANKI_LANG) -> List[Dict[str, Any]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    notes: List[Dict[str, Any]] = []

    for ln in lines:
        cand = _line_to_basic_candidate(ln)
        if cand:
            cand["extra"] = f"proposal ({lang})"
            notes.append(cand)
        if len(notes) >= 3:
            return notes

    if notes:
        return notes

    merged = " ".join(lines)[:MAX_SOURCE_TEXT_CHARS].strip()
    if not merged:
        return []

    year_match = re.search(r"\b\d{3,4}\b", merged)
    if year_match:
        cloze = merged[: year_match.start()] + "{{c1::" + year_match.group(0) + "}}" + merged[year_match.end() :]
        return [{"type": "cloze", "front": "", "back": "", "cloze": cloze, "extra": f"proposal ({lang})", "tags": []}]

    words = merged.split()
    if len(words) >= 4:
        phrase = " ".join(words[:2])
        cloze = merged.replace(phrase, "{{c1::" + phrase + "}}", 1)
        return [{"type": "cloze", "front": "", "back": "", "cloze": cloze, "extra": f"proposal ({lang})", "tags": []}]

    term = merged.strip(" .,:;!?")
    if term:
        return [
            {
                "type": "basic",
                "front": f"What is {term}?",
                "back": merged,
                "cloze": "",
                "extra": f"proposal ({lang})",
                "tags": [],
            }
        ]
    return []


def clean_candidate_notes(notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    clean: List[Dict[str, Any]] = []
    for n in notes:
        ntype = (n.get("type") or "basic").lower()
        front = (n.get("front") or "").strip()
        back = (n.get("back") or "").strip()
        cloze = (n.get("cloze") or "").strip()
        extra = (n.get("extra") or "").strip()
        tags = n.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        tags_clean = normalize_tags([t for t in tags if t and isinstance(t, str)])
        if ntype == "cloze":
            if cloze:
                clean.append(
                    {
                        "type": "cloze",
                        "front": "",
                        "back": back,
                        "cloze": cloze,
                        "extra": extra,
                        "tags": tags_clean,
                    }
                )
        else:
            if front and back:
                clean.append(
                    {
                        "type": "basic",
                        "front": front,
                        "back": back,
                        "cloze": "",
                        "extra": extra,
                        "tags": tags_clean,
                    }
                )
    return clean


def proposal_prompt(lang: str, max_notes: int, feedback: str = "") -> str:
    base = f"""
Generate high-quality Anki proposal cards from input text.
Output JSON only in this format:
{{"notes":[{{"type":"basic|cloze","front":"","back":"","cloze":"","extra":"","tags":[]}}]}}
Return 1 to {max_notes} notes.

Rules:
- Use language: {lang}
- If input is a short term/phrase (about 1-4 words), produce 2 complementary cards when possible:
  1) a direct definition card ("What is X?")
  2) a role/category card ("What is represented by X?" or "X is a unit of what?")
- Prefer basic cards for direct Q/A facts.
- Prefer cloze cards when preserving an important sentence helps recall.
- If input is a single term, produce at least one useful definition-style basic card.
- For factoid prompts (dates, names, starts/ends), make at least one direct atomic card with a short answer.
- If a concept is associated with a named source/work, include that anchor in the front when helpful
  (example style: "In Deep Utopia, what question becomes central in a solved world?").
- Front must be specific and answerable; never use placeholders.
- Keep each front/back concise, testable, and easy to answer quickly.
- Prefer simpler wording in answers over abstract phrasing.
- `extra` is optional and short.
- Tags: 1-3 short kebab-case topical tags when possible.
- Never return blank front/back for basic cards.
- Never return blank cloze text for cloze cards.
- Never use generic fronts like "Recall this fact".
- Do not output commentary, markdown, or code fences.
- If input is too vague for a good card, return empty notes.
""".strip()
    fb = (feedback or "").strip()
    if not fb:
        return base
    return (
        base
        + "\n\nRevision instructions from user feedback (apply strictly when possible):\n"
        + fb
    )


def extract_json_payload(text: str) -> Optional[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    if "```" in raw:
        for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE):
            block = (m.group(1) or "").strip()
            if block:
                candidates.append(block)
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        start = cand.find("{")
        while start != -1:
            depth = 0
            for idx in range(start, len(cand)):
                ch = cand[idx]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        piece = cand[start : idx + 1]
                        try:
                            obj = json.loads(piece)
                            if isinstance(obj, dict):
                                return obj
                        except Exception:
                            break
            start = cand.find("{", start + 1)
    return None


def _run_codex_exec_prompt(prompt: str) -> Tuple[int, str, str]:
    with tempfile.TemporaryDirectory(prefix="codex_propose_") as tmp_dir:
        out_path = os.path.join(tmp_dir, "last_message.txt")
        cmd = [
            PROPOSAL_CODEX_CMD,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--cd",
            PROJECT_ROOT,
            "--output-last-message",
            out_path,
        ]
        if PROPOSAL_CODEX_STRICT_WORKSPACE_ONLY:
            # Force Codex to run without extra sandbox permissions (notably full-disk read access).
            cmd.extend(["-c", "sandbox_permissions=[]"])
        if PROPOSAL_CODEX_MODEL:
            cmd.extend(["-m", PROPOSAL_CODEX_MODEL])
        cmd.append(prompt)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(15, PROPOSAL_CODEX_TIMEOUT_SEC),
            check=False,
        )
        last_message = ""
        if os.path.exists(out_path):
            try:
                with open(out_path, "r", encoding="utf-8") as f:
                    last_message = f.read().strip()
            except Exception:
                last_message = ""
        combined = "\n".join(
            x for x in [(proc.stdout or "").strip(), (proc.stderr or "").strip(), last_message] if x
        ).strip()
        return proc.returncode, combined, last_message


def _run_codex_legacy_prompt(prompt: str) -> Tuple[int, str]:
    cmd = [PROPOSAL_CODEX_CMD, "-q", "-C", PROJECT_ROOT]
    if PROPOSAL_CODEX_STRICT_WORKSPACE_ONLY:
        cmd.extend(["-c", "sandbox_permissions=[]"])
    if PROPOSAL_CODEX_MODEL:
        cmd.extend(["-m", PROPOSAL_CODEX_MODEL])
    cmd.append(prompt)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(15, PROPOSAL_CODEX_TIMEOUT_SEC),
        check=False,
    )
    combined = "\n".join(x for x in [(proc.stdout or "").strip(), (proc.stderr or "").strip()] if x).strip()
    if "Please pass patch text through stdin" not in combined:
        return proc.returncode, combined
    if not PROPOSAL_CODEX_ALLOW_LEGACY_FALLBACK:
        return proc.returncode, combined
    # Older broken codex builds may incorrectly trigger an apply-patch branch.
    if not PROPOSAL_CODEX_CLI_JS or not os.path.exists(PROPOSAL_CODEX_CLI_JS) or shutil.which("node") is None:
        return proc.returncode, combined
    script = (
        "process.argv=['node','x','-q'];"
        "if(process.env.CODEX_MODEL){process.argv.push('-m',process.env.CODEX_MODEL);}"
        "process.argv.push(process.env.CODEX_PROMPT||'');"
        f"import({json.dumps(PROPOSAL_CODEX_CLI_JS)});"
    )
    env = os.environ.copy()
    env["CODEX_PROMPT"] = prompt
    env["CODEX_MODEL"] = PROPOSAL_CODEX_MODEL
    proc2 = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=max(15, PROPOSAL_CODEX_TIMEOUT_SEC),
        check=False,
        env=env,
    )
    combined2 = "\n".join(x for x in [(proc2.stdout or "").strip(), (proc2.stderr or "").strip()] if x).strip()
    return proc2.returncode, combined2 or combined


def codex_generate_proposal_notes_from_text(
    text: str,
    lang: str = ANKI_LANG,
    max_notes: int = PROPOSAL_MAX_NOTES,
    feedback: str = "",
) -> List[Dict[str, Any]]:
    if not PROPOSAL_CODEX_CMD:
        return []

    prompt = f"{proposal_prompt(lang, max_notes, feedback)}\n\nInput text:\n{text}"
    try:
        code, combined, last_message = _run_codex_exec_prompt(prompt)
    except FileNotFoundError:
        logger.warning("Codex CLI not found for proposal generation: %s", PROPOSAL_CODEX_CMD)
        return []
    except subprocess.TimeoutExpired:
        logger.warning("Codex proposal generation timed out after %ss", PROPOSAL_CODEX_TIMEOUT_SEC)
        return []
    except Exception as e:
        logger.warning("Codex proposal generation failed: %s", e)
        return []

    lowered = combined.lower()
    if "unrecognized subcommand" in lowered or "unexpected argument 'exec'" in lowered:
        try:
            code, combined = _run_codex_legacy_prompt(prompt)
            last_message = ""
        except Exception as e:
            logger.warning("Codex legacy fallback failed: %s", e)
            return []

    # Prefer the explicit last message file when available; it is cleaner than CLI logs.
    parse_candidates = [last_message, combined]
    for chunk in parse_candidates:
        obj = extract_json_payload(chunk)
        if not obj:
            continue
        notes = obj.get("notes", [])
        if not isinstance(notes, list):
            continue
        clean = clean_candidate_notes(notes)[: max(1, max_notes)]
        if clean:
            return clean

    if code != 0:
        logger.warning("Codex proposal generation failed (exit=%s)", code)
    else:
        logger.warning("Codex proposal generation returned no valid note JSON.")
    return []


def ollama_generate_proposal_notes_from_text(
    text: str,
    lang: str = ANKI_LANG,
    max_notes: int = PROPOSAL_MAX_NOTES,
    feedback: str = "",
) -> List[Dict[str, Any]]:
    payload = {
        "model": PROPOSAL_OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "prompt": f"{proposal_prompt(lang, max_notes, feedback)}\n\nInput text:\n{text}",
        "options": {"temperature": 0.2},
    }
    try:
        r = requests.post(
            f"{PROPOSAL_OLLAMA_BASE_URL}/api/generate",
            json=payload,
            timeout=PROPOSAL_OLLAMA_TIMEOUT_SEC,
        )
        r.raise_for_status()
        body = r.json()
        raw = body.get("response")
        if isinstance(raw, dict):
            obj = raw
        else:
            obj = extract_json_payload(str(raw or ""))
        if not obj:
            return []
        notes = obj.get("notes", [])
        if not isinstance(notes, list):
            return []
        return clean_candidate_notes(notes)[: max(1, max_notes)]
    except Exception as e:
        logger.warning("Ollama propose generation failed: %s", e)
        return []


def openai_generate_proposal_notes_from_text(
    text: str,
    lang: str = ANKI_LANG,
    max_notes: int = PROPOSAL_MAX_NOTES,
    feedback: str = "",
) -> List[Dict[str, Any]]:
    if client is None:
        return []
    try:
        content: List[Dict[str, Any]] = [
            {"type": "input_text", "text": proposal_prompt(lang, max_notes, feedback)},
            {"type": "input_text", "text": text},
        ]
        resp = client.responses.create(
            model=OPENAI_VISION_MODEL,
            input=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": CARD_SCHEMA["name"],
                    "schema": CARD_SCHEMA["schema"],
                    "strict": True,
                }
            },
            temperature=0.2,
            max_output_tokens=900,
        )
        return _parse_notes(resp)[: max(1, max_notes)]
    except Exception as e:
        logger.warning("OpenAI propose generation failed: %s", e)
        return []


def generate_proposal_notes(
    text: str,
    lang: str = ANKI_LANG,
    max_notes: int = PROPOSAL_MAX_NOTES,
    feedback: str = "",
) -> Tuple[List[Dict[str, Any]], str]:
    backend = PROPOSAL_GENERATION_BACKEND
    if backend in {"auto", "codex-cli"}:
        notes = codex_generate_proposal_notes_from_text(text, lang, max_notes, feedback)
        if notes:
            return notes, "codex-cli"
        if backend == "codex-cli":
            return [], "codex-cli-unavailable"

    if backend in {"auto", "ollama-local-mac"}:
        notes = ollama_generate_proposal_notes_from_text(text, lang, max_notes, feedback)
        if notes:
            return notes, "ollama-local-mac"
        if backend == "ollama-local-mac":
            return [], "ollama-local-mac-unavailable"

    if backend == "openai-api":
        notes = openai_generate_proposal_notes_from_text(text, lang, max_notes, feedback)
        if notes:
            return notes, "openai-api"
        return [], "openai-api-unavailable"

    if backend == "heuristic":
        notes = heuristic_propose_notes_from_text(text, lang)[: max(1, max_notes)]
        return notes, "heuristic"

    if backend == "auto":
        notes = heuristic_propose_notes_from_text(text, lang)[: max(1, max_notes)]
        if notes:
            return notes, "heuristic_fallback"

    return [], "no_llm_backend"


def _trim_for_telegram(value: str, limit: int = 900) -> str:
    txt = (value or "").strip()
    if len(txt) <= limit:
        return txt
    return txt[: max(1, limit - 3)].rstrip() + "..."


def format_proposal_message(proposal_id: int, note: Dict[str, Any]) -> str:
    tags = " ".join(note.get("tags", []))
    if note["type"] == "cloze":
        body = (
            f"Proposal #{proposal_id}\n"
            f"Type: CLOZE\n"
            f"Cloze: {_trim_for_telegram(note.get('cloze',''), 1500)}\n"
            f"Extra: {_trim_for_telegram(note.get('extra','') or '(none)', 300)}\n"
            f"Tags: {tags or '(none)'}"
        )
    else:
        body = (
            f"Proposal #{proposal_id}\n"
            f"Type: BASIC\n"
            f"Front: {_trim_for_telegram(note.get('front',''), 800)}\n"
            f"Back: {_trim_for_telegram(note.get('back',''), 1200)}\n"
            f"Extra: {_trim_for_telegram(note.get('extra','') or '(none)', 300)}\n"
            f"Tags: {tags or '(none)'}"
        )
    return (
        body
        + "\n\nReact with 👍/✅ to approve+sync, or 👎/❌ to reject."
        + "\nReply with `feedback ...` (or `/feedback ...`) to revise."
    )


def stable_json_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def note_hash(note: Dict[str, Any]) -> str:
    payload = {
        "type": note.get("type", "basic"),
        "front": note.get("front", ""),
        "back": note.get("back", ""),
        "cloze": note.get("cloze", ""),
        "extra": note.get("extra", ""),
        "tags": normalize_tags(note.get("tags", []) if isinstance(note.get("tags"), list) else []),
    }
    return stable_json_hash(payload)


def mochi_card_hash(card: Dict[str, Any]) -> str:
    payload = {
        "content": card.get("content", ""),
        "deck-id": card.get("deck-id", ""),
        "tags": card.get("tags", []),
    }
    return stable_json_hash(payload)


def snapshot_db_backup(user_id: int, reason: str) -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_name = f"db_backup_u{user_id}_{safe_file_name(reason)}_{stamp}.sqlite3"
    out_path = os.path.abspath(os.path.join(BACKUP_DIR, out_name))
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as src:
        with sqlite3.connect(out_path) as dst:
            src.backup(dst)
    return out_path


# -------------------------
# DB helpers
# -------------------------
def ensure_user(conn: sqlite3.Connection, user_id: int):
    cur = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if not cur.fetchone():
        conn.execute(
            "INSERT INTO users (user_id, deck_name) VALUES (?, ?)",
            (user_id, DEFAULT_DECK_NAME),
        )
        conn.commit()


def set_deck_name(conn: sqlite3.Connection, user_id: int, name: str):
    ensure_user(conn, user_id)
    conn.execute("UPDATE users SET deck_name=? WHERE user_id=?", (name, user_id))
    conn.commit()


def get_deck_name(conn: sqlite3.Connection, user_id: int) -> str:
    ensure_user(conn, user_id)
    cur = conn.execute("SELECT deck_name FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] else DEFAULT_DECK_NAME


def set_mochi_api_key(conn: sqlite3.Connection, user_id: int, key: str):
    ensure_user(conn, user_id)
    conn.execute("UPDATE users SET mochi_api_key=? WHERE user_id=?", (key, user_id))
    conn.commit()


def set_mochi_deck_id(conn: sqlite3.Connection, user_id: int, deck_id: str):
    ensure_user(conn, user_id)
    conn.execute("UPDATE users SET mochi_deck_id=? WHERE user_id=?", (deck_id, user_id))
    conn.commit()


def get_user_row(conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
    ensure_user(conn, user_id)
    cur = conn.execute(
        "SELECT user_id, deck_name, mochi_api_key, mochi_deck_id FROM users WHERE user_id=?",
        (user_id,),
    )
    row = cur.fetchone()
    mochi_api_key = (row[2] or "").strip() or MOCHI_DEFAULT_API_KEY
    mochi_deck_id = (row[3] or "").strip() or MOCHI_DEFAULT_DECK_ID
    return {
        "user_id": row[0],
        "deck_name": row[1],
        "mochi_api_key": mochi_api_key,
        "mochi_deck_id": mochi_deck_id,
    }


def add_source(
    conn: sqlite3.Connection,
    user_id: int,
    source_type: str,
    source_label: str = "",
    content_text: str = "",
    file_path: str = "",
    url: str = "",
    meta: Optional[Dict[str, Any]] = None,
    status: str = "pending",
) -> int:
    ensure_user(conn, user_id)
    if status not in {"pending", "processed", "ignored"}:
        status = "pending"
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO sources (user_id, source_type, source_label, content_text, file_path, url, meta, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            source_type,
            source_label[:160],
            content_text[:MAX_SOURCE_TEXT_CHARS],
            file_path,
            url,
            meta_json,
            status,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_source_status(conn: sqlite3.Connection, user_id: int, source_id: int, status: str) -> bool:
    if status not in {"pending", "processed", "ignored"}:
        return False
    cur = conn.execute(
        "UPDATE sources SET status=? WHERE id=? AND user_id=?",
        (status, source_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_user_sources(
    conn: sqlite3.Connection,
    user_id: int,
    limit: int = 2000,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    args: List[Any] = [user_id]
    where = "WHERE user_id=?"
    if status:
        where += " AND status=?"
        args.append(status)
    args.append(limit)
    cur = conn.execute(
        f"""
        SELECT id, source_type, source_label, content_text, file_path, url, meta, status, created_at
        FROM sources
        {where}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(args),
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


def get_source_by_id(conn: sqlite3.Connection, user_id: int, source_id: int) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, source_type, source_label, content_text, file_path, url, meta, status, created_at
        FROM sources
        WHERE user_id=? AND id=?
        LIMIT 1
        """,
        (user_id, source_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    meta_raw = row[6] or ""
    try:
        parsed_meta = json.loads(meta_raw) if meta_raw else {}
    except json.JSONDecodeError:
        parsed_meta = {}
    return {
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


def get_pending_text_sources(conn: sqlite3.Connection, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, source_type, source_label, content_text, file_path, url, meta, status, created_at
        FROM sources
        WHERE user_id=? AND status='pending' AND TRIM(content_text) <> ''
        ORDER BY id ASC
        LIMIT ?
        """,
        (user_id, max(1, limit)),
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


def pending_source_count(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM sources WHERE user_id=? AND status='pending'", (user_id,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def add_note(
    conn: sqlite3.Connection,
    user_id: int,
    note_type: str,
    front: str,
    back: str,
    cloze: str,
    extra: str,
    tags: List[str],
    source_id: int = 0,
    origin: str = "manual",
) -> int:
    ensure_user(conn, user_id)
    tags_json = json.dumps(normalize_tags(tags), ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO notes (user_id, type, front, back, cloze, extra, tags, source_id, origin)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            note_type,
            front,
            back,
            cloze,
            extra,
            tags_json,
            source_id,
            origin,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_user_notes(conn: sqlite3.Connection, user_id: int) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, type, front, back, cloze, extra, tags, source_id, origin, created_at
        FROM notes
        WHERE user_id=?
        ORDER BY id
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "type": r[1],
                "front": r[2],
                "back": r[3],
                "cloze": r[4],
                "extra": r[5],
                "tags": json.loads(r[6]) if r[6] else [],
                "source_id": int(r[7] or 0),
                "origin": r[8] or "unknown",
                "created_at": r[9],
            }
        )
    return out


def count_user_notes(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM notes WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def clear_user_notes(conn: sqlite3.Connection, user_id: int) -> int:
    cur = conn.execute("DELETE FROM notes WHERE user_id=?", (user_id,))
    conn.commit()
    return cur.rowcount


def get_note_by_id(conn: sqlite3.Connection, user_id: int, note_id: int) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, type, front, back, cloze, extra, tags, source_id, origin, created_at
        FROM notes
        WHERE user_id=? AND id=?
        LIMIT 1
        """,
        (user_id, note_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "type": row[1],
        "front": row[2],
        "back": row[3],
        "cloze": row[4],
        "extra": row[5],
        "tags": json.loads(row[6]) if row[6] else [],
        "source_id": int(row[7] or 0),
        "origin": row[8] or "unknown",
        "created_at": row[9],
    }


def update_note_fields(
    conn: sqlite3.Connection,
    user_id: int,
    note_id: int,
    note_type: str,
    front: str,
    back: str,
    cloze: str,
    extra: str,
    tags: List[str],
    origin: str,
):
    conn.execute(
        """
        UPDATE notes
        SET type=?, front=?, back=?, cloze=?, extra=?, tags=?, origin=?
        WHERE id=? AND user_id=?
        """,
        (
            note_type,
            front,
            back,
            cloze,
            extra,
            json.dumps(normalize_tags(tags), ensure_ascii=False),
            origin,
            note_id,
            user_id,
        ),
    )
    conn.commit()


def get_mochi_sync_rows(conn: sqlite3.Connection, user_id: int) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT note_id, mochi_card_id, mochi_deck_id, local_hash, remote_hash, remote_updated_at, last_synced_at
        FROM mochi_sync
        WHERE user_id=?
        ORDER BY id
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "note_id": int(r[0]),
                "mochi_card_id": r[1],
                "mochi_deck_id": r[2],
                "local_hash": r[3],
                "remote_hash": r[4],
                "remote_updated_at": r[5],
                "last_synced_at": r[6],
            }
        )
    return out


def link_mochi_sync(
    conn: sqlite3.Connection,
    user_id: int,
    note_id: int,
    mochi_card_id: str,
    mochi_deck_id: str,
    local_hash: str,
    remote_hash: str,
    remote_updated_at: str,
):
    conn.execute(
        "DELETE FROM mochi_sync WHERE user_id=? AND (note_id=? OR mochi_card_id=?)",
        (user_id, note_id, mochi_card_id),
    )
    conn.execute(
        """
        INSERT INTO mochi_sync (
          user_id, note_id, mochi_card_id, mochi_deck_id, local_hash, remote_hash, remote_updated_at, last_synced_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            user_id,
            note_id,
            mochi_card_id,
            mochi_deck_id,
            local_hash,
            remote_hash,
            remote_updated_at,
        ),
    )
    conn.commit()


def remove_mochi_sync_by_card(conn: sqlite3.Connection, user_id: int, mochi_card_id: str):
    conn.execute("DELETE FROM mochi_sync WHERE user_id=? AND mochi_card_id=?", (user_id, mochi_card_id))
    conn.commit()


def add_note_proposal(
    conn: sqlite3.Connection,
    user_id: int,
    source_id: int,
    note: Dict[str, Any],
    telegram_message_id: int = 0,
    parent_proposal_id: int = 0,
    root_proposal_id: int = 0,
    revision_index: int = 0,
    feedback_id: int = 0,
) -> int:
    ensure_user(conn, user_id)
    cur = conn.execute(
        """
        INSERT INTO note_proposals (
          user_id, source_id, parent_proposal_id, root_proposal_id, revision_index, feedback_id,
          type, front, back, cloze, extra, tags, telegram_message_id, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            user_id,
            source_id,
            max(0, int(parent_proposal_id or 0)),
            max(0, int(root_proposal_id or 0)),
            max(0, int(revision_index or 0)),
            max(0, int(feedback_id or 0)),
            note.get("type", "basic"),
            note.get("front", ""),
            note.get("back", ""),
            note.get("cloze", ""),
            note.get("extra", ""),
            json.dumps(normalize_tags(note.get("tags", [])), ensure_ascii=False),
            telegram_message_id,
        ),
    )
    conn.commit()
    proposal_id = int(cur.lastrowid)
    if int(root_proposal_id or 0) <= 0:
        conn.execute(
            "UPDATE note_proposals SET root_proposal_id=? WHERE id=? AND user_id=?",
            (proposal_id, proposal_id, user_id),
        )
        conn.commit()
    return proposal_id


def set_note_proposal_message_id(conn: sqlite3.Connection, user_id: int, proposal_id: int, message_id: int):
    conn.execute(
        "UPDATE note_proposals SET telegram_message_id=? WHERE id=? AND user_id=?",
        (message_id, proposal_id, user_id),
    )
    conn.commit()


def get_pending_proposal_by_message(
    conn: sqlite3.Connection,
    user_id: int,
    message_id: int,
) -> Optional[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, source_id, parent_proposal_id, root_proposal_id, revision_index, feedback_id,
               type, front, back, cloze, extra, tags, status
        FROM note_proposals
        WHERE user_id=? AND telegram_message_id=? AND status='pending'
        LIMIT 1
        """,
        (user_id, message_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "source_id": int(row[1] or 0),
        "parent_proposal_id": int(row[2] or 0),
        "root_proposal_id": int(row[3] or 0),
        "revision_index": int(row[4] or 0),
        "feedback_id": int(row[5] or 0),
        "type": row[6],
        "front": row[7],
        "back": row[8],
        "cloze": row[9],
        "extra": row[10],
        "tags": json.loads(row[11]) if row[11] else [],
        "status": row[12],
    }


def add_proposal_feedback(
    conn: sqlite3.Connection,
    user_id: int,
    source_id: int,
    target_proposal_id: int,
    feedback_text: str,
    requested_lang: str,
    telegram_message_id: int = 0,
) -> int:
    ensure_user(conn, user_id)
    cur = conn.execute(
        """
        INSERT INTO proposal_feedback (
          user_id, source_id, target_proposal_id, feedback_text, requested_lang, telegram_message_id
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            max(0, int(source_id or 0)),
            max(0, int(target_proposal_id or 0)),
            (feedback_text or "").strip(),
            (requested_lang or "").strip(),
            max(0, int(telegram_message_id or 0)),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_user_proposals(conn: sqlite3.Connection, user_id: int, limit: int = 20000) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, source_id, parent_proposal_id, root_proposal_id, revision_index, feedback_id,
               type, front, back, cloze, extra, tags, telegram_message_id, status, note_id, created_at, decided_at
        FROM note_proposals
        WHERE user_id=?
        ORDER BY id
        LIMIT ?
        """,
        (user_id, max(1, limit)),
    )
    rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": int(r[0]),
                "source_id": int(r[1] or 0),
                "parent_proposal_id": int(r[2] or 0),
                "root_proposal_id": int(r[3] or 0),
                "revision_index": int(r[4] or 0),
                "feedback_id": int(r[5] or 0),
                "type": r[6],
                "front": r[7],
                "back": r[8],
                "cloze": r[9],
                "extra": r[10],
                "tags": json.loads(r[11]) if r[11] else [],
                "telegram_message_id": int(r[12] or 0),
                "status": r[13],
                "note_id": int(r[14] or 0),
                "created_at": r[15],
                "decided_at": r[16],
            }
        )
    return out


def get_user_proposal_feedback(conn: sqlite3.Connection, user_id: int, limit: int = 20000) -> List[Dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, source_id, target_proposal_id, feedback_text, requested_lang, telegram_message_id, created_at
        FROM proposal_feedback
        WHERE user_id=?
        ORDER BY id
        LIMIT ?
        """,
        (user_id, max(1, limit)),
    )
    rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": int(r[0]),
                "source_id": int(r[1] or 0),
                "target_proposal_id": int(r[2] or 0),
                "feedback_text": r[3] or "",
                "requested_lang": r[4] or "",
                "telegram_message_id": int(r[5] or 0),
                "created_at": r[6],
            }
        )
    return out


def set_proposal_decision(
    conn: sqlite3.Connection,
    user_id: int,
    proposal_id: int,
    status: str,
    note_id: int = 0,
):
    if status not in {"approved", "rejected", "expired"}:
        return
    conn.execute(
        "UPDATE note_proposals SET status=?, note_id=?, decided_at=datetime('now') WHERE id=? AND user_id=?",
        (status, note_id, proposal_id, user_id),
    )
    conn.commit()


def expire_pending_proposals_for_source(conn: sqlite3.Connection, user_id: int, source_id: int) -> int:
    cur = conn.execute(
        """
        UPDATE note_proposals
        SET status='expired', note_id=0, decided_at=datetime('now')
        WHERE user_id=? AND source_id=? AND status='pending'
        """,
        (user_id, source_id),
    )
    conn.commit()
    return int(cur.rowcount or 0)


# -------------------------
# OpenAI - card generation (optional)
# -------------------------
CARD_SCHEMA = {
    "name": "anki_cards_batch",
    "schema": {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["basic", "cloze"]},
                        "front": {"type": "string", "description": "Front (ignored for cloze)", "default": ""},
                        "back": {"type": "string", "description": "Back / explanation", "default": ""},
                        "cloze": {"type": "string", "description": "Text with {{c1::...}} clozes", "default": ""},
                        "extra": {"type": "string", "description": "One-sentence note / source", "default": ""},
                        "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                    },
                    "required": ["type", "front", "back", "cloze", "extra", "tags"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["notes"],
        "additionalProperties": False,
    },
    "strict": True,
}


def anki_prompt(lang: str) -> str:
    return (
        f"""
You generate compact, testable Anki-style cards from an image or text.
Return 1-3 high-quality cards. Prefer Cloze for prose/definitions; use Basic for Q->A.

Quality rules:
- Short, crisp fronts. Put the precise answer on the back. Add a 1-line rationale in `extra`.
- Add 1-3 topical tags (kebab-case, no spaces). Always use language: {lang}.
- If the image shows underlined/highlighted phrases, guarantee at least one card per concept and add tag `underlined`.
- If there is nothing testable, return an empty `notes` array.
""".strip()
    )


def _parse_notes(resp: Any) -> List[Dict[str, Any]]:
    notes: List[Dict[str, Any]] = []
    try:
        if getattr(resp, "output_parsed", None):
            parsed = resp.output_parsed
            notes = parsed.get("notes", [])
        else:
            text = resp.output_text
            data = json.loads(text)
            notes = data.get("notes", [])
    except Exception:
        try:
            first_text = resp.output[0].content[0].text  # type: ignore[index]
            data = json.loads(first_text)
            notes = data.get("notes", [])
        except Exception as e:
            logger.exception("Parsing failure: %s", e)
            return []
    return clean_candidate_notes(notes)


def openai_generate_notes_from_image(image_data_url: str, lang: str = ANKI_LANG) -> List[Dict[str, Any]]:
    if client is None:
        return []
    try:
        resp = client.responses.create(
            model=OPENAI_VISION_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": anki_prompt(lang)},
                        {"type": "input_image", "image_url": image_data_url},
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": CARD_SCHEMA["name"],
                    "schema": CARD_SCHEMA["schema"],
                    "strict": True,
                }
            },
            temperature=0.1,
            max_output_tokens=900,
        )
        return _parse_notes(resp)
    except Exception as e:
        logger.exception("OpenAI error (image): %s", e)
        return []


def openai_generate_notes_from_text(text: str, lang: str = ANKI_LANG) -> List[Dict[str, Any]]:
    if client is None:
        return []
    try:
        resp = client.responses.create(
            model=OPENAI_VISION_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": anki_prompt(lang)},
                        {"type": "input_text", "text": text},
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": CARD_SCHEMA["name"],
                    "schema": CARD_SCHEMA["schema"],
                    "strict": True,
                }
            },
            temperature=0.1,
            max_output_tokens=900,
        )
        return _parse_notes(resp)
    except Exception as e:
        logger.exception("OpenAI error (text): %s", e)
        return []


def store_generated_notes(
    conn: sqlite3.Connection,
    user_id: int,
    source_id: int,
    notes: List[Dict[str, Any]],
    origin: str,
) -> int:
    created = 0
    for n in notes:
        add_note(
            conn,
            user_id=user_id,
            note_type=n["type"],
            front=n.get("front", ""),
            back=n.get("back", ""),
            cloze=n.get("cloze", ""),
            extra=n.get("extra", ""),
            tags=n.get("tags", []),
            source_id=source_id,
            origin=origin,
        )
        created += 1
    return created


# -------------------------
# Building Anki deck (.apkg) / CSV / Audit JSONL
# -------------------------
BASIC_MODEL_ID = 1607392320
CLOZE_MODEL_ID = 1607392319

basic_model = genanki.Model(
    BASIC_MODEL_ID,
    "Basic (Front/Back/Extra)",
    fields=[
        {"name": "Front"},
        {"name": "Back"},
        {"name": "Extra"},
    ],
    templates=[
        {
            "name": "Card 1",
            "qfmt": "{{Front}}",
            "afmt": "{{Front}}<hr id=answer>{{Back}}<br>{{Extra}}",
        }
    ],
)

cloze_model = genanki.Model(
    CLOZE_MODEL_ID,
    "Cloze (Text + Extra)",
    fields=[
        {"name": "Text"},
        {"name": "Extra"},
    ],
    templates=[
        {
            "name": "Cloze",
            "qfmt": "{{cloze:Text}}",
            "afmt": "{{cloze:Text}}<hr id=answer>{{Extra}}",
        }
    ],
    model_type=genanki.Model.CLOZE,
)


def stable_deck_id(user_id: int) -> int:
    random.seed(user_id)
    return random.randint(10**9, 2**31 - 1)


def build_apkg(user_id: int, deck_name: str, notes: List[Dict[str, Any]]) -> str:
    deck = genanki.Deck(stable_deck_id(user_id), deck_name)
    for n in notes:
        if n["type"] == "cloze":
            fields = [n.get("cloze", ""), n.get("extra", "")]
            note = genanki.Note(model=cloze_model, fields=fields, tags=n.get("tags", []))
        else:
            fields = [n.get("front", ""), n.get("back", ""), n.get("extra", "")]
            note = genanki.Note(model=basic_model, fields=fields, tags=n.get("tags", []))
        deck.add_note(note)
    out_name = f"{deck_name.replace(' ', '_')}_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.apkg"
    out_path = os.path.join(EXPORT_DIR, out_name)
    pkg = genanki.Package(deck)
    pkg.write_to_file(out_path)
    return out_path


def build_csv(user_id: int, deck_name: str, notes: List[Dict[str, Any]]) -> str:
    import csv

    out_name = f"{deck_name.replace(' ', '_')}_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    out_path = os.path.join(EXPORT_DIR, out_name)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["type", "front", "back", "cloze", "extra", "tags", "source_id", "origin", "created_at"])
        for n in notes:
            tags = " ".join([t for t in n.get("tags", [])])
            writer.writerow(
                [
                    n.get("type", "basic"),
                    n.get("front", ""),
                    n.get("back", ""),
                    n.get("cloze", ""),
                    n.get("extra", ""),
                    tags,
                    n.get("source_id", 0),
                    n.get("origin", "unknown"),
                    n.get("created_at", ""),
                ]
            )
    return out_path


def build_audit_jsonl(
    user_id: int,
    deck_name: str,
    notes: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    proposals: Optional[List[Dict[str, Any]]] = None,
    feedback_log: Optional[List[Dict[str, Any]]] = None,
) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"audit_user_{user_id}_{ts}.jsonl"
    out_path = os.path.join(EXPORT_DIR, out_name)
    source_by_id = {int(s["id"]): s for s in sources}
    linked_source_ids = {int(n.get("source_id", 0)) for n in notes if int(n.get("source_id", 0)) > 0}
    proposals = proposals or []
    feedback_log = feedback_log or []

    with open(out_path, "w", encoding="utf-8") as f:
        meta = {
            "record_type": "meta",
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "user_id": user_id,
            "deck_name": deck_name,
            "notes_count": len(notes),
            "sources_count": len(sources),
            "proposals_count": len(proposals),
            "proposal_feedback_count": len(feedback_log),
        }
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")

        for n in notes:
            sid = int(n.get("source_id", 0) or 0)
            record = {
                "record_type": "note",
                "note": n,
                "source": source_by_id.get(sid),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        for s in sources:
            sid = int(s["id"])
            if sid in linked_source_ids:
                continue
            record = {
                "record_type": "source_without_note",
                "source": s,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        for p in proposals:
            sid = int(p.get("source_id", 0) or 0)
            record = {
                "record_type": "proposal",
                "proposal": p,
                "source": source_by_id.get(sid),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        for fb in feedback_log:
            sid = int(fb.get("source_id", 0) or 0)
            record = {
                "record_type": "proposal_feedback",
                "feedback": fb,
                "source": source_by_id.get(sid),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return out_path


# -------------------------
# Mochi API helpers (optional)
# -------------------------
MOCHI_BASE = "https://app.mochi.cards/api"
MOCHI_SEPARATOR_RE = re.compile(r"\n-{3,}\n")


def mochi_auth(key: str):
    return (key, "")  # HTTP Basic Auth: username=API key, no password


def mochi_request(
    method: str,
    key: str,
    path: str,
    *,
    allow_404: bool = False,
    retries: int = 4,
    retry_sleep: float = 0.6,
    **kwargs: Any,
) -> requests.Response:
    url = f"{MOCHI_BASE}{path}"
    for attempt in range(retries + 1):
        resp = requests.request(method, url, auth=mochi_auth(key), timeout=25, **kwargs)
        if resp.status_code // 100 == 2:
            return resp
        if allow_404 and resp.status_code == 404:
            return resp
        if resp.status_code == 429 and attempt < retries:
            time.sleep(retry_sleep * (attempt + 1))
            continue
        resp.raise_for_status()
    return resp


def mochi_list_templates(key: str) -> List[Dict[str, Any]]:
    r = mochi_request("GET", key, "/templates/")
    return r.json().get("docs", [])


def mochi_list_decks(key: str) -> List[Dict[str, Any]]:
    r = mochi_request("GET", key, "/decks/")
    return r.json().get("docs", [])


def mochi_get_deck(key: str, deck_id: str) -> Optional[Dict[str, Any]]:
    if not deck_id:
        return None
    for d in mochi_list_decks(key):
        if d.get("id") == deck_id:
            return d
    return None


def mochi_find_simple_template_id(key: str) -> Optional[str]:
    try:
        docs = mochi_list_templates(key)
        for d in docs:
            if d.get("name") in {"Simple flashcard", "Basic Flashcard"}:
                return d.get("id")
    except Exception as e:
        logger.warning("Could not list templates: %s", e)
    return None


def mochi_create_deck(key: str, name: str) -> str:
    r = mochi_request(
        "POST",
        key,
        "/decks/",
        json={"name": name},
    )
    return r.json()["id"]


def anki_cloze_to_mochi(md: str) -> str:
    pattern = re.compile(r"\{\{c(\d+)::(.*?)\}\}")
    return pattern.sub(lambda m: "{{" + m.group(1) + "::" + m.group(2) + "}}", md)


def mochi_cloze_to_anki(md: str) -> str:
    pattern = re.compile(r"\{\{(\d+)::(.*?)\}\}")
    return pattern.sub(lambda m: "{{c" + m.group(1) + "::" + m.group(2) + "}}", md)


def mochi_note_content(n: Dict[str, Any]) -> str:
    if n["type"] == "cloze":
        base = anki_cloze_to_mochi(n.get("cloze", ""))
        extra = n.get("extra", "")
        content = base
        if extra:
            content += f"\n\n---\n{extra}"
        return content
    front = n.get("front", "")
    back = n.get("back", "")
    extra = n.get("extra", "")
    content = f"{front}\n\n---\n{back}"
    if extra:
        content += f"\n\n{extra}"
    return content


def mochi_extract_tags(card: Dict[str, Any]) -> List[str]:
    tags_raw = card.get("manual-tags") or card.get("tags") or []
    out: List[str] = []
    for t in tags_raw:
        if isinstance(t, str):
            out.append(t)
            continue
        if isinstance(t, dict):
            name = t.get("name") or t.get("id")
            if isinstance(name, str):
                out.append(name)
    return normalize_tags(out)


def mochi_split_content(content: str) -> Tuple[str, str]:
    if not content:
        return "", ""
    m = MOCHI_SEPARATOR_RE.search(content)
    if not m:
        return content.strip(), ""
    left = content[: m.start()].strip()
    right = content[m.end() :].strip()
    return left, right


def mochi_card_updated_at(card: Dict[str, Any]) -> str:
    raw = card.get("updated-at")
    if isinstance(raw, dict):
        date = raw.get("date")
        if isinstance(date, str):
            return date
    if isinstance(raw, str):
        return raw
    return ""


def mochi_card_to_local_note(card: Dict[str, Any]) -> Dict[str, Any]:
    content = (card.get("content") or "").strip()
    title = (card.get("name") or "").strip()
    tags = mochi_extract_tags(card)
    left, right = mochi_split_content(content)

    cloze_zone = left if left else content
    if "{{" in cloze_zone and re.search(r"\{\{\d+::", cloze_zone):
        return {
            "type": "cloze",
            "front": "",
            "back": "",
            "cloze": mochi_cloze_to_anki(cloze_zone),
            "extra": right,
            "tags": tags,
        }

    if right:
        return {
            "type": "basic",
            "front": left or title or "Untitled",
            "back": right,
            "cloze": "",
            "extra": "",
            "tags": tags,
        }

    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return {
            "type": "basic",
            "front": lines[0],
            "back": "\n".join(lines[1:]),
            "cloze": "",
            "extra": "",
            "tags": tags,
        }
    return {
        "type": "basic",
        "front": title or (lines[0] if lines else "Untitled"),
        "back": content if title else "",
        "cloze": "",
        "extra": "",
        "tags": tags,
    }


def mochi_create_card(key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = mochi_request("POST", key, "/cards/", json=payload)
    return r.json()


def mochi_get_card(key: str, card_id: str) -> Optional[Dict[str, Any]]:
    r = mochi_request("GET", key, f"/cards/{card_id}", allow_404=True)
    if r.status_code == 404:
        return None
    return r.json()


def mochi_delete_card(key: str, card_id: str) -> bool:
    r = mochi_request("DELETE", key, f"/cards/{card_id}", allow_404=True)
    return r.status_code // 100 == 2


def mochi_list_cards(key: str, deck_id: str, limit: int = MOCHI_SYNC_FETCH_LIMIT) -> List[Dict[str, Any]]:
    all_docs: List[Dict[str, Any]] = []
    bookmark: Optional[str] = None
    page_size = max(1, min(limit, 200))
    seen_bookmarks = set()

    def _terminal_bookmark(value: Optional[str]) -> bool:
        if not value:
            return True
        if isinstance(value, str) and value.strip().lower() in {"nil", "null", "none"}:
            return True
        return False

    while True:
        params: Dict[str, Any] = {"deck-id": deck_id, "limit": page_size}
        if bookmark and not _terminal_bookmark(bookmark):
            params["bookmark"] = bookmark
        r = mochi_request("GET", key, "/cards/", params=params)
        payload = r.json()
        all_docs.extend(payload.get("docs", []))
        bookmark = payload.get("bookmark")
        if _terminal_bookmark(bookmark) or bookmark in seen_bookmarks:
            break
        seen_bookmarks.add(bookmark)
    return all_docs


def sync_push_to_mochi(
    conn: sqlite3.Connection,
    user_id: int,
    key: str,
    deck_id: str,
    notes: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    notes = notes if notes is not None else get_user_notes(conn, user_id)
    existing_links = get_mochi_sync_rows(conn, user_id)
    link_by_note = {r["note_id"]: r for r in existing_links}

    result = {
        "total_notes": len(notes),
        "created": 0,
        "already_linked": 0,
        "recreated_missing_remote": 0,
        "skipped": 0,
        "local_changed_not_pushed": 0,
    }

    for n in notes:
        note_id = int(n["id"])
        local_hash = note_hash(n)
        row = link_by_note.get(note_id)

        if row:
            remote = mochi_get_card(key, row["mochi_card_id"])
            remote_deck = (remote or {}).get("deck-id", "") if remote else ""
            if remote is None or remote_deck != deck_id:
                payload = {
                    "content": mochi_note_content(n),
                    "deck-id": deck_id,
                    "manual-tags": [t for t in n.get("tags", [])],
                }
                created = mochi_create_card(key, payload)
                link_mochi_sync(
                    conn,
                    user_id=user_id,
                    note_id=note_id,
                    mochi_card_id=created.get("id", ""),
                    mochi_deck_id=deck_id,
                    local_hash=local_hash,
                    remote_hash=mochi_card_hash(created),
                    remote_updated_at=mochi_card_updated_at(created),
                )
                result["recreated_missing_remote"] += 1
                continue

            if row.get("local_hash") and row["local_hash"] != local_hash:
                result["local_changed_not_pushed"] += 1
                local_hash_for_link = row["local_hash"]
            else:
                local_hash_for_link = local_hash

            link_mochi_sync(
                conn,
                user_id=user_id,
                note_id=note_id,
                mochi_card_id=row["mochi_card_id"],
                mochi_deck_id=deck_id,
                local_hash=local_hash_for_link,
                remote_hash=mochi_card_hash(remote),
                remote_updated_at=mochi_card_updated_at(remote),
            )
            result["already_linked"] += 1
            continue

        payload = {
            "content": mochi_note_content(n),
            "deck-id": deck_id,
            "manual-tags": [t for t in n.get("tags", [])],
        }
        created = mochi_create_card(key, payload)
        card_id = created.get("id", "")
        if not card_id:
            result["skipped"] += 1
            continue
        link_mochi_sync(
            conn,
            user_id=user_id,
            note_id=note_id,
            mochi_card_id=card_id,
            mochi_deck_id=deck_id,
            local_hash=local_hash,
            remote_hash=mochi_card_hash(created),
            remote_updated_at=mochi_card_updated_at(created),
        )
        result["created"] += 1

    return result


def sync_pull_from_mochi(
    conn: sqlite3.Connection,
    user_id: int,
    key: str,
    deck_id: str,
) -> Dict[str, Any]:
    cards = mochi_list_cards(key, deck_id)
    links = get_mochi_sync_rows(conn, user_id)
    link_by_card = {r["mochi_card_id"]: r for r in links}
    seen_card_ids = set()

    result = {
        "remote_cards": len(cards),
        "created_local": 0,
        "updated_local": 0,
        "unchanged": 0,
        "removed_stale_links": 0,
    }

    for card in cards:
        card_id = (card.get("id") or "").strip()
        if not card_id:
            continue
        if (card.get("deck-id") or "") != deck_id:
            continue
        seen_card_ids.add(card_id)
        remote_hash = mochi_card_hash(card)
        remote_updated_at = mochi_card_updated_at(card)
        parsed = mochi_card_to_local_note(card)
        link = link_by_card.get(card_id)

        if link:
            note = get_note_by_id(conn, user_id, int(link["note_id"]))
            if not note:
                remove_mochi_sync_by_card(conn, user_id, card_id)
            else:
                if link.get("remote_hash") != remote_hash:
                    update_note_fields(
                        conn,
                        user_id=user_id,
                        note_id=int(note["id"]),
                        note_type=parsed["type"],
                        front=parsed.get("front", ""),
                        back=parsed.get("back", ""),
                        cloze=parsed.get("cloze", ""),
                        extra=parsed.get("extra", ""),
                        tags=parsed.get("tags", []),
                        origin="mochi_pull",
                    )
                    note = get_note_by_id(conn, user_id, int(note["id"]))
                    result["updated_local"] += 1
                else:
                    result["unchanged"] += 1

                if note:
                    link_mochi_sync(
                        conn,
                        user_id=user_id,
                        note_id=int(note["id"]),
                        mochi_card_id=card_id,
                        mochi_deck_id=deck_id,
                        local_hash=note_hash(note),
                        remote_hash=remote_hash,
                        remote_updated_at=remote_updated_at,
                    )
                continue

        source_id = add_source(
            conn,
            user_id=user_id,
            source_type="mochi_pull",
            source_label=(card.get("name") or f"mochi:{card_id}")[:120],
            content_text=(card.get("content") or "")[:MAX_SOURCE_TEXT_CHARS],
            url=f"mochi://card/{card_id}",
            meta={"mochi_card_id": card_id, "mochi_deck_id": deck_id},
            status="processed",
        )
        note_id = add_note(
            conn,
            user_id=user_id,
            note_type=parsed["type"],
            front=parsed.get("front", ""),
            back=parsed.get("back", ""),
            cloze=parsed.get("cloze", ""),
            extra=parsed.get("extra", ""),
            tags=parsed.get("tags", []),
            source_id=source_id,
            origin="mochi_pull",
        )
        note = get_note_by_id(conn, user_id, note_id)
        link_mochi_sync(
            conn,
            user_id=user_id,
            note_id=note_id,
            mochi_card_id=card_id,
            mochi_deck_id=deck_id,
            local_hash=note_hash(note or parsed),
            remote_hash=remote_hash,
            remote_updated_at=remote_updated_at,
        )
        result["created_local"] += 1

    for link in links:
        if link.get("mochi_deck_id") != deck_id:
            continue
        if link["mochi_card_id"] in seen_card_ids:
            continue
        remove_mochi_sync_by_card(conn, user_id, link["mochi_card_id"])
        result["removed_stale_links"] += 1

    return result


def sync_mochi_both(
    conn: sqlite3.Connection,
    user_id: int,
    key: str,
    deck_id: str,
) -> Dict[str, Any]:
    push = sync_push_to_mochi(conn, user_id, key, deck_id)
    pull = sync_pull_from_mochi(conn, user_id, key, deck_id)
    return {"push": push, "pull": pull}


def repair_mochi_cards_from_local(
    conn: sqlite3.Connection,
    user_id: int,
    key: str,
    deck_id: str,
) -> Dict[str, Any]:
    links = get_mochi_sync_rows(conn, user_id)
    result = {
        "checked_links": 0,
        "recreated": 0,
        "missing_local_note": 0,
        "failed_create": 0,
    }
    for row in links:
        if row.get("mochi_deck_id") != deck_id:
            continue
        result["checked_links"] += 1
        note = get_note_by_id(conn, user_id, int(row["note_id"]))
        if not note:
            result["missing_local_note"] += 1
            continue

        old_card_id = row.get("mochi_card_id", "")
        if old_card_id:
            mochi_delete_card(key, old_card_id)

        payload = {
            "content": mochi_note_content(note),
            "deck-id": deck_id,
            "manual-tags": [t for t in note.get("tags", [])],
        }
        created = mochi_create_card(key, payload)
        new_card_id = created.get("id", "")
        if not new_card_id:
            result["failed_create"] += 1
            continue

        link_mochi_sync(
            conn,
            user_id=user_id,
            note_id=int(note["id"]),
            mochi_card_id=new_card_id,
            mochi_deck_id=deck_id,
            local_hash=note_hash(note),
            remote_hash=mochi_card_hash(created),
            remote_updated_at=mochi_card_updated_at(created),
        )
        result["recreated"] += 1
    return result


# -------------------------
# Telegram Handlers
# -------------------------
HELP_TEXT = (
    "Send text, photos, documents, or a URL. I store each source for traceable card audits.\n\n"
    "Tip: when CARD_GENERATION_BACKEND=codex-ui-queue and AUTO_PROPOSE_FROM_TEXT=1, plain text messages auto-create proposals.\n\n"
    "Core commands:\n"
    "/status - show backend mode and queue stats\n"
    "/setdeck <name> - set deck name (default: Telegram Imports)\n"
    "/addbasic <front> || <back> [|| <extra>] [|| <tag1 tag2>] - manual card\n"
    "/addcloze <cloze> [|| <extra>] [|| <tag1 tag2>] - manual cloze card\n"
    "/propose <text> - draft card proposals; approve with emoji reaction\n"
    "/feedback <text> - reply to a proposal message to revise proposals with your feedback\n"
    "  language: detected from input text (PL->PL, EN->EN); override with prefix like 'lang:pl ...' or 'lang:en ...'\n"
    "/propose_source <source_id> - draft from an existing source row\n"
    "/propose_pending [limit] - batch draft from pending text sources\n"
    "/queue - list pending sources\n"
    "/source_done <source_id> - mark a source as processed\n"
    "/source_ignore <source_id> - ignore a source\n"
    "/export - send Anki .apkg\n"
    "/export_csv - send CSV\n"
    "/export_audit - send JSONL with notes + source lineage\n"
    "/clear - delete all stored cards (sources stay)\n\n"
    "Mochi (optional):\n"
    "/mochi_setkey <api_key>\n"
    "/mochi_createdeck <name>\n"
    "/mochi_decks - list your Mochi decks (name + id)\n"
    "/mochi_setdeck <deck_id>\n"
    "/mochi_status\n"
    "/export_mochi - push unsynced local cards to Mochi\n"
    "/sync_mochi_push - local -> Mochi sync with mapping\n"
    "/sync_mochi_pull - Mochi -> local sync with mapping\n"
    "/sync_mochi - run push then pull (2-way)\n"
    "/mochi_repair_cards - recreate linked cards in selected deck (fix blank/template cards)\n"
    "/backup_db - create a DB snapshot backup\n"
)


def _reaction_emojis(items: List[Any]) -> set[str]:
    out: set[str] = set()
    for it in items:
        if isinstance(it, ReactionTypeEmoji):
            out.add(it.emoji)
        else:
            emoji = getattr(it, "emoji", None)
            if isinstance(emoji, str):
                out.add(emoji)
    return out


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        ensure_user(conn, user_id)
    await update.message.reply_text(
        "Ready. Send a source and I will store it for auditable card creation.\n"
        f"Card backend: {card_backend_label(CARD_GENERATION_BACKEND)}.\n"
        f"Proposal backend: {proposal_backend_label(PROPOSAL_GENERATION_BACKEND)}.\n"
        "Use /help for commands."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        note_count = count_user_notes(conn, user_id)
        pending = pending_source_count(conn, user_id)
        deck_name = get_deck_name(conn, user_id)
    await update.message.reply_text(
        f"Card backend: {card_backend_label(CARD_GENERATION_BACKEND)}\n"
        f"Proposal backend: {proposal_backend_label(PROPOSAL_GENERATION_BACKEND)}\n"
        f"Deck: {deck_name}\n"
        f"Cards: {note_count}\n"
        f"Pending sources: {pending}"
    )


async def setdeck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text("Usage: /setdeck <deck name>")
        return
    with db_conn() as conn:
        set_deck_name(conn, user_id, name)
    await update.message.reply_text(f"OK, deck set to: {name}")


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        n = clear_user_notes(conn, user_id)
    await update.message.reply_text(f"Deleted {n} cards.")


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        pending = get_user_sources(conn, user_id=user_id, limit=12, status="pending")
    if not pending:
        await update.message.reply_text("No pending sources.")
        return
    lines = []
    for s in pending:
        label = s.get("source_label") or s.get("url") or s.get("file_path") or s.get("source_type")
        lines.append(f"#{s['id']} [{s['source_type']}] {label[:90]}")
    await update.message.reply_text("Pending sources:\n" + "\n".join(lines))


def _parse_source_id(args: List[str]) -> int:
    if not args:
        return 0
    try:
        return int(args[0])
    except ValueError:
        return 0


async def source_done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    source_id = _parse_source_id(context.args)
    if source_id <= 0:
        await update.message.reply_text("Usage: /source_done <source_id>")
        return
    with db_conn() as conn:
        ok = set_source_status(conn, user_id, source_id, "processed")
    await update.message.reply_text("Marked as processed." if ok else "Source not found.")


async def source_ignore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    source_id = _parse_source_id(context.args)
    if source_id <= 0:
        await update.message.reply_text("Usage: /source_ignore <source_id>")
        return
    with db_conn() as conn:
        ok = set_source_status(conn, user_id, source_id, "ignored")
    await update.message.reply_text("Marked as ignored." if ok else "Source not found.")


async def addbasic_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    raw = " ".join(context.args).strip()
    parts = split_pipe_args(raw)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /addbasic <front> || <back> [|| <extra>] [|| <tag1 tag2>]")
        return
    front = parts[0]
    back = parts[1]
    extra = parts[2] if len(parts) > 2 else ""
    tags = parse_tags_segment(parts[3] if len(parts) > 3 else "")
    with db_conn() as conn:
        source_id = add_source(
            conn,
            user_id=user_id,
            source_type="manual_basic",
            source_label=front[:120],
            content_text=f"front: {front}\nback: {back}\nextra: {extra}",
            meta={"command": "/addbasic"},
            status="processed",
        )
        note_id = add_note(
            conn,
            user_id=user_id,
            note_type="basic",
            front=front,
            back=back,
            cloze="",
            extra=extra,
            tags=tags,
            source_id=source_id,
            origin="manual",
        )
    await update.message.reply_text(f"Added basic card #{note_id} (source #{source_id}).")


async def addcloze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    raw = " ".join(context.args).strip()
    parts = split_pipe_args(raw)
    if len(parts) < 1 or not parts[0]:
        await update.message.reply_text("Usage: /addcloze <cloze> [|| <extra>] [|| <tag1 tag2>]")
        return
    cloze = parts[0]
    extra = parts[1] if len(parts) > 1 else ""
    tags = parse_tags_segment(parts[2] if len(parts) > 2 else "")
    with db_conn() as conn:
        source_id = add_source(
            conn,
            user_id=user_id,
            source_type="manual_cloze",
            source_label=cloze[:120],
            content_text=f"cloze: {cloze}\nextra: {extra}",
            meta={"command": "/addcloze"},
            status="processed",
        )
        note_id = add_note(
            conn,
            user_id=user_id,
            note_type="cloze",
            front="",
            back="",
            cloze=cloze,
            extra=extra,
            tags=tags,
            source_id=source_id,
            origin="manual",
        )
    await update.message.reply_text(f"Added cloze card #{note_id} (source #{source_id}).")


def _extract_proposal_text_from_message(msg: Any) -> str:
    if msg is None:
        return ""
    candidates = [
        getattr(msg, "text", "") or "",
        getattr(msg, "caption", "") or "",
    ]
    for raw in candidates:
        txt = raw.strip()
        if txt:
            return txt[:MAX_SOURCE_TEXT_CHARS]
    return ""


def _extract_feedback_text(raw_text: str) -> str:
    m = PROPOSAL_FEEDBACK_RE.match(raw_text or "")
    if not m:
        return ""
    return (m.group(1) or "").strip()


async def _create_and_send_proposals(
    update: Update,
    user_id: int,
    source_id: int,
    text: str,
    feedback: str = "",
    replace_pending_source_proposals: bool = False,
    parent_proposal_id: int = 0,
    root_proposal_id: int = 0,
    revision_index: int = 0,
    feedback_id: int = 0,
    announce_result: bool = True,
) -> Tuple[int, str]:
    assert update.message is not None
    proposal_lang, proposal_text, proposal_feedback = resolve_proposal_language(text, feedback, ANKI_LANG)
    clipped = proposal_text[:MAX_SOURCE_TEXT_CHARS].strip()
    if not clipped:
        if announce_result:
            await update.message.reply_text("No text was available to generate proposals.")
        return 0, "empty_input"

    notes, engine = generate_proposal_notes(
        clipped,
        proposal_lang,
        PROPOSAL_MAX_NOTES,
        feedback=proposal_feedback,
    )
    if not notes:
        if announce_result:
            await update.message.reply_text(
                "No LLM proposals generated.\n"
                f"Backend={PROPOSAL_GENERATION_BACKEND}; language={proposal_lang}; engine_result={engine}.\n"
                "Set PROPOSAL_GENERATION_BACKEND=codex-cli/ollama-local-mac/openai-api (recommended in this order), "
                "or PROPOSAL_GENERATION_BACKEND=heuristic as a fallback."
            )
        return 0, engine

    with db_conn() as conn:
        if replace_pending_source_proposals and source_id > 0:
            expire_pending_proposals_for_source(conn, user_id=user_id, source_id=source_id)
        proposal_ids: List[int] = []
        for n in notes[: max(1, PROPOSAL_MAX_NOTES)]:
            pid = add_note_proposal(
                conn,
                user_id=user_id,
                source_id=source_id,
                note=n,
                telegram_message_id=0,
                parent_proposal_id=parent_proposal_id,
                root_proposal_id=root_proposal_id,
                revision_index=revision_index,
                feedback_id=feedback_id,
            )
            proposal_ids.append(pid)
        if source_id > 0:
            set_source_status(conn, user_id, source_id, "processed")

    for pid, note in zip(proposal_ids, notes[: len(proposal_ids)]):
        sent = await update.message.reply_text(format_proposal_message(pid, note))
        with db_conn() as conn:
            set_note_proposal_message_id(conn, user_id=user_id, proposal_id=pid, message_id=sent.message_id)

    if announce_result:
        source_bit = f" from source #{source_id}" if source_id > 0 else ""
        await update.message.reply_text(
            f"Posted {len(proposal_ids)} proposal(s){source_bit} via {engine} (lang={proposal_lang}). "
            "React to each proposal message with 👍/✅ to approve+sync or 👎/❌ to reject."
        )
    return len(proposal_ids), engine


async def _handle_proposal_feedback(
    update: Update,
    user_id: int,
    replied_message_id: int,
    feedback_text: str,
    strict: bool = True,
) -> bool:
    assert update.message is not None
    feedback = (feedback_text or "").strip()
    if not feedback:
        if strict:
            await update.message.reply_text("Provide feedback text after 'feedback' (or use /feedback <details>).")
        return strict

    with db_conn() as conn:
        proposal = get_pending_proposal_by_message(conn, user_id=user_id, message_id=replied_message_id)
        if not proposal:
            if strict:
                await update.message.reply_text("Reply to a pending proposal message when sending feedback.")
            return strict
        source_id = int(proposal.get("source_id", 0) or 0)
        if source_id <= 0:
            await update.message.reply_text("That proposal is not linked to a source, so it cannot be revised.")
            return True
        requested_lang, _, _ = resolve_proposal_language("", feedback, ANKI_LANG)
        feedback_id = add_proposal_feedback(
            conn,
            user_id=user_id,
            source_id=source_id,
            target_proposal_id=int(proposal.get("id", 0) or 0),
            feedback_text=feedback,
            requested_lang=requested_lang,
            telegram_message_id=int(update.message.message_id or 0),
        )
        source = get_source_by_id(conn, user_id=user_id, source_id=source_id)

    if not source:
        await update.message.reply_text(f"Source #{source_id} was not found, so I couldn't apply feedback.")
        return True

    source_text = (source.get("content_text") or "").strip()
    if not source_text:
        await update.message.reply_text(f"Source #{source_id} has no text content to revise.")
        return True

    posted, engine = await _create_and_send_proposals(
        update,
        user_id=user_id,
        source_id=source_id,
        text=source_text,
        feedback=feedback,
        replace_pending_source_proposals=True,
        parent_proposal_id=int(proposal.get("id", 0) or 0),
        root_proposal_id=int(proposal.get("root_proposal_id", 0) or 0) or int(proposal.get("id", 0) or 0),
        revision_index=int(proposal.get("revision_index", 0) or 0) + 1,
        feedback_id=feedback_id,
        announce_result=False,
    )
    if posted <= 0:
        await update.message.reply_text(
            f"I couldn't generate revised proposals from your feedback (engine={engine}). "
            "Try more specific feedback."
        )
        return True

    await update.message.reply_text(
        f"Applied feedback to source #{source_id}. Posted {posted} revised proposal(s) via {engine}.\n"
        "React with 👍/✅ to approve+sync or 👎/❌ to reject."
    )
    return True


async def propose_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id

    raw = " ".join(context.args).strip()
    if not raw and update.message.reply_to_message:
        raw = _extract_proposal_text_from_message(update.message.reply_to_message)
    if not raw:
        await update.message.reply_text("Usage: /propose <text> (or reply to a text message with /propose)")
        return

    with db_conn() as conn:
        source_id = add_source(
            conn,
            user_id=user_id,
            source_type="proposal_text",
            source_label=raw[:120],
            content_text=raw[:MAX_SOURCE_TEXT_CHARS],
            meta={"transport": "propose_cmd"},
            status="pending",
        )
    await _create_and_send_proposals(update, user_id=user_id, source_id=source_id, text=raw, announce_result=True)


async def propose_source_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    source_id = _parse_source_id(context.args)
    if source_id <= 0:
        await update.message.reply_text("Usage: /propose_source <source_id>")
        return

    with db_conn() as conn:
        source = get_source_by_id(conn, user_id=user_id, source_id=source_id)
    if not source:
        await update.message.reply_text(f"Source #{source_id} was not found.")
        return

    text = (source.get("content_text") or "").strip()
    if not text:
        await update.message.reply_text(
            f"Source #{source_id} has no text content.\n"
            "Tip: use /propose directly on text, or run Codex workflow for binary/URL sources."
        )
        return

    await _create_and_send_proposals(update, user_id=user_id, source_id=source_id, text=text, announce_result=True)


async def feedback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    if not update.message.reply_to_message:
        await update.message.reply_text("Usage: reply to a proposal message with /feedback <your feedback>")
        return
    feedback = " ".join(context.args).strip()
    if not feedback:
        await update.message.reply_text("Usage: /feedback <your feedback>")
        return
    await _handle_proposal_feedback(
        update,
        user_id=user_id,
        replied_message_id=update.message.reply_to_message.message_id,
        feedback_text=feedback,
        strict=True,
    )


async def propose_pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    limit = 3
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 20))
        except ValueError:
            await update.message.reply_text("Usage: /propose_pending [limit]")
            return

    with db_conn() as conn:
        pending = get_pending_text_sources(conn, user_id=user_id, limit=limit)
    if not pending:
        await update.message.reply_text("No pending text sources found. Use /queue to inspect pending sources.")
        return

    posted_sources = 0
    posted_proposals = 0
    failed_sources = 0
    engines: List[str] = []

    for source in pending:
        text = (source.get("content_text") or "").strip()
        if not text:
            failed_sources += 1
            continue
        n, engine = await _create_and_send_proposals(
            update,
            user_id=user_id,
            source_id=int(source["id"]),
            text=text,
            announce_result=False,
        )
        engines.append(engine)
        if n > 0:
            posted_sources += 1
            posted_proposals += n
        else:
            failed_sources += 1

    engine_summary = ", ".join(sorted(set(engines))) if engines else "none"
    await update.message.reply_text(
        f"Batch propose complete.\n"
        f"Sources scanned: {len(pending)}\n"
        f"Sources with proposals: {posted_sources}\n"
        f"Proposals posted: {posted_proposals}\n"
        f"Sources without proposals: {failed_sources}\n"
        f"Engine results: {engine_summary}"
    )


async def handle_proposal_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction = update.message_reaction
    if reaction is None or reaction.user is None:
        return

    user_id = reaction.user.id
    added = _reaction_emojis(list(reaction.new_reaction)) - _reaction_emojis(list(reaction.old_reaction))
    if not added:
        return

    is_approve = bool(added & PROPOSAL_APPROVE_EMOJIS)
    is_reject = bool(added & PROPOSAL_REJECT_EMOJIS)
    if not is_approve and not is_reject:
        return

    with db_conn() as conn:
        proposal = get_pending_proposal_by_message(conn, user_id=user_id, message_id=reaction.message_id)
        if not proposal:
            return

        if is_reject and not is_approve:
            set_proposal_decision(conn, user_id=user_id, proposal_id=proposal["id"], status="rejected")
            await context.bot.send_message(chat_id=reaction.chat.id, text=f"Rejected proposal #{proposal['id']}.")
            return

        note_id = add_note(
            conn,
            user_id=user_id,
            note_type=proposal["type"],
            front=proposal.get("front", ""),
            back=proposal.get("back", ""),
            cloze=proposal.get("cloze", ""),
            extra=proposal.get("extra", ""),
            tags=proposal.get("tags", []),
            source_id=int(proposal.get("source_id", 0) or 0),
            origin="proposal_approved",
        )
        set_proposal_decision(conn, user_id=user_id, proposal_id=proposal["id"], status="approved", note_id=note_id)
        note = get_note_by_id(conn, user_id=user_id, note_id=note_id)
        user = get_user_row(conn, user_id)

        missing = _require_mochi_credentials(user)
        if missing:
            await context.bot.send_message(
                chat_id=reaction.chat.id,
                text=f"Approved proposal #{proposal['id']} as note #{note_id}. Saved locally only ({missing})",
            )
            return
        deck_check = _validate_selected_mochi_deck(user["mochi_api_key"], user["mochi_deck_id"])
        if deck_check:
            await context.bot.send_message(
                chat_id=reaction.chat.id,
                text=f"Approved proposal #{proposal['id']} as note #{note_id}. Saved locally only ({deck_check})",
            )
            return

        try:
            sync_res = sync_push_to_mochi(
                conn,
                user_id=user_id,
                key=user["mochi_api_key"],
                deck_id=user["mochi_deck_id"],
                notes=[note] if note else None,
            )
            await context.bot.send_message(
                chat_id=reaction.chat.id,
                text=f"Approved proposal #{proposal['id']} as note #{note_id}. {_fmt_push_result(sync_res)}",
            )
        except Exception as e:
            logger.exception("Proposal approval sync failed: %s", e)
            await context.bot.send_message(
                chat_id=reaction.chat.id,
                text=f"Approved proposal #{proposal['id']} as note #{note_id}, but Mochi sync failed.",
            )


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        deck_name = get_deck_name(conn, user_id)
        notes = get_user_notes(conn, user_id)
    if not notes:
        await update.message.reply_text("No cards to export yet.")
        return
    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    path = build_apkg(user_id, deck_name, notes)
    with open(path, "rb") as f:
        await update.message.reply_document(
            document=InputFile(f, filename=os.path.basename(path)),
            caption=f"Deck: {deck_name} - {len(notes)} cards",
        )


async def export_csv_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        deck_name = get_deck_name(conn, user_id)
        notes = get_user_notes(conn, user_id)
    if not notes:
        await update.message.reply_text("No cards to export yet.")
        return
    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    path = build_csv(user_id, deck_name, notes)
    with open(path, "rb") as f:
        await update.message.reply_document(
            document=InputFile(f, filename=os.path.basename(path)),
            caption=f"CSV for: {deck_name} - {len(notes)} cards",
        )


async def export_audit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        deck_name = get_deck_name(conn, user_id)
        notes = get_user_notes(conn, user_id)
        sources = get_user_sources(conn, user_id=user_id, limit=100000, status=None)
        proposals = get_user_proposals(conn, user_id=user_id, limit=100000)
        feedback_log = get_user_proposal_feedback(conn, user_id=user_id, limit=100000)
    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    path = build_audit_jsonl(user_id, deck_name, notes, sources, proposals=proposals, feedback_log=feedback_log)
    with open(path, "rb") as f:
        await update.message.reply_document(
            document=InputFile(f, filename=os.path.basename(path)),
            caption=(
                f"Audit bundle: {len(notes)} notes, {len(sources)} sources, "
                f"{len(proposals)} proposals, {len(feedback_log)} feedback entries"
            ),
        )


def _require_mochi_credentials(user: Dict[str, Any]) -> Optional[str]:
    if not user["mochi_api_key"]:
        return "Missing Mochi API key. Use /mochi_setkey <api_key> or set MOCHI_API_KEY in .env."
    if not user["mochi_deck_id"]:
        return "Missing Mochi deck id. Use /mochi_setdeck <deck_id> or /mochi_createdeck <name>."
    return None


def _fmt_push_result(res: Dict[str, Any]) -> str:
    return (
        f"Push: created={res['created']}, linked={res['already_linked']}, "
        f"recreated={res['recreated_missing_remote']}, skipped={res['skipped']}, "
        f"local_changed_not_pushed={res['local_changed_not_pushed']}"
    )


def _fmt_pull_result(res: Dict[str, Any]) -> str:
    return (
        f"Pull: remote_cards={res['remote_cards']}, created_local={res['created_local']}, "
        f"updated_local={res['updated_local']}, unchanged={res['unchanged']}, "
        f"removed_stale_links={res['removed_stale_links']}"
    )


def _validate_selected_mochi_deck(key: str, deck_id: str) -> Optional[str]:
    try:
        deck = mochi_get_deck(key, deck_id)
    except Exception as e:
        logger.exception("Could not validate Mochi deck: %s", e)
        return "Could not validate Mochi deck. Try /mochi_decks and pick a valid deck id."
    if not deck:
        return "Configured Mochi deck was not found. Use /mochi_decks then /mochi_setdeck <deck_id>."
    if deck.get("trashed?"):
        return "Configured Mochi deck is trashed. Use /mochi_decks then /mochi_setdeck <deck_id>."
    return None


async def export_mochi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await sync_mochi_push_cmd(update, context)


async def sync_mochi_push_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        user = get_user_row(conn, user_id)
        notes_count = count_user_notes(conn, user_id)
    if notes_count == 0:
        await update.message.reply_text("No local cards to push.")
        return
    missing = _require_mochi_credentials(user)
    if missing:
        await update.message.reply_text(missing)
        return
    deck_check = _validate_selected_mochi_deck(user["mochi_api_key"], user["mochi_deck_id"])
    if deck_check:
        await update.message.reply_text(deck_check)
        return
    backup_path = snapshot_db_backup(user_id, "mochi_push")
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        with db_conn() as conn:
            result = sync_push_to_mochi(conn, user_id, user["mochi_api_key"], user["mochi_deck_id"])
        await update.message.reply_text(
            f"{_fmt_push_result(result)}\nDeck: {user['mochi_deck_id']}\nBackup: {os.path.basename(backup_path)}"
        )
    except Exception as e:
        logger.exception("Mochi push sync failed: %s", e)
        await update.message.reply_text("Mochi push sync failed. Check key/deck and try again.")


async def sync_mochi_pull_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        user = get_user_row(conn, user_id)
    missing = _require_mochi_credentials(user)
    if missing:
        await update.message.reply_text(missing)
        return
    deck_check = _validate_selected_mochi_deck(user["mochi_api_key"], user["mochi_deck_id"])
    if deck_check:
        await update.message.reply_text(deck_check)
        return
    backup_path = snapshot_db_backup(user_id, "mochi_pull")
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        with db_conn() as conn:
            result = sync_pull_from_mochi(conn, user_id, user["mochi_api_key"], user["mochi_deck_id"])
        await update.message.reply_text(
            f"{_fmt_pull_result(result)}\nDeck: {user['mochi_deck_id']}\nBackup: {os.path.basename(backup_path)}"
        )
    except Exception as e:
        logger.exception("Mochi pull sync failed: %s", e)
        await update.message.reply_text("Mochi pull sync failed. Check key/deck and try again.")


async def sync_mochi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        user = get_user_row(conn, user_id)
    missing = _require_mochi_credentials(user)
    if missing:
        await update.message.reply_text(missing)
        return
    deck_check = _validate_selected_mochi_deck(user["mochi_api_key"], user["mochi_deck_id"])
    if deck_check:
        await update.message.reply_text(deck_check)
        return
    backup_path = snapshot_db_backup(user_id, "mochi_2way")
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        with db_conn() as conn:
            result = sync_mochi_both(conn, user_id, user["mochi_api_key"], user["mochi_deck_id"])
        await update.message.reply_text(
            f"{_fmt_push_result(result['push'])}\n{_fmt_pull_result(result['pull'])}\n"
            f"Deck: {user['mochi_deck_id']}\nBackup: {os.path.basename(backup_path)}"
        )
    except Exception as e:
        logger.exception("Mochi 2-way sync failed: %s", e)
        await update.message.reply_text("Mochi 2-way sync failed. Check key/deck and try again.")


async def mochi_repair_cards_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        user = get_user_row(conn, user_id)
    missing = _require_mochi_credentials(user)
    if missing:
        await update.message.reply_text(missing)
        return
    deck_check = _validate_selected_mochi_deck(user["mochi_api_key"], user["mochi_deck_id"])
    if deck_check:
        await update.message.reply_text(deck_check)
        return
    backup_path = snapshot_db_backup(user_id, "mochi_repair")
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        with db_conn() as conn:
            result = repair_mochi_cards_from_local(conn, user_id, user["mochi_api_key"], user["mochi_deck_id"])
        await update.message.reply_text(
            "Repaired linked Mochi cards.\n"
            f"checked_links={result['checked_links']}, recreated={result['recreated']}, "
            f"missing_local_note={result['missing_local_note']}, failed_create={result['failed_create']}\n"
            f"Deck: {user['mochi_deck_id']}\nBackup: {os.path.basename(backup_path)}"
        )
    except Exception as e:
        logger.exception("Mochi card repair failed: %s", e)
        await update.message.reply_text("Mochi card repair failed. Check key/deck and try again.")


async def mochi_setkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    key = " ".join(context.args) if context.args else ""
    if not key:
        await update.message.reply_text("Usage: /mochi_setkey <api_key>")
        return
    with db_conn() as conn:
        set_mochi_api_key(conn, user_id, key)
    await update.message.reply_text("Saved Mochi API key.")


async def mochi_decks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        user = get_user_row(conn, user_id)
    if not user["mochi_api_key"]:
        await update.message.reply_text("Set API key first: /mochi_setkey <api_key> (or MOCHI_API_KEY in .env).")
        return
    try:
        docs = mochi_list_decks(user["mochi_api_key"])
    except Exception as e:
        logger.exception("Mochi list decks failed: %s", e)
        await update.message.reply_text("Could not fetch decks from Mochi.")
        return
    if not docs:
        await update.message.reply_text("No Mochi decks found.")
        return
    lines = []
    for d in docs[:25]:
        did = d.get("id", "?")
        name = (d.get("name") or "(unnamed)").strip()
        lines.append(f"{name} -> {did}")
    await update.message.reply_text("Mochi decks:\n" + "\n".join(lines))


async def mochi_createdeck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text("Usage: /mochi_createdeck <name>")
        return
    with db_conn() as conn:
        user = get_user_row(conn, user_id)
    if not user["mochi_api_key"]:
        await update.message.reply_text("Set your API key first: /mochi_setkey <api_key>")
        return
    try:
        deck_id = mochi_create_deck(user["mochi_api_key"], name)
        with db_conn() as conn:
            set_mochi_deck_id(conn, user_id, deck_id)
        await update.message.reply_text(f"Created Mochi deck '{name}' with id: {deck_id}")
    except Exception as e:
        logger.exception("Create deck failed: %s", e)
        await update.message.reply_text("Failed to create deck in Mochi. Is the API key correct?")


async def mochi_setdeck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    deck_id = " ".join(context.args) if context.args else ""
    if not deck_id:
        await update.message.reply_text("Usage: /mochi_setdeck <deck_id>")
        return
    with db_conn() as conn:
        set_mochi_deck_id(conn, user_id, deck_id)
    await update.message.reply_text(f"Saved Mochi deck id: {deck_id}")


async def mochi_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    with db_conn() as conn:
        user = get_user_row(conn, user_id)
        cur = conn.execute("SELECT mochi_api_key, mochi_deck_id FROM users WHERE user_id=?", (user_id,))
        raw = cur.fetchone()
    raw_key = (raw[0] if raw else "") or ""
    raw_deck = (raw[1] if raw else "") or ""
    key_masked = (user["mochi_api_key"][:4] + "...") if user["mochi_api_key"] else "(none)"
    deck_id = user["mochi_deck_id"] or "(none)"
    key_src = "user" if raw_key else ("env-default" if MOCHI_DEFAULT_API_KEY else "missing")
    deck_src = "user" if raw_deck else ("env-default" if MOCHI_DEFAULT_DECK_ID else "missing")
    await update.message.reply_text(
        f"Mochi key: {key_masked} ({key_src})\n"
        f"Mochi deck id: {deck_id} ({deck_src})"
    )


async def backup_db_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.effective_user is not None
    user_id = update.effective_user.id
    path = snapshot_db_backup(user_id, "manual")
    await update.message.reply_text(f"Created backup: {os.path.basename(path)}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    photo_sizes = update.message.photo
    caption = (update.message.caption or "").strip()
    if not photo_sizes:
        await update.message.reply_text("I can't see a photo in this message.")
        return
    best = photo_sizes[-1]
    await update.message.chat.send_action(ChatAction.TYPING)
    file = await context.bot.get_file(best.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    image_bytes = bio.getvalue()
    saved_path = persist_source_bytes(user_id, f"{best.file_unique_id}.jpg", image_bytes)

    with db_conn() as conn:
        source_id = add_source(
            conn,
            user_id=user_id,
            source_type="telegram_photo",
            source_label=caption[:120] if caption else "telegram photo",
            content_text=caption[:MAX_SOURCE_TEXT_CHARS],
            file_path=saved_path,
            meta={
                "telegram_file_id": best.file_id,
                "telegram_file_unique_id": best.file_unique_id,
                "width": best.width,
                "height": best.height,
                "mime_type": "image/jpeg",
            },
            status="pending",
        )

        if CARD_GENERATION_BACKEND == "openai-api":
            data_url = image_to_data_url(image_bytes)
            notes = openai_generate_notes_from_image(data_url, ANKI_LANG)
            if notes:
                created = store_generated_notes(conn, user_id, source_id, notes, origin="openai")
                set_source_status(conn, user_id, source_id, "processed")
                await update.message.reply_text(
                    f"Added {created} card(s) from source #{source_id}.\n\n{format_note_previews(notes)}"
                )
                return

    await update.message.reply_text(
        f"Stored source #{source_id} for Codex-assisted processing.\n"
        "Use /queue to review pending sources and /export_audit for the full audit dataset."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    doc = update.message.document
    if not doc:
        return

    if doc.file_size and doc.file_size > MAX_DOCUMENT_BYTES:
        await update.message.reply_text(
            f"Document is too large ({doc.file_size} bytes). Max allowed is {MAX_DOCUMENT_BYTES} bytes."
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    remote = await context.bot.get_file(doc.file_id)
    bio = io.BytesIO()
    await remote.download_to_memory(out=bio)
    payload = bio.getvalue()

    guessed_ext = Path(doc.file_name or "").suffix or mimetypes.guess_extension(doc.mime_type or "") or ".bin"
    suggested_name = doc.file_name or f"{doc.file_unique_id}{guessed_ext}"
    saved_path = persist_source_bytes(user_id, suggested_name, payload)
    extracted_text = extract_text_from_document(doc.file_name or "", doc.mime_type or "", payload)
    source_type = "telegram_document_text" if extracted_text else "telegram_document_binary"
    caption = (update.message.caption or "").strip()

    with db_conn() as conn:
        source_id = add_source(
            conn,
            user_id=user_id,
            source_type=source_type,
            source_label=(doc.file_name or "telegram document")[:120],
            content_text=extracted_text if extracted_text else caption[:MAX_SOURCE_TEXT_CHARS],
            file_path=saved_path,
            meta={
                "telegram_file_id": doc.file_id,
                "telegram_file_unique_id": doc.file_unique_id,
                "mime_type": doc.mime_type or "",
                "file_name": doc.file_name or "",
                "file_size": doc.file_size or 0,
                "caption": caption,
            },
            status="pending",
        )

        if extracted_text and CARD_GENERATION_BACKEND == "openai-api":
            notes = openai_generate_notes_from_text(extracted_text, ANKI_LANG)
            if notes:
                created = store_generated_notes(conn, user_id, source_id, notes, origin="openai")
                set_source_status(conn, user_id, source_id, "processed")
                await update.message.reply_text(
                    f"Added {created} card(s) from source #{source_id}.\n\n{format_note_previews(notes)}"
                )
                return

    mode_hint = "Text extracted and queued." if extracted_text else "Binary file queued."
    await update.message.reply_text(
        f"{mode_hint} Source #{source_id} is pending for Codex-assisted processing."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message is not None
    assert update.effective_user is not None
    user_id = update.effective_user.id
    content = (update.message.text or "").strip()
    if not content:
        return
    if content.startswith("/"):
        return
    if update.message.reply_to_message:
        feedback = _extract_feedback_text(content)
        if feedback or PROPOSAL_FEEDBACK_RE.match(content):
            await _handle_proposal_feedback(
                update,
                user_id=user_id,
                replied_message_id=update.message.reply_to_message.message_id,
                feedback_text=feedback,
                strict=True,
            )
            return

    await update.message.chat.send_action(ChatAction.TYPING)
    url_match = URL_ONLY_RE.match(content)
    with db_conn() as conn:
        if url_match:
            url = url_match.group(1)
            source_id = add_source(
                conn,
                user_id=user_id,
                source_type="url",
                source_label=url[:120],
                url=url,
                meta={"transport": "telegram_text"},
                status="pending",
            )
            await update.message.reply_text(
                f"Stored URL source #{source_id}. URL fetching is left to Codex workflows."
            )
            return

        clipped = content[:MAX_SOURCE_TEXT_CHARS]
        source_id = add_source(
            conn,
            user_id=user_id,
            source_type="telegram_text",
            source_label=clipped[:120],
            content_text=clipped,
            meta={"transport": "telegram_text"},
            status="pending",
        )

        if CARD_GENERATION_BACKEND == "codex-ui-queue" and AUTO_PROPOSE_FROM_TEXT:
            # Use the same proposal workflow as /propose for plain text messages.
            await _create_and_send_proposals(
                update,
                user_id=user_id,
                source_id=source_id,
                text=clipped,
                announce_result=True,
            )
            return

        if CARD_GENERATION_BACKEND == "openai-api":
            notes = openai_generate_notes_from_text(clipped, ANKI_LANG)
            if notes:
                created = store_generated_notes(conn, user_id, source_id, notes, origin="openai")
                set_source_status(conn, user_id, source_id, "processed")
                await update.message.reply_text(
                    f"Added {created} card(s) from source #{source_id}.\n\n{format_note_previews(notes)}"
                )
                return

    await update.message.reply_text(
        f"Stored source #{source_id} for Codex-assisted processing.\n"
        "Use /queue to view pending items."
    )


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. See /help.")


# -------------------------
# main
# -------------------------
def main():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Core
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("setdeck", setdeck))
    application.add_handler(CommandHandler("addbasic", addbasic_cmd))
    application.add_handler(CommandHandler("addcloze", addcloze_cmd))
    application.add_handler(CommandHandler("propose", propose_cmd))
    application.add_handler(CommandHandler("feedback", feedback_cmd))
    application.add_handler(CommandHandler("propose_source", propose_source_cmd))
    application.add_handler(CommandHandler("propose_pending", propose_pending_cmd))
    application.add_handler(CommandHandler("queue", queue_cmd))
    application.add_handler(CommandHandler("source_done", source_done_cmd))
    application.add_handler(CommandHandler("source_ignore", source_ignore_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("export_csv", export_csv_cmd))
    application.add_handler(CommandHandler("export_audit", export_audit_cmd))
    application.add_handler(CommandHandler("clear", clear_cmd))

    # Mochi
    application.add_handler(CommandHandler("export_mochi", export_mochi_cmd))
    application.add_handler(CommandHandler("sync_mochi_push", sync_mochi_push_cmd))
    application.add_handler(CommandHandler("sync_mochi_pull", sync_mochi_pull_cmd))
    application.add_handler(CommandHandler("sync_mochi", sync_mochi_cmd))
    application.add_handler(CommandHandler("mochi_repair_cards", mochi_repair_cards_cmd))
    application.add_handler(CommandHandler("mochi_setkey", mochi_setkey_cmd))
    application.add_handler(CommandHandler("mochi_decks", mochi_decks_cmd))
    application.add_handler(CommandHandler("mochi_createdeck", mochi_createdeck_cmd))
    application.add_handler(CommandHandler("mochi_setdeck", mochi_setdeck_cmd))
    application.add_handler(CommandHandler("mochi_status", mochi_status_cmd))
    application.add_handler(CommandHandler("backup_db", backup_db_cmd))

    # Content handlers
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageReactionHandler(handle_proposal_reaction))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info(
        "Bot is running (card_backend=%s, proposal_backend=%s)...",
        CARD_GENERATION_BACKEND,
        PROPOSAL_GENERATION_BACKEND,
    )
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
