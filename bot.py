"""Telegram-бот розыгрышей и игр для группы (aiogram 3, SQLite).

Всё в одном файле. Настройка через переменные окружения:
    BOT_TOKEN       токен от @BotFather
    ADMIN_IDS       ID админов через запятую (остальных можно добавить командой /addadmin)
    GROUP_CHAT_ID   ID группы, например -1001234567890
    LOG_CHANNEL_ID  ID канала для логов (необязательно), например -1001234567890
    DB_PATH         путь к базе (по умолчанию giveaway.db)
    WEEKLY_PRIZE_STARS  приз недели в Stars (по умолчанию 100)

Запуск:  python bot.py
"""

import asyncio
import csv
import html
import io
import json
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import (
    JOIN_TRANSITION,
    LEAVE_TRANSITION,
    BaseFilter,
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
    CommandStart,
    StateFilter,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# =====================================================================
# НАСТРОЙКИ
# =====================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# ID администраторов через запятую, например: ADMIN_IDS=123456789,987654321
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
# ID группы, например -1001234567890. Бот должен быть в ней админом с правом приглашать.
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
# ID канала или группы для логов, например -1001234567890. Бот должен быть там админом.
# Пусто или 0 — логи в канал не отправляются (только в консоль).
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or "0")
DB_PATH = os.getenv("DB_PATH", "giveaway.db")
WEEKLY_PRIZE_STARS = int(os.getenv("WEEKLY_PRIZE_STARS", "100"))
TZ = timezone(timedelta(hours=5))  # Asia/Samarkand

WEEK = 7 * 24 * 3600
MAX_GIFTS_PER_PRIZE = 50  # защита от слишком длинного списка подарков
MAX_TOPUP_STARS = 10_000
TOPUP_PAYLOAD = "bot_topup"
PAYOUT_MAX_ATTEMPTS = 20  # сколько раз бот повторяет невыданный приз
REFERRAL_CONFIRM_SECONDS = 24 * 3600  # приглашение засчитывается, если человек остался в группе сутки
GAME_TITLES = {
    "dice": "Дуэль на кубиках 🎲",
    "darts": "Дартс 🎯",
    "basket": "Баскетбол 🏀",
    "football": "Футбол ⚽",
    "bowling": "Боулинг 🎳",
    "slots": "Слоты 🎰",
    "guess": "Угадай число 🔢",
    "word": "Первое слово 💬",
    "math": "Быстрый счёт 🧮",
    "roulette": "Лотерея 🍀",
    "coin": "Орёл или решка 🪙",
    "btn": "Первый нажмёт",
}
# Telegram-эмодзи, которые бот принимает как бросок в игре, и вид игры
KIND_BY_EMOJI = {"🎲": "dice", "🎯": "darts", "🏀": "basket", "⚽": "football",
                 "🎳": "bowling", "🎰": "slots"}
EMOJI_BY_KIND = {kind: emoji for emoji, kind in KIND_BY_EMOJI.items()}
GUESS_MAX = 100

# =====================================================================
# ПРЕМИУМ-ЭМОДЗИ (пак Telegram iOS Icons, источник: emoji.wivvi.net)
# В тексте — тег <tg-emoji>, в кнопках — icon_custom_emoji_id.
# Внутри тега стоит обычный эмодзи-заглушка, который видно без Premium.
# =====================================================================

PREMIUM = {
    "gift": ("6032644646587338669", "🎁"),
    "star": ("6034923938486684992", "⭐️"),
    "people": ("6032609071373226027", "👥"),
    "add": ("6033108709213736873", "➕"),
    "ok": ("5774022692642492953", "✅"),
    "no": ("5774077015388852135", "❌"),
    "link": ("6028171274939797252", "🔗"),
    "bell": ("6039486778597970865", "🔔"),
    "back": ("5960671702059848143", "⬅️"),
    "calendar": ("5890937706803894250", "📅"),
    "info": ("6028435952299413210", "ℹ️"),
    "hooray": ("6041731551845159060", "🎉"),
    "crown": ("5805553606635559688", "👑"),
    "megaphone": ("6021418126061605425", "📢"),
    "search": ("6032850693348399258", "🔎"),
    "bot": ("6030400221232501136", "🤖"),
}
EMOJI_ID = {name: emoji_id for name, (emoji_id, _) in PREMIUM.items()}


def tg(name: str) -> str:
    """Премиум-эмодзи для текста сообщения (нужен parse_mode=HTML)."""
    emoji_id, fallback = PREMIUM[name]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# =====================================================================
# БАЗА ДАННЫХ (SQLite)
# =====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    referred_by INTEGER,
    joined_at INTEGER
);
CREATE TABLE IF NOT EXISTS referrals (
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invite_links (
    user_id INTEGER PRIMARY KEY,
    link TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    prize TEXT NOT NULL,
    channel TEXT,
    ends_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    winner_id INTEGER,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS participants (
    giveaway_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    prize_stars INTEGER NOT NULL DEFAULT 0,
    winners INTEGER NOT NULL DEFAULT 1,
    ends_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER NOT NULL,
    message_id INTEGER,
    secret TEXT
);
CREATE TABLE IF NOT EXISTS game_entries (
    game_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    won INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, user_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT,
    plan TEXT,
    sent INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    hhmm TEXT NOT NULL,
    config TEXT NOT NULL,
    last_run TEXT
);
CREATE TABLE IF NOT EXISTS blacklist (
    user_id INTEGER PRIMARY KEY,
    added_at INTEGER NOT NULL
);
"""


def _now() -> int:
    return int(time.time())


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        # Миграции для баз, созданных раньше
        migrations = [
            "ALTER TABLE giveaways ADD COLUMN prize_stars INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE games ADD COLUMN secret TEXT",
            # Приглашение засчитывается не сразу, а после проверки (старые считаем подтверждёнными)
            "ALTER TABLE referrals ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 1",
        ]
        for sql in migrations:
            try:
                await conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # колонка уже есть
        await conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('week_start', ?)",
            (str(_now()),),
        )
        await conn.commit()


async def _one(sql: str, args: tuple = ()) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, args)
        row = await cur.fetchone()
        return dict(row) if row else None


async def _all(sql: str, args: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(sql, args)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def _exec(sql: str, args: tuple = ()) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(sql, args)
        await conn.commit()
        return cur.rowcount


# ---------- пользователи и приглашения ----------

async def register_user(user_id: int, username: str | None, first_name: str | None,
                        referrer_id: int | None) -> bool:
    """Сохраняет пользователя. Возвращает True, если он новый."""
    now = _now()
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO users VALUES (?, ?, ?, ?, ?)",
            (user_id, username, first_name, referrer_id, now),
        )
        if cur.rowcount == 0:
            await conn.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username, first_name, user_id),
            )
            await conn.commit()
            return False
        if referrer_id and referrer_id != user_id:
            await conn.execute(
                "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at, confirmed) "
                "VALUES (?, ?, ?, 0)",
                (referrer_id, user_id, now),
            )
        await conn.commit()
        return True


async def get_user(user_id: int) -> dict | None:
    return await _one("SELECT * FROM users WHERE user_id = ?", (user_id,))


async def get_invite_link(user_id: int) -> str | None:
    row = await _one("SELECT link FROM invite_links WHERE user_id = ?", (user_id,))
    return row["link"] if row else None


async def save_invite_link(user_id: int, link: str) -> None:
    await _exec("INSERT OR REPLACE INTO invite_links (user_id, link) VALUES (?, ?)",
                (user_id, link))


async def referrer_by_link(link: str) -> int | None:
    row = await _one("SELECT user_id FROM invite_links WHERE link = ?", (link,))
    return row["user_id"] if row else None


async def add_referral(referrer_id: int, referred_id: int) -> bool:
    """Записывает приглашение как ожидающее проверки. Каждого человека можно засчитать один раз."""
    if referrer_id == referred_id:
        return False
    return await _exec(
        "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at, confirmed) "
        "VALUES (?, ?, ?, 0)",
        (referrer_id, referred_id, _now()),
    ) > 0


async def pending_referrals(created_before: int) -> list[dict]:
    return await _all(
        "SELECT referred_id FROM referrals WHERE confirmed = 0 AND created_at <= ? LIMIT 50",
        (created_before,),
    )


async def confirm_referral(referred_id: int) -> None:
    await _exec("UPDATE referrals SET confirmed = 1 WHERE referred_id = ?", (referred_id,))


async def remove_referral(referred_id: int) -> None:
    """Вышедший из группы не засчитывается как приглашённый."""
    await _exec("DELETE FROM referrals WHERE referred_id = ?", (referred_id,))


async def count_referrals(user_id: int, since: int) -> int:
    row = await _one(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND created_at >= ? "
        "AND confirmed = 1",
        (user_id, since),
    )
    return row["c"]


async def top_referrers(since: int, limit: int = 10) -> list[dict]:
    return await _all(
        """
        SELECT r.referrer_id AS user_id, u.username, u.first_name, COUNT(*) AS cnt
        FROM referrals r
        LEFT JOIN users u ON u.user_id = r.referrer_id
        WHERE r.created_at >= ? AND r.confirmed = 1
        GROUP BY r.referrer_id
        ORDER BY cnt DESC
        LIMIT ?
        """,
        (since, limit),
    )


async def week_start() -> int:
    row = await _one("SELECT value FROM settings WHERE key = 'week_start'")
    return int(row["value"])


async def set_week_start(ts: int) -> None:
    await _exec("UPDATE settings SET value = ? WHERE key = 'week_start'", (str(ts),))


# ---------- администраторы ----------

async def is_admin(uid: int) -> bool:
    """Админ — либо из ADMIN_IDS (переменная окружения), либо добавлен командой в боте."""
    if uid in ADMIN_IDS:
        return True
    return await _one("SELECT 1 AS x FROM admins WHERE user_id = ?", (uid,)) is not None


async def all_admin_ids() -> set[int]:
    rows = await _all("SELECT user_id FROM admins")
    return ADMIN_IDS | {r["user_id"] for r in rows}


async def add_admin(uid: int, added_by: int) -> bool:
    return await _exec(
        "INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
        (uid, added_by, _now()),
    ) > 0


async def remove_admin(uid: int) -> bool:
    return await _exec("DELETE FROM admins WHERE user_id = ?", (uid,)) > 0


# ---------- чёрный список ----------

async def is_blocked(uid: int) -> bool:
    return await _one("SELECT 1 AS x FROM blacklist WHERE user_id = ?", (uid,)) is not None


async def block_user(uid: int) -> bool:
    return await _exec("INSERT OR IGNORE INTO blacklist (user_id, added_at) VALUES (?, ?)",
                       (uid, _now())) > 0


async def unblock_user(uid: int) -> bool:
    return await _exec("DELETE FROM blacklist WHERE user_id = ?", (uid,)) > 0


# ---------- выплаты Stars (очередь с повтором) ----------

async def create_payout(uid: int, amount: int, reason: str) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO payouts (user_id, amount, reason, created_at) VALUES (?, ?, ?, ?)",
            (uid, amount, reason, _now()),
        )
        await conn.commit()
        return cur.lastrowid


async def get_payout(pid: int) -> dict | None:
    return await _one("SELECT * FROM payouts WHERE id = ?", (pid,))


async def pending_payouts() -> list[dict]:
    return await _all(
        "SELECT id FROM payouts WHERE status = 'pending' AND attempts < ? ORDER BY id LIMIT 20",
        (PAYOUT_MAX_ATTEMPTS,),
    )


async def update_payout(pid: int, **fields) -> None:
    cols = ", ".join(f"{name} = ?" for name in fields)
    await _exec(f"UPDATE payouts SET {cols} WHERE id = ?", (*fields.values(), pid))


# ---------- расписание игр ----------

async def create_schedule(admin_id: int, hhmm: str, config: dict) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO schedules (admin_id, hhmm, config) VALUES (?, ?, ?)",
            (admin_id, hhmm, json.dumps(config, ensure_ascii=False)),
        )
        await conn.commit()
        return cur.lastrowid


async def list_schedules() -> list[dict]:
    return await _all("SELECT id, hhmm, config FROM schedules ORDER BY hhmm")


async def delete_schedule(sid: int) -> bool:
    return await _exec("DELETE FROM schedules WHERE id = ?", (sid,)) > 0


async def due_schedules(hhmm: str, today: str) -> list[dict]:
    return await _all(
        "SELECT * FROM schedules WHERE hhmm = ? AND (last_run IS NULL OR last_run != ?)",
        (hhmm, today),
    )


async def mark_schedule_run(sid: int, today: str) -> None:
    await _exec("UPDATE schedules SET last_run = ? WHERE id = ?", (today, sid))


# ---------- розыгрыши ----------

async def create_giveaway(title: str, prize: str, prize_stars: int, channel: str | None,
                          ends_at: int, created_by: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO giveaways (title, prize, prize_stars, channel, ends_at, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, prize, prize_stars, channel, ends_at, created_by, _now()),
        )
        await conn.commit()
        return cur.lastrowid


async def get_giveaway(gid: int) -> dict | None:
    return await _one("SELECT * FROM giveaways WHERE id = ?", (gid,))


async def list_active_giveaways() -> list[dict]:
    return await _all(
        """
        SELECT g.*, (SELECT COUNT(*) FROM participants p WHERE p.giveaway_id = g.id) AS cnt
        FROM giveaways g
        WHERE g.status = 'active'
        ORDER BY g.ends_at
        """
    )


async def due_giveaways(now: float) -> list[dict]:
    return await _all(
        "SELECT id FROM giveaways WHERE status = 'active' AND ends_at <= ?", (int(now),)
    )


async def add_participant(gid: int, user_id: int) -> bool:
    return await _exec(
        "INSERT OR IGNORE INTO participants VALUES (?, ?, ?)", (gid, user_id, _now())
    ) > 0


async def is_participant(gid: int, user_id: int) -> bool:
    return await _one(
        "SELECT 1 AS x FROM participants WHERE giveaway_id = ? AND user_id = ?",
        (gid, user_id),
    ) is not None


async def participant_count(gid: int) -> int:
    row = await _one("SELECT COUNT(*) AS c FROM participants WHERE giveaway_id = ?", (gid,))
    return row["c"]


async def participant_ids(gid: int) -> list[int]:
    rows = await _all("SELECT user_id FROM participants WHERE giveaway_id = ?", (gid,))
    return [r["user_id"] for r in rows]


async def export_participants(gid: int) -> list[dict]:
    return await _all(
        "SELECT p.user_id, p.joined_at, u.username, u.first_name FROM participants p "
        "LEFT JOIN users u ON u.user_id = p.user_id WHERE p.giveaway_id = ? ORDER BY p.joined_at",
        (gid,),
    )


async def close_giveaway(gid: int, winner_id: int | None) -> None:
    await _exec(
        "UPDATE giveaways SET status = 'finished', winner_id = ? WHERE id = ?",
        (winner_id, gid),
    )


async def cancel_giveaway(gid: int) -> bool:
    return await _exec(
        "UPDATE giveaways SET status = 'cancelled' WHERE id = ? AND status = 'active'", (gid,)
    ) > 0


async def update_giveaway_text(gid: int, field: str, value: str) -> bool:
    if field not in ("title", "prize"):
        raise ValueError(field)
    return await _exec(
        f"UPDATE giveaways SET {field} = ? WHERE id = ? AND status = 'active'", (value, gid)
    ) > 0


# ---------- игры ----------

async def create_game(kind: str, title: str, prize_stars: int, winners: int,
                      ends_at: int, created_by: int, secret: str | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO games (kind, title, prize_stars, winners, ends_at, created_by, secret) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, title, prize_stars, winners, ends_at, created_by, secret),
        )
        await conn.commit()
        return cur.lastrowid


async def set_game_message(gid: int, message_id: int) -> None:
    await _exec("UPDATE games SET message_id = ? WHERE id = ?", (message_id, gid))


async def get_game(gid: int) -> dict | None:
    return await _one("SELECT * FROM games WHERE id = ?", (gid,))


async def active_game(kind: str) -> dict | None:
    """Последняя активная игра указанного типа, которая ещё принимает участников."""
    return await _one(
        "SELECT * FROM games WHERE kind = ? AND status = 'active' AND ends_at > ? "
        "ORDER BY id DESC LIMIT 1",
        (kind, _now()),
    )


async def list_active_games() -> list[dict]:
    return await _all(
        "SELECT g.*, (SELECT COUNT(*) FROM game_entries e WHERE e.game_id = g.id) AS cnt "
        "FROM games g WHERE g.status = 'active' AND g.ends_at > ? ORDER BY g.id DESC",
        (_now(),),
    )


async def cancel_game(gid: int) -> bool:
    return await _exec(
        "UPDATE games SET status = 'cancelled' WHERE id = ? AND status = 'active'", (gid,)
    ) > 0


async def due_games(now: float) -> list[dict]:
    return await _all(
        "SELECT id FROM games WHERE status = 'active' AND ends_at <= ?", (int(now),)
    )


async def enter_game(gid: int, user_id: int, value: int) -> bool:
    """Записывает участника (один раз на игру). Возвращает True, если записан."""
    return await _exec(
        "INSERT OR IGNORE INTO game_entries (game_id, user_id, value, created_at) "
        "VALUES (?, ?, ?, ?)",
        (gid, user_id, value, _now()),
    ) > 0


async def game_entries_count(gid: int) -> int:
    row = await _one("SELECT COUNT(*) AS c FROM game_entries WHERE game_id = ?", (gid,))
    return row["c"]


async def ranked_entries(gid: int, game: dict) -> list[dict]:
    """Порядок призовых мест.
    Броски (кубик, дартс и т.д.) — по убыванию значения; «Угадай число» — кто ближе
    к загаданному; остальные — в порядке нажатия. При равенстве выигрывает тот, кто раньше."""
    kind = game["kind"]
    if kind == "guess":
        order = "ABS(value - CAST(? AS INTEGER)), created_at, rowid"
        args = (gid, game["secret"])
    elif kind in KIND_BY_EMOJI.values() or kind == "roulette":
        order = "value DESC, created_at, rowid"
        args = (gid,)
    else:
        order = "created_at, rowid"
        args = (gid,)
    return await _all(
        f"SELECT user_id, value FROM game_entries WHERE game_id = ? ORDER BY {order}",
        args,
    )


async def mark_game_finished(gid: int, winner_ids: list[int]) -> None:
    for uid in winner_ids:
        await _exec("UPDATE game_entries SET won = 1 WHERE game_id = ? AND user_id = ?",
                    (gid, uid))
    await _exec("UPDATE games SET status = 'finished' WHERE id = ?", (gid,))


# ---------- статистика ----------

async def bot_stats() -> dict:
    week = await week_start()
    queries = {
        "users": "SELECT COUNT(*) AS c FROM users",
        "giveaways": "SELECT COUNT(*) AS c FROM giveaways",
        "giveaways_active": "SELECT COUNT(*) AS c FROM giveaways WHERE status = 'active'",
        "games": "SELECT COUNT(*) AS c FROM games",
        "game_entries": "SELECT COUNT(*) AS c FROM game_entries",
        "referrals_all": "SELECT COUNT(*) AS c FROM referrals WHERE confirmed = 1",
        "referrals_week": "SELECT COUNT(*) AS c FROM referrals WHERE confirmed = 1 AND created_at >= ?",
        "payouts_pending": "SELECT COUNT(*) AS c FROM payouts WHERE status = 'pending'",
        "blocked": "SELECT COUNT(*) AS c FROM blacklist",
    }
    stats = {}
    for key, sql in queries.items():
        args = (week,) if key == "referrals_week" else ()
        stats[key] = (await _one(sql, args))["c"]
    return stats


# =====================================================================
# КЛАВИАТУРЫ
# =====================================================================

def btn(text: str, icon: str | None = None, cb: str | None = None,
        url: str | None = None, disabled: bool = False) -> InlineKeyboardButton:
    """Кнопка с премиум-иконкой. disabled=True делает кнопку неактивной (Bot API 10.3)."""
    extra = {"disabled": True} if disabled else {}
    return InlineKeyboardButton(
        text=text,
        callback_data=cb,
        url=url,
        icon_custom_emoji_id=EMOJI_ID[icon] if icon else None,
        **extra,
    )


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def admin_menu() -> InlineKeyboardMarkup:
    return kb(
        [btn("Создать розыгрыш", "gift", "adm:new")],
        [btn("Активные розыгрыши", "calendar", "adm:list")],
        [btn("Игры", "gift", "adm:games")],
        [btn("Топ недели", "crown", "adm:week")],
        [btn("Закрыть неделю и выдать итоги", "ok", "adm:closeweek")],
        [btn("Баланс и пополнение Stars", "star", "adm:bal")],
        [btn("Статистика", "info", "adm:stats")],
        [btn("Администраторы", "bot", "adm:admins")],
    )


def games_menu() -> InlineKeyboardMarkup:
    rows = [[btn(title, "gift", f"adm:game:{kind}")] for kind, title in GAME_TITLES.items()]
    rows.append([btn("Активные игры", "calendar", "adm:glist")])
    rows.append([btn("Расписание игр", "calendar", "adm:sched")])
    rows.append([btn("В меню", "back", "adm:menu")])
    return kb(*rows)


def balance_menu() -> InlineKeyboardMarkup:
    return kb(
        [btn("Пополнить баланс", "star", "adm:topup")],
        [btn("В меню", "back", "adm:menu")],
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return kb([btn("В меню", "back", "adm:menu")])


def active_list(rows: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [btn(f"#{r['id']} {plain(r['title'])[:28]} · {r['cnt']} уч.", "gift", f"adm:g:{r['id']}")]
        for r in rows
    ]
    buttons.append([btn("Назад", "back", "adm:menu")])
    return kb(*buttons)


def giveaway_menu(gid: int) -> InlineKeyboardMarkup:
    return kb(
        [btn("Изменить название", "info", f"adm:et:{gid}:title"),
         btn("Изменить приз", "gift", f"adm:et:{gid}:prize")],
        [btn("Экспорт участников (CSV)", "people", f"adm:csv:{gid}")],
        [btn("Отменить розыгрыш", "no", f"adm:cancel:{gid}")],
        [btn("Завершить сейчас", "ok", f"adm:fin:{gid}")],
        [btn("Назад", "back", "adm:list")],
    )


# =====================================================================
# ОБЩИЕ ПОМОЩНИКИ
# =====================================================================

class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and await is_admin(event.from_user.id)


class NewGiveaway(StatesGroup):
    title = State()
    prize = State()
    stars = State()
    hours = State()
    channel = State()


class EditGiveaway(StatesGroup):
    value = State()


class NewGame(StatesGroup):
    title = State()
    hint = State()
    secret = State()
    stars = State()
    winners = State()
    minutes = State()
    schedule = State()


class TopUp(StatesGroup):
    amount = State()


def esc(text: str | None) -> str:
    return html.escape(text or "")


def plain(html_text: str) -> str:
    """Убирает HTML-теги (для текста кнопок, где разметка не поддерживается)."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text or ""))


def rich_input(message: Message, limit: int) -> str | None:
    """Текст админа с его форматированием в HTML: премиум-эмодзи, жирный, курсив, шрифты.
    Обычные эмодзи остаются обычными. Возвращает None, если текст пустой или длиннее limit."""
    if not message.text or not message.text.strip() or len(message.text.strip()) > limit:
        return None
    return message.html_text.strip()


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


def parse_int(text: str, lo: int, hi: int) -> int | None:
    text = text.strip()
    if not text.lstrip("-").isdigit():
        return None
    value = int(text)
    return value if lo <= value <= hi else None


def parse_float(text: str, lo: float, hi: float) -> float | None:
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return value if lo <= value <= hi else None


def user_link(uid: int, user: dict | None) -> str:
    if user and user.get("username"):
        label = f"@{user['username']}"
    elif user and user.get("first_name"):
        label = user["first_name"]
    else:
        label = str(uid)
    return f'<a href="tg://user?id={uid}">{esc(label)}</a>'


def stars_line(stars: int) -> str:
    return f"\n{tg('star')} Stars каждому победителю: {stars}" if stars else ""


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except TelegramAPIError as e:
        logging.warning("Не удалось отправить сообщение %s: %s", chat_id, e)


async def log_event(bot: Bot, text: str) -> None:
    """Пишет событие в канал логов (LOG_CHANNEL_ID). Ошибки логирования не должны ломать бота."""
    logging.info("LOG: %s", plain(text))
    if not LOG_CHANNEL_ID:
        return
    stamp = datetime.now(TZ).strftime("%d.%m.%Y %H:%M:%S")
    try:
        await bot.send_message(LOG_CHANNEL_ID, f"<code>{stamp}</code>\n{text}",
                               disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001 — логирование не должно валить обработчики
        logging.warning("Не удалось отправить лог в канал: %s", e)


async def post_to_group(bot: Bot, text: str, **kwargs):
    try:
        return await bot.send_message(GROUP_CHAT_ID, text, **kwargs)
    except TelegramAPIError as e:
        logging.warning("Не удалось опубликовать в группе: %s", e)
        return None


async def stars_balance(bot: Bot) -> int | None:
    try:
        return (await bot.get_my_star_balance()).amount
    except TelegramAPIError as e:
        logging.warning("Не удалось получить баланс: %s", e)
        return None


async def bot_base_url(bot: Bot) -> str:
    me = await bot.get_me()
    return f"https://t.me/{me.username}"


async def is_member(bot: Bot, chat_id: int, uid: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, uid)
    except TelegramAPIError as e:
        logging.warning("Проверка участия в группе %s не удалась: %s", chat_id, e)
        return False
    return member.status in ("creator", "administrator", "member")


async def group_url(bot: Bot) -> str | None:
    """Ссылка на группу: по @username или постоянная ссылка-приглашение."""
    try:
        chat = await bot.get_chat(GROUP_CHAT_ID)
    except TelegramAPIError as e:
        logging.warning("Не удалось получить группу %s: %s", GROUP_CHAT_ID, e)
        return None
    if chat.username:
        return f"https://t.me/{chat.username}"
    return chat.invite_link


def leaderboard_text(top: list[dict], title: str = "Топ приглашений недели") -> str:
    if not top:
        return f"{tg('crown')} <b>{title}</b>\n\nПока никто никого не пригласил."
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, row in enumerate(top, 1):
        if row.get("username"):
            name = f"@{row['username']}"
        else:
            name = row.get("first_name") or str(row["user_id"])
        place = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{place} {esc(name)} — {row['cnt']}")
    return f"{tg('crown')} <b>{title}</b>\n\n" + "\n".join(lines)


# ---------- выплаты Stars ----------
# Stars с баланса бота напрямую пользователю не переводятся, поэтому приз выдаётся
# подарками из каталога Telegram на сумму не больше приза (округление вниз).
# Каждая выплата хранится в таблице payouts: если отправка оборвалась, бот продолжит
# с того же подарка, а не выдаст всё заново.

async def build_stars_plan(bot: Bot, amount: int) -> list[str] | None:
    """Список gift_id на сумму не больше amount. None — каталог подарков недоступен."""
    try:
        catalog = await bot.get_available_gifts()
    except TelegramAPIError as e:
        logging.warning("Не удалось получить каталог подарков: %s", e)
        return None

    available = sorted(
        (g for g in catalog.gifts
         if g.star_count > 0 and (g.remaining_count is None or g.remaining_count > 0)),
        key=lambda g: g.star_count,
        reverse=True,
    )
    plan: list[str] = []
    left = amount
    for gift in available:
        limit = gift.remaining_count if gift.remaining_count is not None else MAX_GIFTS_PER_PRIZE
        used = 0
        while left >= gift.star_count and used < limit and len(plan) < MAX_GIFTS_PER_PRIZE:
            plan.append(gift.id)
            left -= gift.star_count
            used += 1
    return plan


async def process_payout(bot: Bot, pid: int) -> bool:
    """Отправляет подарки по выплате. Возвращает True, если выплата полностью выдана."""
    p = await get_payout(pid)
    if not p:
        return False
    if p["status"] == "done":
        return True

    if p["plan"] is None:
        plan_ids = await build_stars_plan(bot, p["amount"])
        if plan_ids is None:
            await update_payout(pid, attempts=p["attempts"] + 1,
                                last_error="каталог подарков недоступен")
            return False
        await update_payout(pid, plan=",".join(plan_ids))
    else:
        plan_ids = [x for x in p["plan"].split(",") if x]

    for index in range(p["sent"], len(plan_ids)):
        try:
            await bot.send_gift(user_id=p["user_id"], gift_id=plan_ids[index], text="Ваш приз!")
        except TelegramAPIError as e:
            await update_payout(pid, sent=index, attempts=p["attempts"] + 1,
                                last_error=str(e)[:300])
            await log_event(bot, f"{tg('no')} Выплата #{pid}: пользователь "
                                 f"<code>{p['user_id']}</code>, {p['amount']} Stars, отправлено "
                                 f"{index} из {len(plan_ids)}. Повтор будет позже. "
                                 f"Ошибка: {esc(str(e))}")
            return False
        await update_payout(pid, sent=index + 1)

    await update_payout(pid, status="done", last_error=None)
    note = "" if plan_ids or p["amount"] == 0 else " ⚠️ сумма меньше стоимости любого подарка"
    await log_event(bot, f"{tg('star')} Выплата #{pid}: {p['amount']} Stars пользователю "
                         f"<code>{p['user_id']}</code> ({len(plan_ids)} подарков). "
                         f"{esc(p['reason'] or '')}{note}")
    return True


async def queue_stars_prize(bot: Bot, uid: int, amount: int, reason: str) -> bool:
    """Ставит выплату в очередь и сразу пробует отправить. True — выдано полностью."""
    if amount <= 0:
        return True
    pid = await create_payout(uid, amount, reason)
    return await process_payout(bot, pid)


async def retry_payouts(bot: Bot) -> None:
    for row in await pending_payouts():
        await process_payout(bot, row["id"])


async def check_pending_referrals(bot: Bot) -> None:
    """Засчитывает приглашения через сутки, если приглашённый всё ещё в группе."""
    for row in await pending_referrals(_now() - REFERRAL_CONFIRM_SECONDS):
        uid = row["referred_id"]
        try:
            member = await bot.get_chat_member(GROUP_CHAT_ID, uid)
        except TelegramAPIError:
            continue  # временная ошибка — проверим в следующий раз
        if member.status in ("creator", "administrator", "member"):
            await confirm_referral(uid)
        else:
            await remove_referral(uid)


# =====================================================================
# ЗАВЕРШЕНИЕ РОЗЫГРЫШЕЙ, ИГР И НЕДЕЛЬ
# =====================================================================

async def finish_giveaway(bot: Bot, gid: int) -> tuple[bool, int | None]:
    """Выбирает случайного победителя. Возвращает (завершён ли сейчас, победитель)."""
    g = await get_giveaway(gid)
    if not g or g["status"] != "active":
        return False, None

    ids = await participant_ids(gid)
    winner = random.choice(ids) if ids else None
    await close_giveaway(gid, winner)
    await log_event(bot, f"{tg('gift')} Розыгрыш #{gid} завершён. Участников: {len(ids)}, "
                         f"победитель: {f'<code>{winner}</code>' if winner else 'нет'}")

    title, prize = g["title"], g["prize"]  # уже HTML с форматированием админа
    if winner:
        w = await get_user(winner)
        await safe_send(
            bot, g["created_by"],
            f"{tg('gift')} Розыгрыш #{gid} «{title}» завершён.\n"
            f"{tg('people')} Участников: {len(ids)}\n"
            f"{tg('crown')} Победитель: {user_link(winner, w)}",
        )
        extra = ""
        if g["prize_stars"]:
            sent = await queue_stars_prize(bot, winner, g["prize_stars"], f"розыгрыш #{gid}")
            extra = (f"\n{tg('star')} {g['prize_stars']} Stars отправлены подарками."
                     if sent else "\nStars поставлены в очередь: бот повторит выдачу автоматически.")
        await safe_send(
            bot, winner,
            f"{tg('hooray')} <b>Поздравляем!</b>\nВы выиграли в розыгрыше «{title}».\n"
            f"Приз: {prize}{extra}\nАдминистратор свяжется с вами.",
        )
    else:
        await safe_send(
            bot, g["created_by"],
            f"{tg('info')} Розыгрыш #{gid} «{title}» завершён, но участников не было.",
        )
    return True, winner


def score_text(kind: str, value: int) -> str:
    if kind in KIND_BY_EMOJI.values() or kind == "roulette":
        return f" — выпало {value}"
    if kind == "guess":
        return f" — назвал {value}"
    return ""


async def finish_game(bot: Bot, gid: int) -> None:
    game = await get_game(gid)
    if not game or game["status"] != "active":
        return
    entries = await ranked_entries(gid, game)
    if game["kind"] == "coin":
        # Победители — только те, кто угадал сторону монеты
        entries = [e for e in entries if str(e["value"]) == game["secret"]]
    winners = entries[: game["winners"]]
    await mark_game_finished(gid, [w["user_id"] for w in winners])
    await log_event(bot, f"{tg('gift')} Игра #{gid} ({game['kind']}) завершена. Победители: "
                         + (", ".join(f"<code>{w['user_id']}</code>" for w in winners) or "нет"))

    # Делаем кнопку неактивной, чтобы в группе было видно, что игра закончилась
    if game["kind"] == "btn" and game["message_id"]:
        try:
            await bot.edit_message_reply_markup(
                chat_id=GROUP_CHAT_ID,
                message_id=game["message_id"],
                reply_markup=kb([btn("Игра завершена", "ok", "noop", disabled=True)]),
            )
        except TelegramAPIError as e:
            logging.warning("Не удалось отключить кнопку игры #%s: %s", gid, e)

    title = game["title"]
    if not winners:
        await post_to_group(bot, f"{tg('info')} Игра «{title}» завершена: участников не было.")
        return

    lines = []
    for place, w in enumerate(winners, 1):
        user = await get_user(w["user_id"])
        score = score_text(game["kind"], w["value"])
        lines.append(f"{place}. {user_link(w['user_id'], user)}{score}")

        extra = ""
        if game["prize_stars"]:
            sent = await queue_stars_prize(bot, w["user_id"], game["prize_stars"],
                                           f"игра #{gid}")
            extra = (f"\n{tg('star')} {game['prize_stars']} Stars отправлены подарками."
                     if sent else "\nStars поставлены в очередь: бот повторит выдачу автоматически.")
        await safe_send(bot, w["user_id"], f"{tg('hooray')} Вы победили в игре «{title}»!{extra}")

    reveal = ""
    if game["kind"] == "guess":
        reveal = f"\nЗагаданное число было: {game['secret']}"
    elif game["kind"] == "word":
        reveal = f"\nСлово было: {esc(game['secret'])}"
    elif game["kind"] == "coin":
        reveal = f"\nВыпало: {'Орёл' if game['secret'] == '0' else 'Решка'}"
    await post_to_group(
        bot,
        f"{tg('crown')} <b>Игра «{title}» завершена!</b>{reveal}\n\n" + "\n".join(lines),
    )


async def close_week(bot: Bot) -> str:
    """Подводит итоги недели по приглашениям и начинает новую неделю."""
    top = await top_referrers(await week_start(), 10)
    await set_week_start(int(time.time()))

    report = leaderboard_text(top, "Итоги недели")
    if top and top[0]["cnt"] > 0:
        winner = top[0]
        sent = await queue_stars_prize(bot, winner["user_id"], WEEKLY_PRIZE_STARS, "итоги недели")
        if sent:
            report += (f"\n\n{tg('crown')} Победитель: {user_link(winner['user_id'], winner)} — "
                       f"приз {WEEKLY_PRIZE_STARS} {tg('star')} отправлен автоматически.")
            await safe_send(
                bot, winner["user_id"],
                f"{tg('hooray')} Вы победитель недели по приглашениям!\n"
                f"Приз {WEEKLY_PRIZE_STARS} {tg('star')} — подарок уже отправлен вам в Telegram.",
            )
        else:
            report += (f"\n\n{tg('crown')} Победитель: {user_link(winner['user_id'], winner)} — "
                       f"{WEEKLY_PRIZE_STARS} {tg('star')}. Приз в очереди: бот повторит отправку "
                       "автоматически, проверьте баланс Stars.")
            await safe_send(
                bot, winner["user_id"],
                f"{tg('hooray')} Вы победитель недели по приглашениям!\n"
                f"Приз: {WEEKLY_PRIZE_STARS} {tg('star')} — отправим в ближайшее время.",
            )
    for admin_id in await all_admin_ids():
        await safe_send(bot, admin_id, report)
    await log_event(bot, f"{tg('crown')} Неделя закрыта. Итоги отправлены админам.")
    return report


async def run_schedule(bot: Bot, sched: dict, today: str) -> None:
    """Публикует игру из расписания (раз в сутки в заданное время)."""
    await mark_schedule_run(sched["id"], today)
    config = json.loads(sched["config"])
    minutes = config.pop("minutes")
    config.pop("label", None)
    await publish_game(bot, sched["admin_id"], config, minutes)


async def join_giveaway(message: Message, bot: Bot, gid: int) -> None:
    uid = message.from_user.id
    if await is_blocked(uid):
        await message.answer(f"{tg('no')} Вы не можете участвовать в розыгрышах.")
        return
    g = await get_giveaway(gid)
    if not g or g["status"] != "active" or g["ends_at"] <= time.time():
        await message.answer(f"{tg('no')} Этот розыгрыш уже завершён или не существует.")
        return

    if await is_participant(gid, uid):
        await message.answer(f"{tg('ok')} Вы уже участвуете в розыгрыше «{g['title']}».")
        return

    if g["channel"] and not await is_member(bot, GROUP_CHAT_ID, uid):
        url = await group_url(bot)
        markup = kb([btn("Вступить в группу", "bell", url=url)]) if url else None
        await message.answer(
            f"{tg('bell')} Чтобы участвовать, сначала вступите в группу, "
            "а затем снова откройте ссылку розыгрыша.",
            reply_markup=markup,
        )
        return

    await add_participant(gid, uid)
    count = await participant_count(gid)
    await message.answer(
        f"{tg('hooray')} <b>Вы участвуете!</b>\n\n"
        f"{tg('gift')} <b>{g['title']}</b>\n"
        f"Приз: {g['prize']}{stars_line(g['prize_stars'])}\n"
        f"{tg('calendar')} Итоги: {fmt_ts(g['ends_at'])}\n"
        f"{tg('people')} Участников сейчас: {count}"
    )


# =====================================================================
# ОБРАБОТЧИКИ
# =====================================================================

router = Router()


# ---------- команды для всех ----------

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, bot: Bot, state: FSMContext):
    await state.clear()
    user = message.from_user
    args = command.args or ""

    await register_user(user.id, user.username, user.first_name, None)

    if args.startswith("g_") and args[2:].isdigit():
        await join_giveaway(message, bot, int(args[2:]))
        return

    if await is_admin(user.id):
        await message.answer(f"{tg('bot')} <b>Панель администратора</b>\nВыберите действие:",
                             reply_markup=admin_menu())
        return

    await message.answer(
        f"{tg('gift')} <b>Розыгрыши и конкурсы</b>\n\n"
        "Участвуйте в розыгрышах и играх, которые публикует администратор.\n"
        f"{tg('add')} /invite — ваша ссылка для приглашений в группу\n"
        f"{tg('crown')} /top — рейтинг приглашений недели\n"
        f"Каждую неделю лучший по приглашениям получает {WEEKLY_PRIZE_STARS} {tg('star')}"
    )


@router.message(Command("invite"))
async def cmd_invite(message: Message, bot: Bot):
    uid = message.from_user.id
    link = await get_invite_link(uid)
    if not link:
        try:
            created = await bot.create_chat_invite_link(GROUP_CHAT_ID, name=f"user {uid}")
        except TelegramAPIError as e:
            logging.warning("Не удалось создать ссылку приглашения: %s", e)
            await message.answer(f"{tg('no')} Не удалось создать ссылку. "
                                 "Администратору нужно проверить права бота в группе.")
            return
        link = created.invite_link
        await save_invite_link(uid, link)

    count = await count_referrals(uid, await week_start())
    await message.answer(
        f"{tg('link')} <b>Ваша ссылка в группу:</b>\n{link}\n\n"
        f"{tg('people')} Приглашено на этой неделе: {count}\n"
        "Приглашённый засчитывается, если пробыл в группе не меньше суток."
    )


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_group_join(event: ChatMemberUpdated):
    """Записывает приглашение, если человек вступил по ссылке конкретного пользователя.
    Засчитывается через сутки (см. check_pending_referrals)."""
    if event.chat.id != GROUP_CHAT_ID or event.invite_link is None:
        return
    if event.new_chat_member.user.is_bot:
        return
    referrer = await referrer_by_link(event.invite_link.invite_link)
    if referrer is None or await is_blocked(referrer):
        return
    await add_referral(referrer, event.new_chat_member.user.id)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_group_leave(event: ChatMemberUpdated):
    if event.chat.id != GROUP_CHAT_ID:
        return
    await remove_referral(event.new_chat_member.user.id)


@router.message(Command("top"))
async def cmd_top(message: Message):
    top = await top_referrers(await week_start(), 10)
    await message.answer(leaderboard_text(top))


# ---------- игры в группе (участники) ----------

@router.message(F.dice)
async def on_dice(message: Message):
    """Бросок 🎲 в группе во время активной игры «Дуэль на кубиках»."""
    kind = KIND_BY_EMOJI.get(message.dice.emoji)
    if message.chat.id != GROUP_CHAT_ID or kind is None:
        return
    if await is_blocked(message.from_user.id):
        return
    game = await active_game(kind)
    if not game:
        return
    if await enter_game(game["id"], message.from_user.id, message.dice.value):
        await message.reply(f"{tg('ok')} Записано: {message.dice.value}")
    else:
        await message.reply(f"{tg('info')} Вы уже бросали кубик в этой игре.")


@router.callback_query(F.data.startswith("gbtn:"))
async def on_game_button(cb: CallbackQuery, bot: Bot):
    """Нажатие кнопки в игре «Первый нажмёт» и «Лотерея»."""
    if await is_blocked(cb.from_user.id):
        await cb.answer("Вы не можете участвовать в играх", show_alert=True)
        return
    gid = int(cb.data.split(":")[1])
    game = await get_game(gid)
    if not game or game["status"] != "active" or game["ends_at"] <= time.time():
        await cb.answer("Игра уже завершена", show_alert=True)
        return
    if game["kind"] == "roulette":
        # Лотерея: каждому выпадает случайное число, побеждают самые большие
        value = random.randint(1, GUESS_MAX)
        if not await enter_game(gid, cb.from_user.id, value):
            await cb.answer("Вы уже крутили рулетку", show_alert=True)
            return
        await cb.answer(f"Вам выпало {value}", show_alert=True)
        return
    if not await enter_game(gid, cb.from_user.id, 0):
        await cb.answer("Вы уже участвуете", show_alert=True)
        return
    await cb.answer("Вы в игре!")
    if await game_entries_count(gid) >= game["winners"]:
        await finish_game(bot, gid)


@router.callback_query(F.data.startswith("gcoin:"))
async def on_coin_choice(cb: CallbackQuery):
    """Выбор стороны монеты в игре «Орёл или решка»."""
    if await is_blocked(cb.from_user.id):
        await cb.answer("Вы не можете участвовать в играх", show_alert=True)
        return
    _, gid_str, side_str = cb.data.split(":")
    gid, side = int(gid_str), int(side_str)
    game = await get_game(gid)
    if not game or game["status"] != "active" or game["ends_at"] <= time.time():
        await cb.answer("Игра уже завершена", show_alert=True)
        return
    if not await enter_game(gid, cb.from_user.id, side):
        await cb.answer("Вы уже сделали выбор", show_alert=True)
        return
    await cb.answer("Ваш выбор принят!")


@router.message(Command("guess"), F.chat.id == GROUP_CHAT_ID)
async def on_guess(message: Message, command: CommandObject):
    """Попытка в игре «Угадай число»: /guess 42."""
    if await is_blocked(message.from_user.id):
        return
    game = await active_game("guess")
    if not game:
        return
    number = parse_int(command.args or "", 1, GUESS_MAX)
    if number is None:
        await message.reply(f"Напишите число от 1 до {GUESS_MAX}, например /guess 42")
        return
    if await enter_game(game["id"], message.from_user.id, number):
        await message.reply(f"{tg('ok')} Принято: {number}")
    else:
        await message.reply(f"{tg('info')} Вы уже делали попытку в этой игре.")


@router.message(F.text, F.chat.id == GROUP_CHAT_ID)
async def on_group_text(message: Message, bot: Bot):
    """Ответы в играх прямо текстом в группе: число для «Угадай число», слово для «Первое слово»."""
    text = message.text.strip()
    if text.startswith("/") or await is_blocked(message.from_user.id):
        return

    guess = await active_game("guess")
    if guess and text.isdigit():
        number = parse_int(text, 1, GUESS_MAX)
        if number is None:
            await message.reply(f"Напишите число от 1 до {GUESS_MAX}.")
            return
        if await enter_game(guess["id"], message.from_user.id, number):
            await message.reply(f"{tg('ok')} Принято: {number}")
        else:
            await message.reply(f"{tg('info')} Вы уже делали попытку в этой игре.")
        return

    # «Первое слово» и «Быстрый счёт»: первые правильные ответы получают призы
    for kind in ("word", "math"):
        game = await active_game(kind)
        if not game or text.lower() != game["secret"].lower():
            continue
        if not await enter_game(game["id"], message.from_user.id, 0):
            return
        await message.reply(f"{tg('ok')} Верно! Вы в призах.")
        if await game_entries_count(game["id"]) >= game["winners"]:
            await finish_game(bot, game["id"])
        return


# ---------- админ: администраторы и чёрный список ----------

async def admins_text() -> str:
    lines = [f"• <code>{uid}</code> (из настроек сервера)" for uid in sorted(ADMIN_IDS)]
    rows = await _all("SELECT user_id FROM admins ORDER BY added_at")
    lines += [f"• <code>{r['user_id']}</code>" for r in rows]
    return (f"{tg('bot')} <b>Администраторы</b>\n\n" + "\n".join(lines) +
            "\n\nДобавить: <code>/addadmin ID</code> или ответом на сообщение человека командой /addadmin"
            "\nУдалить: <code>/removeadmin ID</code>\n"
            "Чёрный список: <code>/block ID</code>, <code>/unblock ID</code>")


def _target_user_id(message: Message, command: CommandObject) -> int | None:
    """ID из аргумента команды или автора сообщения, на которое ответили."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return parse_int(command.args or "", 1, 9_999_999_999_999)


@router.message(Command("admins"), IsAdmin())
async def cmd_admins(message: Message):
    await message.answer(await admins_text(), reply_markup=back_to_menu())


@router.callback_query(F.data == "adm:admins", IsAdmin())
async def cb_admins(cb: CallbackQuery):
    await cb.message.edit_text(await admins_text(), reply_markup=back_to_menu())
    await cb.answer()


@router.message(Command("addadmin"), IsAdmin())
async def cmd_addadmin(message: Message, command: CommandObject, bot: Bot):
    target = _target_user_id(message, command)
    if target is None:
        await message.answer("Укажите ID числом: /addadmin 123456789\n"
                             "Или ответьте командой /addadmin на сообщение нужного человека.")
        return
    if await is_admin(target):
        await message.answer(f"{tg('info')} Этот пользователь уже администратор.")
        return
    await add_admin(target, message.from_user.id)
    await message.answer(f"{tg('ok')} Администратор <code>{target}</code> добавлен.",
                         reply_markup=back_to_menu())
    await log_event(bot, f"{tg('bot')} Админ <code>{message.from_user.id}</code> добавил админа "
                         f"<code>{target}</code>")


@router.message(Command("removeadmin"), IsAdmin())
async def cmd_removeadmin(message: Message, command: CommandObject, bot: Bot):
    target = _target_user_id(message, command)
    if target is None:
        await message.answer("Укажите ID числом: /removeadmin 123456789")
        return
    if target in ADMIN_IDS:
        await message.answer(f"{tg('no')} Этот админ задан в настройках сервера (ADMIN_IDS). "
                             "Его можно убрать только там.")
        return
    if target == message.from_user.id:
        await message.answer(f"{tg('no')} Нельзя удалить самого себя.")
        return
    if not await remove_admin(target):
        await message.answer(f"{tg('info')} Такого админа нет в списке.")
        return
    await message.answer(f"{tg('ok')} Администратор <code>{target}</code> удалён.",
                         reply_markup=back_to_menu())
    await log_event(bot, f"{tg('bot')} Админ <code>{message.from_user.id}</code> удалил админа "
                         f"<code>{target}</code>")


@router.message(Command("block"), IsAdmin())
async def cmd_block(message: Message, command: CommandObject, bot: Bot):
    target = _target_user_id(message, command)
    if target is None:
        await message.answer("Укажите ID числом: /block 123456789 "
                             "или ответьте командой на сообщение нужного человека.")
        return
    if await is_admin(target):
        await message.answer(f"{tg('no')} Админа нельзя добавить в чёрный список.")
        return
    await block_user(target)
    await message.answer(f"{tg('ok')} Пользователь <code>{target}</code> в чёрном списке: "
                         "не участвует в играх и розыгрышах.", reply_markup=back_to_menu())
    await log_event(bot, f"{tg('no')} Админ <code>{message.from_user.id}</code> заблокировал "
                         f"<code>{target}</code>")


@router.message(Command("unblock"), IsAdmin())
async def cmd_unblock(message: Message, command: CommandObject, bot: Bot):
    target = _target_user_id(message, command)
    if target is None:
        await message.answer("Укажите ID числом: /unblock 123456789")
        return
    if not await unblock_user(target):
        await message.answer(f"{tg('info')} Этого пользователя нет в чёрном списке.")
        return
    await message.answer(f"{tg('ok')} Пользователь <code>{target}</code> разблокирован.",
                         reply_markup=back_to_menu())
    await log_event(bot, f"{tg('ok')} Админ <code>{message.from_user.id}</code> разблокировал "
                         f"<code>{target}</code>")


# ---------- админ: панель ----------

@router.message(Command("admin"), IsAdmin())
async def cmd_admin(message: Message):
    await message.answer(f"{tg('bot')} <b>Панель администратора</b>\nВыберите действие:",
                         reply_markup=admin_menu())


@router.callback_query(F.data == "adm:menu", IsAdmin())
async def cb_menu(cb: CallbackQuery):
    await cb.message.edit_text(f"{tg('bot')} <b>Панель администратора</b>\nВыберите действие:",
                               reply_markup=admin_menu())
    await cb.answer()


@router.callback_query(F.data == "adm:games", IsAdmin())
async def cb_games(cb: CallbackQuery):
    await cb.message.edit_text(f"{tg('gift')} <b>Игры</b>\nВыберите тип игры:",
                               reply_markup=games_menu())
    await cb.answer()


@router.callback_query(F.data == "adm:stats", IsAdmin())
async def cb_stats(cb: CallbackQuery):
    s = await bot_stats()
    text = (f"{tg('info')} <b>Статистика</b>\n\n"
            f"Пользователей: {s['users']}\n"
            f"Розыгрышей: {s['giveaways']} (активных: {s['giveaways_active']})\n"
            f"Игр: {s['games']}, участий в играх: {s['game_entries']}\n"
            f"Приглашений подтверждено: {s['referrals_all']}, за эту неделю: {s['referrals_week']}\n"
            f"Выплат в очереди: {s['payouts_pending']}\n"
            f"В чёрном списке: {s['blocked']}")
    await cb.message.edit_text(text, reply_markup=back_to_menu())
    await cb.answer()


# ---------- админ: розыгрыши ----------

@router.callback_query(F.data == "adm:list", IsAdmin())
async def cb_list(cb: CallbackQuery):
    rows = await list_active_giveaways()
    if rows:
        text = f"{tg('calendar')} <b>Активные розыгрыши</b>"
        markup = active_list(rows)
    else:
        text = f"{tg('calendar')} Активных розыгрышей нет."
        markup = back_to_menu()
    await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data.startswith("adm:g:"), IsAdmin())
async def cb_giveaway(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = await get_giveaway(gid)
    if not g:
        await cb.answer("Розыгрыш не найден", show_alert=True)
        return
    count = await participant_count(gid)
    status = {"active": "активен", "finished": "завершён", "cancelled": "отменён"}.get(
        g["status"], g["status"])
    await cb.message.edit_text(
        f"{tg('gift')} <b>#{gid} {g['title']}</b>\n"
        f"Приз: {g['prize']}{stars_line(g['prize_stars'])}\n"
        f"Статус: {status}\n"
        f"{tg('calendar')} Итоги: {fmt_ts(g['ends_at'])}\n"
        f"{tg('people')} Участников: {count}",
        reply_markup=giveaway_menu(gid),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:fin:"), IsAdmin())
async def cb_finish(cb: CallbackQuery, bot: Bot):
    gid = int(cb.data.split(":")[2])
    done, _ = await finish_giveaway(bot, gid)
    if not done:
        await cb.answer("Розыгрыш уже завершён", show_alert=True)
        return
    await cb.message.edit_text(f"{tg('ok')} Розыгрыш #{gid} завершён. Результат отправлен.",
                               reply_markup=back_to_menu())
    await cb.answer()


@router.callback_query(F.data.startswith("adm:cancel:"), IsAdmin())
async def cb_cancel_giveaway(cb: CallbackQuery, bot: Bot):
    gid = int(cb.data.split(":")[2])
    g = await get_giveaway(gid)
    if not g or not await cancel_giveaway(gid):
        await cb.answer("Нельзя отменить: розыгрыш уже завершён или отменён", show_alert=True)
        return
    for uid in await participant_ids(gid):
        await safe_send(bot, uid, f"{tg('no')} Розыгрыш #{gid} «{g['title']}» отменён администратором.")
    await log_event(bot, f"{tg('no')} Розыгрыш #{gid} отменён админом "
                         f"<code>{cb.from_user.id}</code>")
    await cb.message.edit_text(f"{tg('no')} Розыгрыш #{gid} отменён. Участники уведомлены.",
                               reply_markup=back_to_menu())
    await cb.answer()


@router.callback_query(F.data.startswith("adm:et:"), IsAdmin())
async def cb_edit_giveaway(cb: CallbackQuery, state: FSMContext):
    _, _, gid_str, field = cb.data.split(":")
    await state.set_state(EditGiveaway.value)
    await state.update_data(gid=int(gid_str), field=field)
    label = "новое название" if field == "title" else "новое описание приза"
    await cb.message.answer(f"Напишите {label} розыгрыша #{gid_str}.\n"
                            "В уже опубликованном в группе сообщении текст не обновится.")
    await cb.answer()


@router.message(StateFilter(EditGiveaway.value), IsAdmin(), F.text)
async def edit_giveaway_value(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    limit = 100 if data["field"] == "title" else 200
    value = rich_input(message, limit)
    if value is None:
        await message.answer(f"Текст должен быть непустым и не длиннее {limit} символов.")
        return
    await state.clear()
    if await update_giveaway_text(data["gid"], data["field"], value):
        await message.answer(f"{tg('ok')} Сохранено.", reply_markup=back_to_menu())
        await log_event(bot, f"{tg('gift')} Розыгрыш #{data['gid']}: изменено поле "
                             f"«{data['field']}» админом <code>{message.from_user.id}</code>")
    else:
        await message.answer(f"{tg('no')} Розыгрыш уже завершён или отменён.",
                             reply_markup=back_to_menu())


@router.callback_query(F.data.startswith("adm:csv:"), IsAdmin())
async def cb_export(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    rows = await export_participants(gid)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "username", "first_name", "joined_at"])
    for r in rows:
        writer.writerow([r["user_id"], r["username"] or "", r["first_name"] or "",
                         fmt_ts(r["joined_at"])])
    document = BufferedInputFile(buf.getvalue().encode("utf-8-sig"),
                                 filename=f"giveaway_{gid}.csv")
    await cb.message.answer_document(document, caption=f"Участников: {len(rows)}")
    await cb.answer()


@router.callback_query(F.data == "adm:week", IsAdmin())
async def cb_week(cb: CallbackQuery):
    top = await top_referrers(await week_start(), 10)
    await cb.message.edit_text(leaderboard_text(top), reply_markup=back_to_menu())
    await cb.answer()


@router.callback_query(F.data == "adm:closeweek", IsAdmin())
async def cb_close_week(cb: CallbackQuery, bot: Bot):
    report = await close_week(bot)
    await cb.message.edit_text(report + f"\n\n{tg('ok')} Новая неделя началась.",
                               reply_markup=back_to_menu())
    await cb.answer()


@router.callback_query(F.data == "adm:bal", IsAdmin())
async def cb_balance(cb: CallbackQuery, bot: Bot):
    balance = await stars_balance(bot)
    if balance is None:
        text = f"{tg('no')} Не удалось получить баланс Stars."
    else:
        text = f"{tg('star')} <b>Баланс бота:</b> {balance} Stars"
    await cb.message.edit_text(text, reply_markup=balance_menu())
    await cb.answer()


@router.callback_query(F.data == "adm:topup", IsAdmin())
async def cb_topup(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TopUp.amount)
    await cb.message.answer(
        f"{tg('star')} <b>Пополнение баланса</b>\n\n"
        f"Напишите количество Stars (от 1 до {MAX_TOPUP_STARS}). "
        "Бот пришлёт счёт, который нужно оплатить."
    )
    await cb.answer()


@router.message(StateFilter(TopUp.amount), IsAdmin(), F.text)
async def topup_amount(message: Message, state: FSMContext, bot: Bot):
    amount = parse_int(message.text, 1, MAX_TOPUP_STARS)
    if amount is None:
        await message.answer(f"Введите целое число от 1 до {MAX_TOPUP_STARS}, например 500.")
        return
    await state.clear()
    await bot.send_invoice(
        chat_id=message.from_user.id,
        title="Пополнение баланса бота",
        description=f"Пополнение баланса бота на {amount} Stars",
        payload=TOPUP_PAYLOAD,
        currency="XTR",
        prices=[LabeledPrice(label="Stars", amount=amount)],
        provider_token="",
    )
    await log_event(bot, f"{tg('star')} Админ <code>{message.from_user.id}</code> создал счёт "
                         f"на пополнение {amount} Stars")


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    # Одобряем только наш счёт и только от админа
    ok = query.invoice_payload == TOPUP_PAYLOAD and await is_admin(query.from_user.id)
    await query.answer(ok=ok, error_message=None if ok else "Этот счёт недоступен.")


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot):
    payment = message.successful_payment
    if payment.invoice_payload != TOPUP_PAYLOAD:
        return
    balance = await stars_balance(bot)
    balance_text = f" Текущий баланс: {balance} Stars." if balance is not None else ""
    await message.answer(
        f"{tg('ok')} Баланс пополнен на {payment.total_amount} {tg('star')}.{balance_text}"
    )
    await log_event(bot, f"{tg('star')} Пополнение баланса: +{payment.total_amount} Stars "
                         f"от <code>{message.from_user.id}</code>.{balance_text}")


# ---------- админ: создание розыгрыша ----------

@router.callback_query(F.data == "adm:new", IsAdmin())
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewGiveaway.title)
    await cb.message.answer(f"{tg('gift')} <b>Новый розыгрыш</b>\n\n"
                            "Шаг 1/5: напишите название розыгрыша.")
    await cb.answer()


@router.message(StateFilter(NewGiveaway.title), IsAdmin(), F.text)
async def new_title(message: Message, state: FSMContext):
    title = rich_input(message, 100)
    if title is None:
        await message.answer("Название должно быть непустым и не длиннее 100 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(NewGiveaway.prize)
    await message.answer("Шаг 2/5: что за приз? Напишите текстом.")


@router.message(StateFilter(NewGiveaway.prize), IsAdmin(), F.text)
async def new_prize(message: Message, state: FSMContext):
    prize = rich_input(message, 200)
    if prize is None:
        await message.answer("Описание приза должно быть непустым и не длиннее 200 символов.")
        return
    await state.update_data(prize=prize)
    await state.set_state(NewGiveaway.stars)
    await message.answer(
        "Шаг 3/5: сколько Stars получит победитель? Введите число от 0 до 100000. "
        "0 — без Stars.\nStars выдаются подарками, поэтому сумма округляется вниз."
    )


@router.message(StateFilter(NewGiveaway.stars), IsAdmin(), F.text)
async def new_stars(message: Message, state: FSMContext):
    stars = parse_int(message.text, 0, 100_000)
    if stars is None:
        await message.answer("Введите целое число от 0 до 100000.")
        return
    await state.update_data(stars=stars)
    await state.set_state(NewGiveaway.hours)
    await message.answer("Шаг 4/5: через сколько часов подвести итоги? Например: 24 или 0.5")


@router.message(StateFilter(NewGiveaway.hours), IsAdmin(), F.text)
async def new_hours(message: Message, state: FSMContext):
    hours = parse_float(message.text, 0.01, 24 * 90)
    if hours is None:
        await message.answer("Введите число часов больше нуля, например 24.")
        return
    await state.update_data(ends_at=time.time() + hours * 3600)
    await state.set_state(NewGiveaway.channel)
    await message.answer("Шаг 5/5: участники должны быть в группе?\nНапишите «да» или «нет».")


@router.message(StateFilter(NewGiveaway.channel), IsAdmin(), F.text)
async def new_channel(message: Message, state: FSMContext, bot: Bot):
    answer = message.text.strip().lower()
    if answer not in ("да", "нет"):
        await message.answer("Напишите «да» или «нет».")
        return
    must_be_member = answer == "да"

    data = await state.get_data()
    balance = await stars_balance(bot)
    if balance is not None and balance < data["stars"]:
        await state.clear()
        await message.answer(f"{tg('no')} Не хватает Stars на балансе бота: приз {data['stars']}, "
                             f"на балансе {balance}. Пополните баланс и создайте розыгрыш заново.",
                             reply_markup=back_to_menu())
        return

    gid = await create_giveaway(
        data["title"], data["prize"], data["stars"],
        str(GROUP_CHAT_ID) if must_be_member else None,
        int(data["ends_at"]), message.from_user.id,
    )
    await state.clear()
    await log_event(bot, f"{tg('gift')} Создан розыгрыш #{gid} «{data['title']}», приз: "
                         f"{data['prize']}, Stars: {data['stars']}, создал "
                         f"<code>{message.from_user.id}</code>")

    link = f"{await bot_base_url(bot)}?start=g_{gid}"
    note = (f"\n\n{tg('info')} Участвовать могут только участники группы. "
            "Бот должен быть в группе и иметь права админа."
            if must_be_member else "")
    await message.answer(
        f"{tg('ok')} <b>Розыгрыш #{gid} создан</b>\n\n"
        f"{tg('gift')} {data['title']}\nПриз: {data['prize']}"
        f"{stars_line(data['stars'])}\n"
        f"{tg('calendar')} Итоги: {fmt_ts(data['ends_at'])}\n\n"
        f"{tg('link')} Ссылка для участников:\n{link}{note}",
        reply_markup=kb(
            [btn("Открыть ссылку", "link", url=link)],
            [btn("Опубликовать в группе", "megaphone", f"adm:pub:{gid}")],
            [btn("В меню", "back", "adm:menu")],
        ),
    )


@router.callback_query(F.data.startswith("adm:pub:"), IsAdmin())
async def cb_publish(cb: CallbackQuery, bot: Bot):
    gid = int(cb.data.split(":")[2])
    g = await get_giveaway(gid)
    if not g:
        await cb.answer("Розыгрыш не найден", show_alert=True)
        return
    link = f"{await bot_base_url(bot)}?start=g_{gid}"
    text = (
        f"{tg('gift')} <b>Розыгрыш «{g['title']}»</b>\n"
        f"Приз: {g['prize']}{stars_line(g['prize_stars'])}\n"
        f"{tg('calendar')} Итоги: {fmt_ts(g['ends_at'])}\n\n"
        "Нажмите кнопку, чтобы участвовать."
    )
    sent = await post_to_group(bot, text, reply_markup=kb([btn("Участвовать", "gift", url=link)]))
    if sent:
        await cb.answer("Опубликовано в группе")
    else:
        await cb.answer("Не удалось опубликовать. Проверьте, что бот в группе.", show_alert=True)


# ---------- админ: создание игры ----------

@router.callback_query(F.data.startswith("adm:game:"), IsAdmin())
async def cb_game_new(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[2]
    if kind not in GAME_TITLES:
        await cb.answer("Неизвестная игра", show_alert=True)
        return
    await state.update_data(kind=kind)
    await state.set_state(NewGame.title)
    await cb.message.answer(f"{tg('gift')} <b>{GAME_TITLES[kind]}</b>\n\nНапишите название игры.")
    await cb.answer()


@router.message(StateFilter(NewGame.title), IsAdmin(), F.text)
async def game_title(message: Message, state: FSMContext):
    title = rich_input(message, 100)
    if title is None:
        await message.answer("Название должно быть непустым и не длиннее 100 символов.")
        return
    await state.update_data(title=title)
    if (await state.get_data())["kind"] == "word":
        await state.set_state(NewGame.hint)
        await message.answer("Напишите подсказку, которую увидят участники. "
                             "Само слово в подсказку не пишите.")
        return
    await state.set_state(NewGame.stars)
    await message.answer("Сколько Stars получит каждый победитель? "
                         "Введите число от 0 до 100000. 0 — без Stars.")


@router.message(StateFilter(NewGame.hint), IsAdmin(), F.text)
async def game_hint(message: Message, state: FSMContext):
    hint = rich_input(message, 200)
    if hint is None:
        await message.answer("Подсказка должна быть непустой и не длиннее 200 символов.")
        return
    await state.update_data(hint=hint)
    await state.set_state(NewGame.secret)
    await message.answer("Теперь напишите слово-ответ. Его увидите только вы.")


@router.message(StateFilter(NewGame.secret), IsAdmin(), F.text)
async def game_secret(message: Message, state: FSMContext):
    secret = message.text.strip().lower()
    if not secret or " " in secret or len(secret) > 40:
        await message.answer("Слово-ответ — одно слово до 40 символов, без пробелов.")
        return
    await state.update_data(secret=secret)
    await state.set_state(NewGame.stars)
    await message.answer("Сколько Stars получит каждый победитель? "
                         "Введите число от 0 до 100000. 0 — без Stars.")


@router.message(StateFilter(NewGame.stars), IsAdmin(), F.text)
async def game_stars(message: Message, state: FSMContext):
    stars = parse_int(message.text, 0, 100_000)
    if stars is None:
        await message.answer("Введите целое число от 0 до 100000.")
        return
    await state.update_data(stars=stars)
    await state.set_state(NewGame.winners)
    await message.answer("Сколько будет победителей? Введите число от 1 до 50.")


@router.message(StateFilter(NewGame.winners), IsAdmin(), F.text)
async def game_winners(message: Message, state: FSMContext):
    winners = parse_int(message.text, 1, 50)
    if winners is None:
        await message.answer("Введите число от 1 до 50.")
        return
    await state.update_data(winners=winners)
    await state.set_state(NewGame.minutes)
    await message.answer("Через сколько минут завершить игру? Например: 10 или 2.5")


@router.message(StateFilter(NewGame.minutes), IsAdmin(), F.text)
async def game_minutes(message: Message, state: FSMContext):
    minutes = parse_float(message.text, 0.5, 7 * 24 * 60)
    if minutes is None:
        await message.answer("Введите число минут от 0.5 до 10080.")
        return
    await state.update_data(minutes=minutes)
    await state.set_state(NewGame.schedule)
    await message.answer(
        "Когда публиковать игру?\n"
        "Напишите «сейчас» — опубликовать сразу, или время ЧЧ:ММ по Самарканду, например 20:00 — "
        "тогда игра будет публиковаться каждый день в это время."
    )


@router.message(StateFilter(NewGame.schedule), IsAdmin(), F.text)
async def game_schedule(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    text = message.text.strip().lower()
    needed = data["stars"] * data["winners"]
    balance = await stars_balance(bot)
    if balance is not None and balance < needed:
        await state.clear()
        await message.answer(f"{tg('no')} Не хватает Stars на балансе бота: нужно {needed}, "
                             f"на балансе {balance}. Пополните баланс и создайте игру заново.",
                             reply_markup=back_to_menu())
        return

    if text == "сейчас":
        await state.clear()
        gid, ok = await publish_game(bot, message.from_user.id, data, data["minutes"])
        if ok:
            await message.answer(f"{tg('ok')} Игра #{gid} опубликована в группе.",
                                 reply_markup=back_to_menu())
        else:
            await message.answer(f"{tg('no')} Игру не удалось опубликовать. "
                                 "Проверьте, что бот есть в группе и может писать в неё.",
                                 reply_markup=back_to_menu())
        return

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        await message.answer("Напишите «сейчас» или время в формате ЧЧ:ММ, например 20:00.")
        return
    hhmm = f"{int(m[1]):02d}:{m[2]}"
    config = {**data, "label": plain(data["title"])[:40]}
    await state.clear()
    sid = await create_schedule(message.from_user.id, hhmm, config)
    await message.answer(
        f"{tg('ok')} Игра «{plain(data['title'])[:40]}» будет публиковаться каждый день в {hhmm} "
        f"(расписание #{sid}). Список и удаление — в разделе «Игры».",
        reply_markup=back_to_menu(),
    )
    await log_event(bot, f"{tg('calendar')} Создано расписание #{sid}: игра «{plain(data['title'])[:40]}» "
                         f"каждый день в {hhmm}, админ <code>{message.from_user.id}</code>")


async def publish_game(bot: Bot, admin_id: int, data: dict, minutes: float) -> tuple[int, bool]:
    """Создаёт игру и публикует её в группе. Возвращает (id игры, опубликована ли)."""
    kind = data["kind"]
    secret = None
    question = ""
    if kind == "guess":
        secret = str(random.randint(1, GUESS_MAX))
    elif kind == "word":
        secret = data["secret"]
    elif kind == "math":
        a, b = random.randint(2, 30), random.randint(2, 12)
        op = random.choice(["+", "-", "×"])
        if op == "-" and a < b:
            a, b = b, a
        secret = str({"+": a + b, "-": a - b, "×": a * b}[op])
        question = f"{a} {op} {b}"
    elif kind == "coin":
        secret = str(random.randint(0, 1))  # 0 — орёл, 1 — решка
    gid = await create_game(
        kind, data["title"], data["stars"], data["winners"],
        int(time.time() + minutes * 60), admin_id, secret,
    )

    title = data["title"]
    prize = stars_line(data["stars"])
    markup = None
    if kind == "btn":
        text = (
            f"{tg('bell')} <b>Игра «{title}»</b>\n\n"
            f"Нажмите кнопку первыми. Первые {data['winners']} получат приз.\n"
            f"Время игры: {minutes:g} мин.{prize}"
        )
        markup = kb([btn("Забрать приз", "gift", f"gbtn:{gid}")])
    elif kind == "guess":
        text = (
            f"{tg('search')} <b>Игра «{title}»</b>\n\n"
            f"Загадано число от 1 до {GUESS_MAX}. Просто напишите его в группу сообщением.\n"
            f"Ближе всех к загаданному выигрывает. Время: {minutes:g} мин.\n"
            f"Победителей: {data['winners']}{prize}"
        )
    elif kind == "word":
        text = (
            f"{tg('info')} <b>Игра «{title}»</b>\n\n"
            f"Подсказка: {data['hint']}\n"
            f"Первые {data['winners']} правильно ответившие в группе получат приз. "
            f"Время: {minutes:g} мин.{prize}"
        )
    elif kind == "roulette":
        text = (
            f"{tg('gift')} <b>Игра «{title}»</b>\n\n"
            f"Нажмите «Крутить»: каждому выпадает число от 1 до {GUESS_MAX}. "
            f"Побеждают самые большие. Время: {minutes:g} мин.\n"
            f"Победителей: {data['winners']}{prize}"
        )
        markup = kb([btn("Крутить", "gift", f"gbtn:{gid}")])
    elif kind == "math":
        text = (
            f"{tg('info')} <b>Игра «{title}»</b>\n\n"
            f"Решите пример первыми и напишите ответ в группу:\n<b>{question} = ?</b>\n"
            f"Первые {data['winners']} правильно ответившие получат приз. "
            f"Время: {minutes:g} мин.{prize}"
        )
    elif kind == "coin":
        text = (
            f"{tg('gift')} <b>Игра «{title}»</b>\n\n"
            f"Выберите сторону монеты. Монету подбросим в конце, угадавшие получат приз.\n"
            f"Время: {minutes:g} мин.\nПобедителей: {data['winners']}{prize}"
        )
        markup = kb([btn("Орёл", None, f"gcoin:{gid}:0"), btn("Решка", None, f"gcoin:{gid}:1")])
    else:  # броски: dice, darts, basket, football, bowling, slots
        text = (
            f"{tg('gift')} <b>Игра «{title}»</b>\n\n"
            f"Отправьте {EMOJI_BY_KIND[kind]} прямо в эту группу в течение {minutes:g} мин. "
            f"Побеждают самые большие значения. Один бросок на человека.\n"
            f"Победителей: {data['winners']}{prize}"
        )

    sent = await post_to_group(bot, text, reply_markup=markup)
    await log_event(bot, f"{tg('gift')} Создана игра #{gid} ({kind}), Stars: {data['stars']}, "
                         f"победителей: {data['winners']}, создал <code>{admin_id}</code>"
                         + ("" if sent else " — НЕ опубликована в группе"))
    if sent:
        await set_game_message(gid, sent.message_id)
    return gid, sent is not None


# ---------- админ: активные игры и расписание ----------

@router.callback_query(F.data == "adm:glist", IsAdmin())
async def cb_game_list(cb: CallbackQuery):
    rows = await list_active_games()
    if not rows:
        await cb.message.edit_text(f"{tg('calendar')} Активных игр нет.",
                                   reply_markup=kb([btn("Назад", "back", "adm:games")]))
    else:
        buttons = [[btn(f"#{r['id']} {plain(r['title'])[:24]} · {r['cnt']} уч. — отменить",
                        "no", f"adm:gc:{r['id']}")] for r in rows]
        buttons.append([btn("Назад", "back", "adm:games")])
        await cb.message.edit_text(f"{tg('calendar')} <b>Активные игры</b>\nНажмите, чтобы отменить:",
                                   reply_markup=kb(*buttons))
    await cb.answer()


@router.callback_query(F.data.startswith("adm:gc:"), IsAdmin())
async def cb_game_cancel(cb: CallbackQuery, bot: Bot):
    gid = int(cb.data.split(":")[2])
    if not await cancel_game(gid):
        await cb.answer("Игра уже завершена или отменена", show_alert=True)
        return
    await log_event(bot, f"{tg('no')} Игра #{gid} отменена админом <code>{cb.from_user.id}</code>")
    await cb.message.edit_text(f"{tg('no')} Игра #{gid} отменена. Призы не выдаются.",
                               reply_markup=kb([btn("Назад", "back", "adm:glist")]))
    await cb.answer()


async def _show_schedules(cb: CallbackQuery) -> None:
    rows = await list_schedules()
    if not rows:
        await cb.message.edit_text(
            f"{tg('calendar')} Расписание пусто. Создайте игру и укажите время вместо «сейчас».",
            reply_markup=kb([btn("Назад", "back", "adm:games")]))
        return
    buttons = [[btn(f"{r['hhmm']} · {json.loads(r['config'])['label'][:30]} — удалить",
                    "no", f"adm:sdel:{r['id']}")] for r in rows]
    buttons.append([btn("Назад", "back", "adm:games")])
    await cb.message.edit_text(f"{tg('calendar')} <b>Расписание игр</b> (каждый день):",
                               reply_markup=kb(*buttons))


@router.callback_query(F.data == "adm:sched", IsAdmin())
async def cb_schedules(cb: CallbackQuery):
    await _show_schedules(cb)
    await cb.answer()


@router.callback_query(F.data.startswith("adm:sdel:"), IsAdmin())
async def cb_schedule_delete(cb: CallbackQuery, bot: Bot):
    sid = int(cb.data.split(":")[2])
    if not await delete_schedule(sid):
        await cb.answer("Уже удалено", show_alert=True)
        return
    await log_event(bot, f"{tg('no')} Расписание #{sid} удалено админом <code>{cb.from_user.id}</code>")
    await _show_schedules(cb)
    await cb.answer("Удалено")


# =====================================================================
# ФОНОВЫЙ ЦИКЛ И ЗАПУСК
# =====================================================================

async def background_loop(bot: Bot) -> None:
    while True:
        try:
            for row in await due_giveaways(time.time()):
                await finish_giveaway(bot, row["id"])
            for row in await due_games(time.time()):
                await finish_game(bot, row["id"])
            if time.time() - await week_start() >= WEEK:
                await close_week(bot)
            await retry_payouts(bot)
            await check_pending_referrals(bot)
            now = datetime.now(TZ)
            for sched in await due_schedules(now.strftime("%H:%M"), now.strftime("%Y-%m-%d")):
                await run_schedule(bot, sched, now.strftime("%Y-%m-%d"))
        except Exception as e:
            logging.exception("Ошибка в фоновом цикле")
            await log_event(bot, f"{tg('no')} Ошибка в фоновом цикле: {esc(repr(e))}")
        await asyncio.sleep(20)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN")
    if not GROUP_CHAT_ID:
        raise SystemExit("Задайте переменную окружения GROUP_CHAT_ID "
                         "(ID группы, например -1001234567890)")

    await init_db()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await log_event(bot, f"{tg('bot')} Бот запущен.")
    background = asyncio.create_task(background_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        background.cancel()


if __name__ == "__main__":
    asyncio.run(main())
