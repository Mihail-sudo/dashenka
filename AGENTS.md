# AGENTS.md

`task.txt` is the authoritative spec; `main.py` implements it (Russian comments/strings). If spec and code ever conflict, ask before changing behavior.

## Project
- Single-file Telegram bot for one user (the developer's girlfriend): `main.py`, plus `requirements.txt` and an example `.env`, with a short usage/install instruction block after the code.
- State lives in local SQLite `bot.db`; logs in `bot.log` + console (INFO). Both are gitignored.
- Stack (pin these): aiogram 3.x, aiosqlite, python-dotenv, APScheduler `AsyncIOScheduler`.
- Code comments and user-facing strings are in **Russian**.
- Bot runs on the developer's **laptop**, not a server — it is frequently off (closed lid, overnight, travel). This drives the catch-up logic below. Final instructions must cover auto-start on macOS (`launchd`) — dev machine is macOS.

## Catch-up logic (the crux — do not get this wrong)
- All dates are calendar-based in **Moscow time** (`zoneinfo "Europe/Moscow"`), stored as `YYYY-MM-DD` in `diary_state.last_sent_date`. Never UTC, never uptime-based.
- On every startup (in `on_startup`) and every `IntervalTrigger(seconds=3600)` tick, compare today against `last_sent_date`. If ≥ 1 day behind, send reasons for **every missed calendar day** (assuming reasons remain), then set `last_sent_date = today` and `current_day += N`.
- When a debt is detected, send **immediately** — do not wait for a scheduled time slot (the laptop may die at any moment).
- If `N > MAX_SEPARATE_MESSAGES` (3): one consolidated summary message, not a spam of N messages. Intro line («Пока меня не было...») only when `N > 1`.
- If a send fails (e.g. no internet): do **not** update `last_sent_date` — the next hourly tick retries.
- If reasons run out: send a final message and notify the admin.

## Access control & commands
- Constants: `GIRL_NAME`, `GIRL_ID`, `ADMIN_ID`, `BOT_TOKEN` (from `.env`, via python-dotenv).
- All admin commands restricted to `ADMIN_ID`; messages from anyone else are ignored.
- Diary: `/load_reasons` (one per line, or `.txt` file), `/reset_diary`, `/status` (reasons left, current day, last send date, debt in days).
- Auction: `/load_compliments`, `/set_why`, `/auction_stats`.
- `/start` shows the auction welcome message + inline buttons «💎 Узнать ценность» (random compliment — must never repeat the previous one; track it) and «❤️ Почему она?».

## Database (aiosqlite)
Tables: `reasons (id, text, sent, sent_date)`, `compliments (id, text, last_sent_id)`, `why_text (id, text)`, `diary_state (id, current_day, last_sent_date)`, `stats (id, button_value_clicks, button_why_clicks)`.

## Logging / errors
- Log to `bot.log` and console at INFO: every check run, every missed day, every send.
- Wrap critical paths in try/except; a failed send must not advance state.
- Entry point: `if __name__ == "__main__":` → polling.