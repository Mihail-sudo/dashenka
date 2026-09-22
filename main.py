"""
Телеграм-бот «Дневник ценности + Аукцион» (aiogram 3, один файл).
Две функции:
  1) Дневник ценности — каждый день бот отправляет девушке причину, почему
     она ценна. Если бот был выключен — пропущенные дни «догоняются» сразу
     при запуске и раз в час по расписанию (см. СТРУКТУРА, ДОГОНЯЮЩАЯ ОТПРАВКА).
  2) Аукцион — команда /start показывает приветствие и reply-кнопки
     «💎 Узнать ценность» (случайный комплимент) и «❤️ Почему она?».

Бот рассчитан ровно на двух людей: ADMIN_ID (управление) и GIRL_ID (девушка).
Сообщения от всех остальных игнорируются.
"""

import asyncio
import logging
import os
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

import http.server
import socketserver
import aiohttp
from threading import Thread

load_dotenv()

# ── Константы из .env ────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)   # только он управляет ботом
GIRL_ID = int(os.getenv("GIRL_ID", "0") or 0)     # девушка, для которой бот
GIRL_NAME = os.getenv("GIRL_NAME", "Дашенька").strip()

# ── Прочие настройки ─────────────────────────────────────────────────────────
DB_PATH = "bot.db"
LOG_FILE = "bot.log"
MSC = ZoneInfo("Europe/Moscow")         # все даты считаются по Москве
CHECK_INTERVAL_SECONDS = 3600           # период проверки долга — раз в час
MAX_SEPARATE_MESSAGES = 3               # дольше этого долга — одно сводное сообщение
COMPLIMENT_MULTILINE_MARKER = "---"      # строка-маркер: между такими строками — один многострочный комплимент

# ── Логирование: файл + консоль ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("bot")


# ── Объекты bot/dp создаются в main(), но handlers (декораторы) нужны на верхнем уровне ──
dp = Dispatcher()
bot: Bot | None = None
scheduler = AsyncIOScheduler(timezone=MSC)


# ── Вспомогательные функции ──────────────────────────────────────────────────
def today_str() -> str:
    """Сегодняшняя дата по МСК в формате YYYY-MM-DD."""
    return datetime.now(MSC).strftime("%Y-%m-%d")


def plural_form(n: int, one: str, few: str, many: str) -> str:
    """Склонение существительного по числу (русский язык)."""
    n10, n100 = n % 10, n % 100
    if 10 <= n100 <= 20:
        return many
    if n10 == 1:
        return one
    if 2 <= n10 <= 4:
        return few
    return many


def missed_dates(last_date: str, today: str) -> list[str]:
    """Все календарные дни строго после last_date и до today включительно."""
    start = datetime.strptime(last_date, "%Y-%m-%d").date() + timedelta(days=1)
    end = datetime.strptime(today, "%Y-%m-%d").date()
    dates: list[str] = []
    d = start
    while d <= end:
        dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_girl(user_id: int) -> bool:
    return user_id == GIRL_ID


# ── База данных ──────────────────────────────────────────────────────────────
async def init_db() -> None:
    """Создаёт таблицы и стартовые (одиночные) записи."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS reasons ("
            "id INTEGER PRIMARY KEY, text TEXT, sent INTEGER DEFAULT 0, sent_date TEXT)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS compliments ("
            "id INTEGER PRIMARY KEY, text TEXT, last_sent_id INTEGER)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS why_text ("
            "id INTEGER PRIMARY KEY, text TEXT, last_sent_id INTEGER)"
        )
        # для старых БД, где last_sent_id ещё не было (колонка появится в новой таблице)
        try:
            await db.execute("ALTER TABLE why_text ADD COLUMN last_sent_id INTEGER")
        except aiosqlite.OperationalError:
            pass  # колонка уже есть
        await db.execute(
            "CREATE TABLE IF NOT EXISTS diary_state ("
            "id INTEGER PRIMARY KEY, current_day INTEGER DEFAULT 0, last_sent_date TEXT)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS stats ("
            "id INTEGER PRIMARY KEY, button_value_clicks INTEGER DEFAULT 0,"
            " button_why_clicks INTEGER DEFAULT 0)"
        )
        # единственная строка дневника; дата = сегодня, чтобы при первом запуске
        # не отправить сразу кучу причин за «прошлые дни»
        cur = await db.execute("SELECT id FROM diary_state WHERE id = 1")
        if not await cur.fetchone():
            await db.execute(
                "INSERT INTO diary_state (id, current_day, last_sent_date) VALUES (1, 0, ?)",
                (today_str(),),
            )
        # единственная строка статистики кнопок
        cur = await db.execute("SELECT id FROM stats WHERE id = 1")
        if not await cur.fetchone():
            await db.execute(
                "INSERT INTO stats (id, button_value_clicks, button_why_clicks) VALUES (1, 0, 0)"
            )
        await db.commit()


# ── ДОГОНЯЮЩАЯ ОТПРАВКА (главная логика) ────────────────────────────────────
async def send_text(target_id: int, text: str) -> None:
    """Отправка с логированием; пробрасывает исключения наверх."""
    assert bot is not None, "bot ещё не инициализирован"
    await bot.send_message(chat_id=target_id, text=text)
    logger.info("Сообщение отправлено (chat_id=%s): %r", target_id, text[:80])


async def notify_reasons_exhausted(db, today: str) -> None:
    """Причины кончились: финальное сообщение девушке + уведомление админа."""
    try:
        await send_text(
            GIRL_ID,
            "Пока меня не было, накопилось много дней, а слова закончились раньше. "
            "Не переживай — я допишу новые причины специально для тебя ❤️",
        )
        await send_text(
            ADMIN_ID,
            "⚠️ Причины в «Дневнике ценности» закончились. "
            "Загрузи новые через /load_reasons, иначе отправлять будет нечего.",
        )
    except Exception:
        logger.exception("Не удалось отправить финальное сообщение о конце причин")
    # двигаем дату, чтобы уведомление не повторялось каждый час —
    # следующий напоминающий «пинок» админу случится в новый день долга
    await db.execute("UPDATE diary_state SET last_sent_date = ? WHERE id = 1", (today,))
    await db.commit()
    logger.warning("Причины закончились, diary_state.last_sent_date = %s", today)


async def send_reasons_and_advance(db, current_day: int, dates: list[str], pending: list[tuple]) -> None:
    """
    Отправляет причины по пропущенным дням и обновляет состояние БД.
    Возбуждает исключение при сбое отправки — тогда состояние НЕ меняется,
    и бот попробует снова через час.
    """
    n_all = len(dates)          # сколько дней пропущено
    n_ready = len(pending)      # сколько причин реально есть в наличии
    start_day = current_day + 1 # нумерация «День N» продолжает прошлый счётчик

    if n_all > MAX_SEPARATE_MESSAGES:
        # большой долг → одно сводное сообщение, а не спам
        lines = [f"День {start_day + i}: {text}" for i, (_, text) in enumerate(pending)]
        body = "\n".join(lines)
        if n_ready < n_all:
            body += "\n…а остальные допишу позже 😅"
        text = (
            f"Пока меня не было, накопилось {n_all} "
            f"{plural_form(n_all, 'причина', 'причины', 'причин')}. Вот они:\n{body}"
        )
        await send_text(GIRL_ID, text)
        logger.info("Отправлено сводное сообщение на %d причин", n_ready)
    else:
        if n_ready > 1:
            # вступление «о долге» — только когда причин больше одной
            await send_text(
                GIRL_ID,
                f"Пока меня не было в сети, я задолжал тебе {n_ready} "
                f"{plural_form(n_ready, 'причину', 'причины', 'причин')}. Лови:",
            )
            logger.info("Отправлено вступление о долге (%d причин)", n_ready)
        for i, (rid, text) in enumerate(pending):
            await send_text(GIRL_ID, f"День {start_day + i}. Причина: {text}")
            logger.info("Отправлена причина №%s за день %s", rid, dates[i])

    # помечаем отправленные причины (sent_date = календарный день, за который отправлено)
    for i, (rid, _) in enumerate(pending):
        await db.execute(
            "UPDATE reasons SET sent = 1, sent_date = ? WHERE id = ?", (dates[i], rid)
        )

    new_day = current_day + n_ready
    await db.execute(
        "UPDATE diary_state SET current_day = ?, last_sent_date = ? WHERE id = 1",
        (new_day, today_str()),
    )
    await db.commit()
    logger.info("Состояние обновлено: current_day=%d, last_sent_date=%s", new_day, today_str())

    if n_ready < n_all:
        # долг закрыт не полностью — причины кончились
        await notify_reasons_exhausted(db, today_str())


async def check_and_send_missed_reasons() -> None:
    """
    Ядро догоняющей отправки. Запускается при старте бота и каждый час.
    Отправляет СРАЗУ при обнаружении долга (не ждёт «расписания 09:00»),
    т.к. ноутбук может выключиться в любой момент.
    """
    logger.info("Запуск проверки пропущенных дней")
    try:
        today = today_str()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT current_day, last_sent_date FROM diary_state WHERE id = 1")
            row = await cur.fetchone()
            if row is None:
                logger.error("diary_state пуста — база повреждена, проверка прервана")
                return
            current_day, last_sent_date = row

            if last_sent_date >= today:
                # долга нет: либо отправляли сегодня, либо дата «из будущего»
                logger.info("Долга нет (last_sent_date=%s, today=%s)", last_sent_date, today)
                return

            dates = missed_dates(last_sent_date, today)
            need = len(dates)
            logger.info("Обнаружен долг: пропущено %d дн. (последняя отправка %s)", need, last_sent_date)

            cur = await db.execute(
                "SELECT id, text FROM reasons WHERE sent = 0 ORDER BY id LIMIT ?", (need,)
            )
            pending = await cur.fetchall()

            if not pending:
                # долг есть, а причин в запасе нет — предупредить админа
                logger.warning("Долг есть, но причины закончились (нужно %d)", need)
                await notify_reasons_exhausted(db, today)
                return

            # внутри — точка, где сбой отправки НЕ сдвинет last_sent_date
            await send_reasons_and_advance(db, current_day, dates, pending)
    except Exception:
        # любая ошибка (нет интернета и т.п.): состояние не трогаем, повторим через час
        logger.exception("Сбой в догоняющей отправке — повторная попытка через час")


# ── АУКЦИОН: приветствие и кнопки ────────────────────────────────────────────
def auction_keyboard() -> ReplyKeyboardMarkup:
    """Две кнопки-реплики: комплимент и «Почему она?»."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="💎 Узнать ценность"),
                KeyboardButton(text="❤️ Почему она?"),
            ]
        ],
        resize_keyboard=True,
    )


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Приветствие аукциона — только для девушки (и админа)."""
    if not (is_girl(message.from_user.id) or is_admin(message.from_user.id)):
        return
    text = (
        "Добро пожаловать на закрытый аукцион.\n"
        f"Лот №1: {GIRL_NAME}.\n"
        "Начальная ставка: 1 моё сердце.\n"
        "Текущая ставка: всё моё время.\n"
        "Купить сейчас: невозможно, потому что она не продаётся.\n"
        "Статус: бесценна."
    )
    await message.answer(text, reply_markup=auction_keyboard())


@dp.message(F.text == "💎 Узнать ценность")
async def msg_auction_value(message: Message) -> None:
    """Кнопка «💎 Узнать ценность»: случайный комплимент, не повторяя прошлый."""
    if not is_girl(message.from_user.id):
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            last = await (await db.execute(
                "SELECT last_sent_id FROM compliments WHERE last_sent_id IS NOT NULL"
                " ORDER BY id DESC LIMIT 1"
            )).fetchone()
            last_id = last[0] if last else None

            all_rows = await (await db.execute("SELECT id, text FROM compliments")).fetchall()
            if not all_rows:
                text = "Комплименты ещё не загружены 😅"
            else:
                pool = [r for r in all_rows if r[0] != last_id] or all_rows
                chosen = random.choice(pool)
                # запоминаем, что именно этот комплимент уже был показан
                await db.execute(
                    "UPDATE compliments SET last_sent_id = ? WHERE id = ?", (chosen[0], chosen[0])
                )
                text = chosen[1]
            await db.execute(
                "UPDATE stats SET button_value_clicks = button_value_clicks + 1 WHERE id = 1"
            )
            await db.commit()
        await send_text(GIRL_ID, text)
        logger.info("Комплимент отправлен (нажатие «Узнать ценность»)")
    except Exception:
        logger.exception("Сбой при отправке комплимента")


@dp.message(F.text == "❤️ Почему она?")
async def msg_auction_why(message: Message) -> None:
    """Кнопка «❤️ Почему она?»: случайный текст, не повторяя прошлый."""
    if not is_girl(message.from_user.id):
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            last = await (await db.execute(
                "SELECT last_sent_id FROM why_text WHERE last_sent_id IS NOT NULL"
                " ORDER BY id DESC LIMIT 1"
            )).fetchone()
            last_id = last[0] if last else None

            all_rows = await (await db.execute("SELECT id, text FROM why_text")).fetchall()
            if not all_rows:
                text = "Я ещё не написал, почему именно она 😌"
            else:
                pool = [r for r in all_rows if r[0] != last_id] or all_rows
                chosen = random.choice(pool)
                # запоминаем, какой текст «Почему она?» уже был показан
                await db.execute(
                    "UPDATE why_text SET last_sent_id = ? WHERE id = ?", (chosen[0], chosen[0])
                )
                text = chosen[1]
            await db.execute(
                "UPDATE stats SET button_why_clicks = button_why_clicks + 1 WHERE id = 1"
            )
            await db.commit()
        await send_text(GIRL_ID, text)
        logger.info("Отправлен текст «Почему она?» (нажатие кнопки)")
    except Exception:
        logger.exception("Сбой при отправке текста «Почему она?»")


# ── АДМИН-КОМАНДЫ ────────────────────────────────────────────────────────────
def message_body(message: Message) -> str:
    """Текст сообщения без префикса-команды в первой строке."""
    text = message.text or ""
    lines = text.splitlines()
    if lines and lines[0].startswith("/"):
        lines[0] = re.sub(r"^/\S+\s*", "", lines[0])
    return "\n".join(lines).strip()


async def extract_lines(message: Message, bot: Bot) -> list[str]:
    """Строчки из текста сообщения или из приложенного .txt-файла."""
    if message.document:
        data = await bot.download(message.document)
        raw = data.read()
        for enc in ("utf-8-sig", "cp1251"):
            try:
                return [line.strip() for line in raw.decode(enc).splitlines() if line.strip()]
            except UnicodeDecodeError:
                continue
        return []
    return [
        line.strip() for line in message_body(message).splitlines() if line.strip()
    ]


@dp.message(Command("load_reasons"))
async def cmd_load_reasons(message: Message, bot: Bot) -> None:
    """Загрузка причин дневника: построчно в сообщении или .txt файлом."""
    if not is_admin(message.from_user.id):
        return
    try:
        lines = await extract_lines(message, bot)
        if not lines:
            await message.answer("Не нашёл ни одной строки — пришли причины по одной на строку или .txt файлом.")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany("INSERT INTO reasons (text) VALUES (?)", [(l,) for l in lines])
            await db.commit()
            total = (await (await db.execute("SELECT COUNT(*) FROM reasons")).fetchone())[0]
        await message.answer(f"Загружено причин: {len(lines)}. Всего в базе: {total}.")
        logger.info("Админ загрузил %d причин (всего %d)", len(lines), total)
    except Exception:
        logger.exception("Сбой при загрузке причин")
        await message.answer("Ошибка при загрузке причин — см. bot.log.")


def parse_compliments(lines: list[str]) -> list[str]:
    """Комплименты из строк: обычные — по одной на строку; блок между
    строками-маркерами «---» собирается в один многострочный комплимент."""
    compliments: list[str] = []
    buffer: list[str] = []
    in_multiline = False
    for line in lines:
        if line == COMPLIMENT_MULTILINE_MARKER:
            if buffer:
                compliments.append("\n".join(buffer))
                buffer = []
            in_multiline = not in_multiline
            continue
        if in_multiline:
            buffer.append(line)
        else:
            compliments.append(line)
    if buffer:
        compliments.append("\n".join(buffer))
    return compliments


@dp.message(Command("load_compliments"))
async def cmd_load_compliments(message: Message, bot: Bot) -> None:
    """Загрузка комплиментов для кнопки «Узнать ценность».
    Каждая строка — отдельный комплимент; блок между строками «---»
    — один многострочный комплимент. Работает и с .txt файлом."""
    if not is_admin(message.from_user.id):
        return
    try:
        lines = await extract_lines(message, bot)
        if not lines:
            await message.answer(
                "Не нашёл ни одной строки — пришли комплименты, например:\n"
                "/load_compliments\n"
                "ты красивая\n"
                "---\n"
                "ты самая лучшая,\n"
                "я это знаю точно.\n"
                "---"
            )
            return
        compliments = parse_compliments(lines)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executemany(
                "INSERT INTO compliments (text) VALUES (?)", [(c,) for c in compliments]
            )
            await db.commit()
            total = (await (await db.execute("SELECT COUNT(*) FROM compliments")).fetchone())[0]
        await message.answer(
            f"Загружено комплиментов: {len(compliments)}. Всего в базе: {total}."
        )
        logger.info("Админ загрузил %d комплиментов (всего %d)", len(compliments), total)
    except Exception:
        logger.exception("Сбой при загрузке комплиментов")
        await message.answer("Ошибка при загрузке комплиментов — см. bot.log.")


@dp.message(Command("set_why"))
async def cmd_set_why(message: Message, bot: Bot) -> None:
    """Установка текстов «Почему она?»: как у комплиментов — каждая строка
    отдельный текст, блок между «---» один многострочный. .txt тоже можно.
    ЗАМЕНЯЕТ все прошлые тексты кнопки."""
    if not is_admin(message.from_user.id):
        return
    try:
        lines = await extract_lines(message, bot)
        if not lines:
            await message.answer(
                "Пришли текст после команды, например:\n"
                "/set_why Потому что…\n"
                "или несколько, разделяя «---» для многострочных."
            )
            return
        texts = parse_compliments(lines)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM why_text")
            await db.executemany(
                "INSERT INTO why_text (text) VALUES (?)", [(t,) for t in texts]
            )
            await db.commit()
        await message.answer(f"Сохранено текстов «Почему она?»: {len(texts)} ✅")
        logger.info("Обновлён текст «Почему она?»: %d шт", len(texts))
    except Exception:
        logger.exception("Сбой при сохранении текста «Почему она?»")
        await message.answer("Ошибка при сохранении — см. bot.log.")


@dp.message(Command("reset_diary"))
async def cmd_reset_diary(message: Message) -> None:
    """Сброс счётчика дней дневника (только админ)."""
    if not is_admin(message.from_user.id):
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE diary_state SET current_day = 0 WHERE id = 1")
            await db.commit()
        await message.answer("Счётчик дней сброшен (current_day = 0) ✅")
        logger.info("Админ сбросил счётчик дней")
    except Exception:
        logger.exception("Сбой при сбросе счётчика")
        await message.answer("Ошибка при сбросе — см. bot.log.")


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Статус дневника: остаток причин, день, дата отправки, долг."""
    if not is_admin(message.from_user.id):
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            total_s, sent_s = (await (await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(sent), 0) FROM reasons")).fetchone())
            state = await (await db.execute(
                "SELECT current_day, last_sent_date FROM diary_state WHERE id = 1")).fetchone()
            current_day, last_date = state

        debt = 0
        if last_date:
            debt = max(0, (datetime.now(MSC).date() - datetime.strptime(last_date, "%Y-%m-%d").date()).days)

        text = (
            "📊 <b>Статус «Дневника ценности»</b>\n"
            f"💎 Причин осталось: <b>{total_s - sent_s}</b> (всего {total_s})\n"
            f"📅 Текущий день: <b>{current_day}</b>\n"
            f"🗓 Последняя отправка: <b>{last_date or 'ещё не было'}</b>\n"
            f"⏳ Долг: <b>{debt} дн.</b>"
        )
        await message.answer(text, parse_mode=ParseMode.HTML)
        logger.info("Админ запросил /status (остаток %d, долг %d дн.)", total_s - sent_s, debt)
    except Exception:
        logger.exception("Сбой при получении статуса")
        await message.answer("Ошибка при получении статуса — см. bot.log.")


@dp.message(Command("auction_stats"))
async def cmd_auction_stats(message: Message) -> None:
    """Статистика нажатий кнопок аукциона."""
    if not is_admin(message.from_user.id):
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            v, w = await (await db.execute(
                "SELECT button_value_clicks, button_why_clicks FROM stats WHERE id = 1")).fetchone()
        text = (
            "📊 <b>Статистика аукциона</b>\n"
            f"💎 «Узнать ценность»: <b>{v}</b>\n"
            f"❤️ «Почему она?»: <b>{w}</b>"
        )
        await message.answer(text, parse_mode=ParseMode.HTML)
        logger.info("Админ запросил /auction_stats (value=%d, why=%d)", v, w)
    except Exception:
        logger.exception("Сбой при получении статистики аукциона")
        await message.answer("Ошибка при получении статистики — см. bot.log.")


# ── Запуск / остановка ───────────────────────────────────────────────────────
async def on_startup(dispatcher: Dispatcher, bot: Bot) -> None:
    """При старте: БД, планировщик и сразу одна проверка долга."""
    await init_db()
    scheduler.add_job(
        check_and_send_missed_reasons,
        IntervalTrigger(seconds=CHECK_INTERVAL_SECONDS),
        id="check_and_send_missed_reasons",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Бот запущен, планировщик активен (интервал %d с)", CHECK_INTERVAL_SECONDS)
    await check_and_send_missed_reasons()


async def on_shutdown(dispatcher: Dispatcher, bot: Bot) -> None:
    """Аккуратно останавливаем планировщик при завершении."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Бот остановлен")


class PingHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Бот активен! ❤️".encode("utf-8"))

def start_web_server():
    port = int(os.environ.get("PORT", 8000))
    server = socketserver.TCPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()

# === 2. САМОСТОЯТЕЛЬНЫЙ АВТОПИНГ В ИНТЕРНЕТ ===
async def self_ping():
    # Замените ссылку ниже на URL вашего приложения из панели управления Render!
    url = "https://onrender.com"
    
    await asyncio.sleep(30)  # Даем боту время запуститься
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url) as response:
                    print(f"[Pinger] Успешный пинг, статус: {response.status}")
            except Exception as e:
                print(f"[Pinger] Ошибка пинга: {e}")
            await asyncio.sleep(600)  # Повторяем каждые 10 минут


def main() -> None:
    global bot
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан: скопируй .env.example в .env и заполни его.")
        return
    if not ADMIN_ID or not GIRL_ID:
        logger.error("ADMIN_ID и GIRL_ID должны быть заданы в .env.")
        return

    bot = Bot(BOT_TOKEN)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    Thread(target=start_web_server, daemon=True).start()
    asyncio.create_task(self_ping())

    asyncio.run(dp.start_polling(bot, skip_updates=True))


if __name__ == "__main__":
    main()


# ─────────────────────────────────────────────────────────────────────────────
# КРАТКАЯ ИНСТРУКЦИЯ
# ─────────────────────────────────────────────────────────────────────────────
# 1) Установка:
#    python3 -m venv venv && source venv/bin/activate
#    pip install -r requirements.txt
#    cp .env.example .env   # заполни BOT_TOKEN, ADMIN_ID, GIRL_ID, GIRL_NAME
# 2) Запуск:
#    python main.py         # бот начнёт работать (polling), логи в bot.log и консоль
#
# Команды (только админ):
#    /load_reasons      — причины дневника: по одной на строку или .txt файлом
#    /reset_diary       — сброс счётчика дней
#    /status            — остаток причин, текущий день, дата отправки, долг
#    /load_compliments  — комплименты для кнопки «💎 Узнать ценность»
#    /set_why           — тексты «❤️ Почему она?» (несколько; «---» = один многострочный)
#    /auction_stats     — сколько раз нажаты кнопки
#
# Догоняющая отправка: бот помнит дату последней отправки (МСК) и проверяет
# долг при каждом запуске и каждый час. Если ноут был выключен — пропущенные
# дни отправляются сразу при возвращении бота в сеть (до 3 дней — отдельными
# сообщениями, дольше — одним сводным). Если отправка упала — дата не
# сдвигается, повторим через час.
#
# АВТОЗАПУСК НА macOS (launchd):
# Создай файл ~/Library/LaunchAgents/com.dashenka.bot.plist:
#     <?xml version="1.0" encoding="UTF-8"?>
#     <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#       "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
#     <plist version="1.0">
#     <dict>
#       <key>Label</key><string>com.dashenka.bot</string>
#       <key>ProgramArguments</key>
#       <array>
#         <string>/Users/ВАШ_ЮЗЕР/python_projects/Dashenka/venv/bin/python</string>
#         <string>/Users/ВАШ_ЮЗЕР/python_projects/Dashenka/main.py</string>
#       </array>
#       <key>RunAtLoad</key><true/>
#       <key>KeepAlive</key><true/>
#     </dict>
#     </plist>
# Загрузи: launchctl load ~/Library/LaunchAgents/com.dashenka.bot.plist
# KeepAlive=true перезапустит бот при падении; при включении ноутбука launchd
# поднимет его сам. Логи — в bot.log рядом с main.py.
# ─────────────────────────────────────────────────────────────────────────────