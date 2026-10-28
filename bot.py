"""Telegram-бот розыгрышей и игр для группы (aiogram 3, SQLite).

Всё в одном файле. Настройка через переменные окружения:
    BOT_TOKEN       токен от @BotFather
    ADMIN_IDS       ID админов через запятую
    GROUP_CHAT_ID   ID группы, например -1001234567890
    DB_PATH         путь к базе (по умолчанию giveaway.db)
    WEEKLY_PRIZE_STARS  приз недели в Stars (по умолчанию 100)

Запуск:  python bot.py
"""

import asyncio
import html
import logging
import os
import random
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "9212568185:AAGGOWoK6151PrThW086zq_UUrWevCEfZPc")
# ID администраторов через запятую, например: ADMIN_IDS=123456789,987654321
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "8615345751").replace(" ", "").split(",") if x}
# ID группы, например -1001234567890. Бот должен быть в ней админом с правом приглашать.
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "-1004419166138"))
DB_PATH = os.getenv("DB_PATH", "giveaway.db")
WEEKLY_PRIZE_STARS = int(os.getenv("WEEKLY_PRIZE_STARS", "100"))
TZ = timezone(timedelta(hours=5))  # Asia/Samarkand

WEEK = 7 * 24 * 3600
MAX_GIFTS_PER_PRIZE = 50  # защита от слишком длинного списка подарков
MAX_TOPUP_STARS = 10_000
TOPUP_PAYLOAD = "bot_topup"
GAME_TITLES = {"dice": "Дуэль на кубиках 🎲", "btn": "Первый нажмёт"}

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
    message_id INTEGER
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
"""


def _now() -> int:
    return int(time.time())


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        # Миграция для баз, созданных до призов в Stars у розыгрышей
        try:
            await conn.execute(
                "ALTER TABLE giveaways ADD COLUMN prize_stars INTEGER NOT NULL DEFAULT 0"
            )
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
                "INSERT OR IGNORE INTO referrals VALUES (?, ?, ?)",
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
    """Засчитывает приглашение. Каждого человека можно засчитать только один раз."""
    if referrer_id == referred_id:
        return False
    return await _exec(
        "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at) VALUES (?, ?, ?)",
        (referrer_id, referred_id, _now()),
    ) > 0


async def count_referrals(user_id: int, since: int) -> int:
    row = await _one(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND created_at >= ?",
        (user_id, since),
    )
    return row["c"]


async def top_referrers(since: int, limit: int = 10) -> list[dict]:
    return await _all(
        """
        SELECT r.referrer_id AS user_id, u.username, u.first_name, COUNT(*) AS cnt
        FROM referrals r
        LEFT JOIN users u ON u.user_id = r.referrer_id
        WHERE r.created_at >= ?
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


async def close_giveaway(gid: int, winner_id: int | None) -> None:
    await _exec(
        "UPDATE giveaways SET status = 'finished', winner_id = ? WHERE id = ?",
        (winner_id, gid),
    )


# ---------- игры ----------

async def create_game(kind: str, title: str, prize_stars: int, winners: int,
                      ends_at: int, created_by: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO games (kind, title, prize_stars, winners, ends_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, title, prize_stars, winners, ends_at, created_by),
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


async def has_entered(gid: int, user_id: int) -> bool:
    return await _one(
        "SELECT 1 AS x FROM game_entries WHERE game_id = ? AND user_id = ?",
        (gid, user_id),
    ) is not None


async def game_entries_count(gid: int) -> int:
    row = await _one("SELECT COUNT(*) AS c FROM game_entries WHERE game_id = ?", (gid,))
    return row["c"]


async def ranked_entries(gid: int, kind: str) -> list[dict]:
    """Участники по порядку призовых мест: для кубиков — по убыванию значения,
    для кнопки — в порядке нажатия. При равенстве выигрывает тот, кто раньше."""
    order = "value DESC, created_at, rowid" if kind == "dice" else "created_at, rowid"
    return await _all(
        f"SELECT user_id, value FROM game_entries WHERE game_id = ? ORDER BY {order}",
        (gid,),
    )


async def mark_game_finished(gid: int, winner_ids: list[int]) -> None:
    for uid in winner_ids:
        await _exec("UPDATE game_entries SET won = 1 WHERE game_id = ? AND user_id = ?",
                    (gid, uid))
    await _exec("UPDATE games SET status = 'finished' WHERE id = ?", (gid,))


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
    )


def games_menu() -> InlineKeyboardMarkup:
    return kb(
        [btn("Дуэль на кубиках 🎲", "gift", "adm:game:dice")],
        [btn("Первый нажмёт", "bell", "adm:game:btn")],
        [btn("В меню", "back", "adm:menu")],
    )


def publish_menu(gid: int) -> InlineKeyboardMarkup:
    return kb(
        [btn("Опубликовать в группе", "megaphone", f"adm:pub:{gid}")],
        [btn("В меню", "back", "adm:menu")],
    )


def balance_menu() -> InlineKeyboardMarkup:
    return kb(
        [btn("Пополнить баланс", "star", "adm:topup")],
        [btn("В меню", "back", "adm:menu")],
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return kb([btn("В меню", "back", "adm:menu")])


def active_list(rows: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [btn(f"#{r['id']} {r['title'][:28]} · {r['cnt']} уч.", "gift", f"adm:g:{r['id']}")]
        for r in rows
    ]
    buttons.append([btn("Назад", "back", "adm:menu")])
    return kb(*buttons)


def giveaway_menu(gid: int) -> InlineKeyboardMarkup:
    return kb(
        [btn("Завершить сейчас", "no", f"adm:fin:{gid}")],
        [btn("Назад", "back", "adm:list")],
    )


# =====================================================================
# ОБЩИЕ ПОМОЩНИКИ
# =====================================================================

class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and event.from_user.id in ADMIN_IDS


class NewGiveaway(StatesGroup):
    title = State()
    prize = State()
    stars = State()
    hours = State()
    channel = State()


class NewGame(StatesGroup):
    title = State()
    stars = State()
    winners = State()
    minutes = State()


class TopUp(StatesGroup):
    amount = State()


def esc(text: str | None) -> str:
    return html.escape(text or "")


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


async def post_to_group(bot: Bot, text: str, **kwargs):
    try:
        return await bot.send_message(GROUP_CHAT_ID, text, **kwargs)
    except TelegramAPIError as e:
        logging.warning("Не удалось опубликовать в группе: %s", e)
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


async def send_stars_prize(bot: Bot, uid: int, amount: int) -> bool:
    """Выдаёт приз в Stars подарками из каталога Telegram на сумму не больше amount.

    Stars с баланса бота напрямую пользователю не переводятся, поэтому приз выдаётся
    подарками. Их стоимость списывается с баланса Stars бота. Сумма округляется вниз.
    """
    if amount <= 0:
        return True
    try:
        catalog = await bot.get_available_gifts()
    except TelegramAPIError as e:
        logging.warning("Не удалось получить каталог подарков: %s", e)
        return False

    available = sorted(
        (g for g in catalog.gifts
         if g.star_count > 0 and (g.remaining_count is None or g.remaining_count > 0)),
        key=lambda g: g.star_count,
        reverse=True,
    )
    plan = []
    left = amount
    for gift in available:
        limit = gift.remaining_count if gift.remaining_count is not None else MAX_GIFTS_PER_PRIZE
        used = 0
        while left >= gift.star_count and used < limit and len(plan) < MAX_GIFTS_PER_PRIZE:
            plan.append(gift)
            left -= gift.star_count
            used += 1
    if not plan:
        logging.warning("Приз %s Stars меньше стоимости любого подарка", amount)
        return False

    for gift in plan:
        try:
            await bot.send_gift(user_id=uid, gift_id=gift.id, text="Ваш приз!")
        except TelegramAPIError as e:
            logging.warning("Не удалось отправить подарок %s: %s", uid, e)
            return False
    return True


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

    title, prize = esc(g["title"]), esc(g["prize"])
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
            sent = await send_stars_prize(bot, winner, g["prize_stars"])
            extra = (f"\n{tg('star')} {g['prize_stars']} Stars отправлены подарками."
                     if sent else "\nStars придётся выдать вручную: автоотправка не удалась.")
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


async def finish_game(bot: Bot, gid: int) -> None:
    game = await get_game(gid)
    if not game or game["status"] != "active":
        return
    entries = await ranked_entries(gid, game["kind"])
    winners = entries[: game["winners"]]
    await mark_game_finished(gid, [w["user_id"] for w in winners])

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

    title = esc(game["title"])
    if not winners:
        await post_to_group(bot, f"{tg('info')} Игра «{title}» завершена: участников не было.")
        return

    lines = []
    for place, w in enumerate(winners, 1):
        user = await get_user(w["user_id"])
        score = f" — выпало {w['value']}" if game["kind"] == "dice" else ""
        lines.append(f"{place}. {user_link(w['user_id'], user)}{score}")

        extra = ""
        if game["prize_stars"]:
            sent = await send_stars_prize(bot, w["user_id"], game["prize_stars"])
            extra = (f"\n{tg('star')} {game['prize_stars']} Stars отправлены подарками."
                     if sent else "\nПриз Stars выдаст администратор вручную.")
        await safe_send(bot, w["user_id"], f"{tg('hooray')} Вы победили в игре «{title}»!{extra}")

    await post_to_group(
        bot,
        f"{tg('crown')} <b>Игра «{title}» завершена!</b>\n\n" + "\n".join(lines),
    )


async def close_week(bot: Bot) -> str:
    """Подводит итоги недели по приглашениям и начинает новую неделю."""
    top = await top_referrers(await week_start(), 10)
    await set_week_start(int(time.time()))

    report = leaderboard_text(top, "Итоги недели")
    if top and top[0]["cnt"] > 0:
        winner = top[0]
        sent = await send_stars_prize(bot, winner["user_id"], WEEKLY_PRIZE_STARS)
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
                       f"{WEEKLY_PRIZE_STARS} {tg('star')}. Автоотправка не удалась, "
                       "проверьте баланс Stars бота и выдайте приз вручную.")
            await safe_send(
                bot, winner["user_id"],
                f"{tg('hooray')} Вы победитель недели по приглашениям!\n"
                f"Приз: {WEEKLY_PRIZE_STARS} {tg('star')} — администратор свяжется с вами.",
            )
    for admin_id in ADMIN_IDS:
        await safe_send(bot, admin_id, report)
    return report


async def join_giveaway(message: Message, bot: Bot, gid: int) -> None:
    uid = message.from_user.id
    g = await get_giveaway(gid)
    if not g or g["status"] != "active" or g["ends_at"] <= time.time():
        await message.answer(f"{tg('no')} Этот розыгрыш уже завершён или не существует.")
        return

    if await is_participant(gid, uid):
        await message.answer(f"{tg('ok')} Вы уже участвуете в розыгрыше «{esc(g['title'])}».")
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
        f"{tg('gift')} <b>{esc(g['title'])}</b>\n"
        f"Приз: {esc(g['prize'])}{stars_line(g['prize_stars'])}\n"
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

    if user.id in ADMIN_IDS:
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
        f"{tg('people')} Приглашено на этой неделе: {count}"
    )


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_group_join(event: ChatMemberUpdated):
    """Засчитывает приглашение, если человек вступил по ссылке конкретного пользователя."""
    if event.chat.id != GROUP_CHAT_ID or event.invite_link is None:
        return
    referrer = await referrer_by_link(event.invite_link.invite_link)
    if referrer is None:
        return
    await add_referral(referrer, event.new_chat_member.user.id)


@router.message(Command("top"))
async def cmd_top(message: Message):
    top = await top_referrers(await week_start(), 10)
    await message.answer(leaderboard_text(top))


# ---------- игры в группе (участники) ----------

@router.message(F.dice)
async def on_dice(message: Message):
    """Бросок 🎲 в группе во время активной игры «Дуэль на кубиках»."""
    if message.chat.id != GROUP_CHAT_ID or message.dice.emoji != "🎲":
        return
    game = await active_game("dice")
    if not game:
        return
    if await enter_game(game["id"], message.from_user.id, message.dice.value):
        await message.reply(f"{tg('ok')} Записано: {message.dice.value}")
    else:
        await message.reply(f"{tg('info')} Вы уже бросали кубик в этой игре.")


@router.callback_query(F.data.startswith("gbtn:"))
async def on_game_button(cb: CallbackQuery, bot: Bot):
    """Нажатие кнопки в игре «Первый нажмёт»."""
    gid = int(cb.data.split(":")[1])
    game = await get_game(gid)
    if not game or game["status"] != "active" or game["ends_at"] <= time.time():
        await cb.answer("Игра уже завершена", show_alert=True)
        return
    if not await enter_game(gid, cb.from_user.id, 0):
        await cb.answer("Вы уже участвуете", show_alert=True)
        return
    await cb.answer("Вы в игре!")
    if await game_entries_count(gid) >= game["winners"]:
        await finish_game(bot, gid)


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
    await cb.message.edit_text(
        f"{tg('gift')} <b>#{gid} {esc(g['title'])}</b>\n"
        f"Приз: {esc(g['prize'])}{stars_line(g['prize_stars'])}\n"
        f"Статус: {'активен' if g['status'] == 'active' else 'завершён'}\n"
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
    try:
        balance = (await bot.get_my_star_balance()).amount
        text = f"{tg('star')} <b>Баланс бота:</b> {balance} Stars"
    except TelegramAPIError as e:
        logging.warning("Не удалось получить баланс: %s", e)
        text = f"{tg('no')} Не удалось получить баланс Stars."
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


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    # Одобряем только наш счёт и только от админа
    ok = query.invoice_payload == TOPUP_PAYLOAD and query.from_user.id in ADMIN_IDS
    await query.answer(ok=ok, error_message=None if ok else "Этот счёт недоступен.")


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot):
    payment = message.successful_payment
    if payment.invoice_payload != TOPUP_PAYLOAD:
        return
    try:
        balance = (await bot.get_my_star_balance()).amount
        balance_text = f" Текущий баланс: {balance} Stars."
    except TelegramAPIError:
        balance_text = ""
    await message.answer(
        f"{tg('ok')} Баланс пополнен на {payment.total_amount} {tg('star')}.{balance_text}"
    )


# ---------- админ: создание розыгрыша ----------

@router.callback_query(F.data == "adm:new", IsAdmin())
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewGiveaway.title)
    await cb.message.answer(f"{tg('gift')} <b>Новый розыгрыш</b>\n\n"
                            "Шаг 1/5: напишите название розыгрыша.")
    await cb.answer()


@router.message(StateFilter(NewGiveaway.title), IsAdmin(), F.text)
async def new_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip()[:100])
    await state.set_state(NewGiveaway.prize)
    await message.answer("Шаг 2/5: что за приз? Напишите текстом.")


@router.message(StateFilter(NewGiveaway.prize), IsAdmin(), F.text)
async def new_prize(message: Message, state: FSMContext):
    await state.update_data(prize=message.text.strip()[:200])
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
    gid = await create_giveaway(
        data["title"], data["prize"], data["stars"],
        str(GROUP_CHAT_ID) if must_be_member else None,
        int(data["ends_at"]), message.from_user.id,
    )
    await state.clear()

    link = f"{await bot_base_url(bot)}?start=g_{gid}"
    note = (f"\n\n{tg('info')} Участвовать могут только участники группы. "
            "Бот должен быть в группе и иметь права админа."
            if must_be_member else "")
    await message.answer(
        f"{tg('ok')} <b>Розыгрыш #{gid} создан</b>\n\n"
        f"{tg('gift')} {esc(data['title'])}\nПриз: {esc(data['prize'])}"
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
        f"{tg('gift')} <b>Розыгрыш «{esc(g['title'])}»</b>\n"
        f"Приз: {esc(g['prize'])}{stars_line(g['prize_stars'])}\n"
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
    await state.update_data(title=message.text.strip()[:100])
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
async def game_minutes(message: Message, state: FSMContext, bot: Bot):
    minutes = parse_float(message.text, 0.5, 7 * 24 * 60)
    if minutes is None:
        await message.answer("Введите число минут от 0.5 до 10080.")
        return

    data = await state.get_data()
    await state.clear()
    gid = await create_game(
        data["kind"], data["title"], data["stars"], data["winners"],
        int(time.time() + minutes * 60), message.from_user.id,
    )

    title = esc(data["title"])
    if data["kind"] == "dice":
        text = (
            f"{tg('gift')} <b>Игра «{title}»</b>\n\n"
            f"Отправьте 🎲 прямо в эту группу в течение {minutes:g} мин. "
            f"Побеждают самые большие значения. Один бросок на человека.\n"
            f"Победителей: {data['winners']}{stars_line(data['stars'])}"
        )
        markup = None
    else:
        text = (
            f"{tg('bell')} <b>Игра «{title}»</b>\n\n"
            f"Нажмите кнопку первыми. Первые {data['winners']} получат приз.\n"
            f"Время игры: {minutes:g} мин.{stars_line(data['stars'])}"
        )
        markup = kb([btn("Забрать приз", "gift", f"gbtn:{gid}")])

    sent = await post_to_group(bot, text, reply_markup=markup)
    if sent:
        await set_game_message(gid, sent.message_id)
        await message.answer(f"{tg('ok')} Игра #{gid} опубликована в группе.",
                             reply_markup=back_to_menu())
    else:
        await message.answer(f"{tg('no')} Игру не удалось опубликовать. "
                             "Проверьте, что бот есть в группе и может писать в неё.",
                             reply_markup=back_to_menu())


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
        except Exception:
            logging.exception("Ошибка в фоновом цикле")
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

    background = asyncio.create_task(background_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        background.cancel()


if __name__ == "__main__":
    asyncio.run(main())
