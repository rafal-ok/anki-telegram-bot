# Simple Dockerfile for Telegram → Anki/Mochi Bot
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# The bot reads env from the environment; mount or copy a .env and use a wrapper if desired.

CMD ["python", "telegram_anki_mochi_bot.py"]
