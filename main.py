import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta

from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove,
    InputMediaPhoto, FSInputFile,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_ADMIN_IDS = [8413475805, 1754507338]


def _load_admin_ids():
    env = os.getenv("ADMIN_IDS", "").strip()
    if not env:
        return DEFAULT_ADMIN_IDS
    ids = [int(x.strip()) for x in env.split(",") if x.strip().isdigit()]
    return ids or DEFAULT_ADMIN_IDS


ADMIN_IDS = _load_admin_ids()


def is_admin(uid):
    return uid in ADMIN_IDS


scheduler = AsyncIOScheduler(timezone=TIMEZONE)

DB_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "bot.db")
print(f"[BOOT] BASE_DIR: {BASE_DIR}")
print(f"[BOOT] DB_PATH:  {DB_PATH}")

if not os.path.exists(DB_PATH):
    print(f"[BOOT] Базы нет — создаю: {DB_PATH}")
    open(DB_PATH, "a").close()
else:
    print(f"[BOOT] База найдена: {DB_PATH}")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
router = Router()

WELCOME_TEXT = (
    "Здравствуйте! 💅\n\n"
    "Добро пожаловать в бот записи на маникюр.\n"
    "Здесь вы можете посмотреть свободные окошки и записаться."
)

RULES_TEXT = (
    "Правила:\n"
    "1. 12+ с запиской от родителей\n"
    "2. Обращаться на ты\n"
    "3. Не принимаю с сопровождением\n"
    "4. Опоздание больше чем 20 минут — штраф 200р."
)

RU_MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}
RU_MONTHS_NUM = {v: k.capitalize() for k, v in RU_MONTHS.items()}


def init_db():
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS months (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "name TEXT, year INTEGER, month_num INTEGER, UNIQUE(name, year))")
    conn.execute("CREATE TABLE IF NOT EXISTS slots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "month_id INTEGER, day INTEGER, time TEXT, is_booked INTEGER DEFAULT 0, "
                 "UNIQUE(month_id, day, time))")
    conn.execute("CREATE TABLE IF NOT EXISTS bookings ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, "
                 "name TEXT, phone TEXT, hand_photo_file_id TEXT, length_shape TEXT, "
                 "reference TEXT, promo_code TEXT, discount INTEGER DEFAULT 0, status TEXT, "
                 "slot_id INTEGER, month_id INTEGER, day INTEGER, time TEXT, "
                 "booking_datetime TEXT, payment_screenshot_file_id TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS promos (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "code TEXT UNIQUE, discount INTEGER, type TEXT, max_uses INTEGER, used_count INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS pending_reschedules ("
                 "booking_id INTEGER PRIMARY KEY, new_slot_id INTEGER, new_month_id INTEGER, "
                 "new_day INTEGER, new_time TEXT, new_datetime TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS extra_requests ("
                 "user_id INTEGER PRIMARY KEY, username TEXT, "
                 "day INTEGER, month INTEGER, year INTEGER, hh INTEGER, mm INTEGER, "
                 "slot_id INTEGER, month_id INTEGER, "
                 "status TEXT, reason TEXT, created_at TEXT)")
    conn.commit()
    m = db_one("SELECT COUNT(*) AS c FROM months")
    print(f"[BOOT] Месяцев в базе: {m['c'] if m else 0}")


def db_exec(q, p=()):
    cur = conn.execute(q, p); conn.commit(); return cur

def db_one(q, p=()):
    return conn.execute(q, p).fetchone()

def db_all(q, p=()):
    return conn.execute(q, p).fetchall()

def get_payment_details():
    env = os.getenv("ADMIN_PAYMENT_DETAILS")
    if env:
        return env
    row = db_one("SELECT value FROM settings WHERE key='payment_details'")
    return row["value"] if row else "Реквизиты не заданы."

def get_months():
    return db_all("SELECT * FROM months ORDER BY year, month_num")

def get_slots(mid):
    return db_all("SELECT * FROM slots WHERE month_id=? ORDER BY day, time", (mid,))


def get_welcome_text():
    row = db_one("SELECT value FROM settings WHERE key='welcome_text'")
    return row["value"] if row else WELCOME_TEXT


def get_welcome_photos():
    """Возвращает список file_id от админа, либо None, если фото не заданы.
    Пустой список трактуется как None, чтобы бот использовал локальные photo1-3."""
    row = db_one("SELECT value FROM settings WHERE key='welcome_photos'")
    if row is None:
        return None
    try:
        val = json.loads(row["value"])
    except Exception:
        return None
    if not isinstance(val, list) or len(val) == 0:
        return None
    return val


async def resolve_username(bot, uid, cached=None):
    if cached:
        return cached.lstrip("@")
    try:
        chat = await bot.get_chat(uid)
        if chat.username:
            return chat.username
    except Exception:
        pass
    return None


async def display_name(bot, uid, cached=None):
    un = await resolve_username(bot, uid, cached)
    return f"@{un}" if un else f"клиент (id {uid})"


async def send_to_admins(bot, text=None, photo=None, reply_markup=None,
                         media=None, caption=None):
    for aid in ADMIN_IDS:
        try:
            if media:
                await bot.send_media_group(aid, media=media)
                if caption:
                    await bot.send_message(aid, caption, reply_markup=reply_markup)
            elif photo:
                await bot.send_photo(aid, photo, caption=caption or "", reply_markup=reply_markup)
            elif text:
                await bot.send_message(aid, text, reply_markup=reply_markup)
        except Exception as e:
            print(f"send_to_admins error for {aid}: {e}")


class IsAdmin(BaseFilter):
    async def __call__(self, event):
        return is_admin(getattr(event.from_user, "id", None))


def parse_month(text):
    text = text.strip().lower()
    year = datetime.now().year
    parts = text.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        year = int(parts[-1]); parts = parts[:-1]
    month_name = " ".join(parts)
    month_num = None
    for name, num in RU_MONTHS.items():
        if name in month_name:
            month_num = num; break
    if month_num is None and month_name.isdigit():
        month_num = int(month_name)
    if month_num is None:
        return None
    return RU_MONTHS_NUM.get(month_num, month_name.capitalize()), year, month_num


def parse_schedule(text):
    groups = [g.strip() for g in text.split(",") if g.strip()]
    result = {}
    for group in groups:
        parts = group.split()
        if not parts:
            continue
        day = int(parts[0]); rest = parts[1:]; times = []
        if any(":" in p for p in rest):
            for p in rest:
                if ":" in p:
                    h, m = p.split(":")
                    times.append(f"{int(h):02d}:{int(m):02d}")
        else:
            for i in range(0, len(rest), 2):
                if i + 1 < len(rest):
                    times.append(f"{int(rest[i]):02d}:{int(rest[i+1]):02d}")
        result[day] = times
    return result


def format_schedule_preview(name, year, month_num, schedule):
    lines = [f"📅 {name} {year}"]
    for day in sorted(schedule.keys()):
        lines.append(f"{day:02d}.{month_num:02d}")
        lines.append(", ".join(schedule[day]))
    return "\n".join(lines)


def format_month_schedule(mid):
    m = db_one("SELECT * FROM months WHERE id=?", (mid,))
    if not m:
        return ""
    slots = db_all("SELECT * FROM slots WHERE month_id=? ORDER BY day, time", (mid,))
    grouped = {}
    for s in slots:
        t = s["time"] + (" ❌" if s["is_booked"] else "")
        grouped.setdefault(s["day"], []).append(t)
    lines = [f"📅 {m['name']} {m['year']}"]
    for day in sorted(grouped.keys()):
        lines.append(f"{day:02d}.{m['month_num']:02d}")
        lines.append(", ".join(grouped[day]))
    return "\n".join(lines)


def format_all_schedules():
    months = get_months()
    if not months:
        return "Записей пока нет."
    return "\n\n".join(format_month_schedule(m["id"]) for m in months)


def admin_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Создать запись"), KeyboardButton(text="Записи")],
        [KeyboardButton(text="Промокод"), KeyboardButton(text="Реквизиты")],
        [KeyboardButton(text="Правила"), KeyboardButton(text="Главное сообщение")],
    ], resize_keyboard=True)


def user_main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Записаться")],
        [KeyboardButton(text="Доп окошко")],
        [KeyboardButton(text="Правила"), KeyboardButton(text="Мои записи")],
    ], resize_keyboard=True)


def yes_reset_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Да"), KeyboardButton(text="Сбросить")],
    ], resize_keyboard=True)


def admin_month_menu_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Добавить запись"), KeyboardButton(text="Отменить запись")],
        [KeyboardButton(text="Забронировать"), KeyboardButton(text="Убрать бронь")],
        [KeyboardButton(text="🗑 Удалить месяц")],
        [KeyboardButton(text="Назад")],
    ], resize_keyboard=True)


def _find_local_welcome_files():
    """Ищет photo1/2/3 рядом с main.py и в подпапках."""
    search_dirs = [
        BASE_DIR,
        os.path.join(BASE_DIR, "app"),
        os.path.join(BASE_DIR, "data"),
        os.path.join(BASE_DIR, "files"),
        os.path.join(BASE_DIR, "media"),
        os.getcwd(),
    ]
    # Убираем дубли путей
    seen = set()
    dirs = []
    for d in search_dirs:
        if d not in seen:
            seen.add(d)
            dirs.append(d)

    paths = []
    for base in ("photo1", "photo2", "photo3"):
        for d in dirs:
            found = False
            for ext in (".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"):
                path = os.path.join(d, base + ext)
                if os.path.exists(path):
                    paths.append(path)
                    print(f"[WELCOME] Нашёл фото: {path}")
                    found = True
                    break
            if found:
                break
    if not paths:
        print(f"[WELCOME] Локальных photo1/2/3 не найдено. Проверял в: {dirs}")
    return paths


async def send_welcome(message: Message):
    text = get_welcome_text()
    file_ids = get_welcome_photos()
    print(f"[WELCOME] file_ids из базы: {file_ids}")

    sources = []
    if file_ids is None:
        for path in _find_local_welcome_files():
            sources.append(FSInputFile(path))
    else:
        for fid in file_ids:
            sources.append(fid)

    print(f"[WELCOME] Источников: {len(sources)}")

    if not sources:
        await message.answer(text)
        return

    if len(sources) == 1:
        try:
            await message.answer_photo(photo=sources[0], caption=text)
            print("[WELCOME] Одно фото отправлено")
            return
        except Exception as e:
            print(f"[WELCOME] Ошибка отправки одного фото: {e}")
        await message.answer(text)
        return

    try:
        media = [InputMediaPhoto(media=s) for s in sources]
        media[0].caption = text
        await message.answer_media_group(media=media)
        return
    except Exception as e:
        print(f"[WELCOME] Ошибка media_group: {e}")

    first = True
    for s in sources:
        try:
            if first:
                await message.answer_photo(photo=s, caption=text)
                first = False
            else:
                await message.answer_photo(photo=s)
        except Exception as e:
            print(f"[WELCOME] Ошибка fallback: {e}")

    if first:
        await message.answer(text)


async def send_user_schedules(message):
    months = get_months()
    if not months:
        await message.answer("Записей пока нет.")
        return
    for m in months:
        await message.answer(format_month_schedule(m["id"]))


async def send_reminder(bot, uid, text):
    try:
        await bot.send_message(uid, text)
    except Exception as e:
        print(f"Reminder error: {e}")


async def daily_report(bot):
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db_all("SELECT * FROM bookings WHERE status='confirmed' AND date(booking_datetime)=?", (today,))
    if not rows:
        await send_to_admins(bot, text=f"📋 На сегодня ({today}) записей нет.")
        return
    text = f"📋 Записи на {today}:\n"
    for r in rows:
        dn = await display_name(bot, r["user_id"], r["username"])
        text += f"{r['time']} — {dn}, тел: {r['phone']}\n"
    await send_to_admins(bot, text=text)


async def schedule_existing_reminders(bot):
    rows = db_all("SELECT * FROM bookings WHERE status='confirmed'")
    now = datetime.now()
    for r in rows:
        dt = datetime.fromisoformat(r["booking_datetime"])
        if dt - timedelta(days=1) > now:
            scheduler.add_job(send_reminder, "date", run_date=dt - timedelta(days=1),
                              args=[bot, r["user_id"],
                                    f"Напоминание: завтра у вас запись на {dt.strftime('%d.%m %H:%M')}"])
        if dt - timedelta(hours=1) > now:
            scheduler.add_job(send_reminder, "date", run_date=dt - timedelta(hours=1),
                              args=[bot, r["user_id"],
                                    f"Напоминание: через час у вас запись на {dt.strftime('%d.%m %H:%M')}"])


async def edit_admin_message(cb, extra):
    try:
        if cb.message.photo:
            await cb.message.edit_caption(caption=(cb.message.caption or "") + f"\n\n{extra}")
        else:
            await cb.message.edit_text((cb.message.text or "") + f"\n\n{extra}")
    except Exception as e:
        print(f"edit_admin_message error: {e}")


async def build_booking_caption(bot, b):
    month = db_one("SELECT * FROM months WHERE id=?", (b["month_id"],))
    date_str = f"{b['day']:02d}.{month['month_num']:02d} {b['time']}"
    dn = await display_name(bot, b["user_id"], b["username"])
    return (
        f"💰 Бронирование\n"
        f"Клиент: {dn}\n"
        f"Имя: {b['name']}\n"
        f"Телефон: {b['phone']}\n"
        f"Дата: {date_str}\n"
        f"Длина и форма: {b['length_shape']}\n"
        f"Промокод: {b['promo_code'] or 'нет'} ({b['discount']}%)"
    )


class AdminStates(StatesGroup):
    choosing_month = State()
    create_month = State()
    create_schedule = State()
    month_menu = State()
    edit_input = State()
    book_day = State()
    book_time = State()
    unbook_day = State()
    unbook_time = State()
    promo_code = State()
    promo_discount = State()
    set_payment = State()
    edit_welcome_text = State()
    edit_welcome_photos = State()
    extra_cancel_reason = State()


class UserStates(StatesGroup):
    booking_month = State()
    booking_day = State()
    booking_time = State()
    booking_hand_photo = State()
    booking_hand_confirm = State()
    booking_length = State()
    booking_reference = State()
    booking_reference_confirm = State()
    booking_phone = State()
    booking_name = State()
    booking_promo = State()
    booking_confirm = State()
    booking_payment_screenshot = State()


class ExtraSlotStates(StatesGroup):
    agree_price = State()
    enter_date = State()
    enter_time = State()
    confirm = State()


# ============= START =============

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await send_welcome(message)
    if is_admin(uid):
        await message.answer(format_all_schedules())
        await message.answer("Админ-меню:", reply_markup=admin_main_kb())
    else:
        await send_user_schedules(message)
        await message.answer("Главное меню:", reply_markup=user_main_kb())


# ============= ПРАВИЛА =============

@router.message(F.text == "Правила")
async def rules_handler(message: Message):
    await message.answer(RULES_TEXT)


# ============= ГЛАВНОЕ СООБЩЕНИЕ =============

@router.message(IsAdmin(), F.text == "Главное сообщение")
async def admin_edit_welcome(message: Message, state: FSMContext):
    await state.set_state(AdminStates.edit_welcome_text)
    await message.answer(
        "Отправьте новый текст главного сообщения (или нажмите «Пропустить»).",
        reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="Пропустить"), KeyboardButton(text="Отмена")]
        ], resize_keyboard=True)
    )


@router.message(AdminStates.edit_welcome_text)
async def admin_welcome_text(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb())
        return
    if message.text != "Пропустить":
        if not message.text:
            await message.answer("Отправьте текст или нажмите «Пропустить».")
            return
        db_exec("INSERT OR REPLACE INTO settings(key, value) VALUES('welcome_text', ?)",
                (message.text,))
    await state.update_data(edit_photos=[])
    await state.set_state(AdminStates.edit_welcome_photos)
    await message.answer(
        "Отправьте фото (по одному). Когда закончите — нажмите «Готово».\n"
        "«Сбросить» — вернуть дефолтные фото.",
        reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="Готово"), KeyboardButton(text="Сбросить")],
            [KeyboardButton(text="Отмена")],
        ], resize_keyboard=True)
    )


@router.message(AdminStates.edit_welcome_photos, F.photo)
async def admin_welcome_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("edit_photos", [])
    photos.append(message.photo[-1].file_id)
    await state.update_data(edit_photos=photos)
    await message.answer(f"Принято фото ({len(photos)}). Ещё или «Готово».")


@router.message(AdminStates.edit_welcome_photos, F.text == "Отмена")
async def admin_welcome_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_main_kb())


@router.message(AdminStates.edit_welcome_photos, F.text == "Сбросить")
async def admin_welcome_reset_photos(message: Message, state: FSMContext):
    db_exec("DELETE FROM settings WHERE key='welcome_photos'")
    await state.clear()
    await message.answer("Фото сброшены на дефолтные (photo1-3.jpg).", reply_markup=admin_main_kb())
    await send_welcome(message)


@router.message(AdminStates.edit_welcome_photos, F.text == "Готово")
async def admin_welcome_done(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("edit_photos", [])
    if not photos:
        await message.answer("Вы не отправили ни одного фото. Нажмите «Сбросить» "
                             "или отправьте фото.")
        return
    db_exec("INSERT OR REPLACE INTO settings(key, value) VALUES('welcome_photos', ?)",
            (json.dumps(photos),))
    await state.clear()
    await message.answer("Сохранено. Превью ниже.", reply_markup=admin_main_kb())
    await send_welcome(message)


@router.message(AdminStates.edit_welcome_photos)
async def admin_welcome_photos_fallback(message: Message):
    await message.answer("Отправьте фото, либо нажмите «Готово» / «Сбросить» / «Отмена».")


# ============= ADMIN =============

@router.message(IsAdmin(), F.text == "Создать запись")
async def admin_create_record(message: Message, state: FSMContext):
    await state.set_state(AdminStates.choosing_month)
    months = get_months()
    buttons = [[KeyboardButton(text=f"{m['name']} {m['year']}")] for m in months]
    buttons.append([KeyboardButton(text="Создать следующий месяц")])
    buttons.append([KeyboardButton(text="Назад")])
    await message.answer("Выберите месяц:",
                         reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


async def open_month_menu(message: Message, state: FSMContext, month_id: int):
    m = db_one("SELECT * FROM months WHERE id=?", (month_id,))
    if not m:
        await message.answer("Месяц не найден.", reply_markup=admin_main_kb())
        await state.clear()
        return
    await state.update_data(month_id=month_id, month_num=m["month_num"],
                            month_name=m["name"], month_year=m["year"])
    await state.set_state(AdminStates.month_menu)
    await message.answer(format_month_schedule(month_id))
    await message.answer(f"Меню: {m['name']} {m['year']}",
                         reply_markup=admin_month_menu_kb())


@router.message(AdminStates.choosing_month)
async def admin_choosing_month(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text == "Назад":
        await state.clear()
        await message.answer("Админ-меню:", reply_markup=admin_main_kb())
        return
    if message.text == "Создать следующий месяц":
        await state.set_state(AdminStates.create_month)
        await message.answer("Введите месяц (например: Сентябрь 2026):",
                             reply_markup=ReplyKeyboardRemove())
        return
    parsed = parse_month(message.text)
    if not parsed:
        await message.answer("Выберите месяц из кнопок.")
        return
    name, year, month_num = parsed
    row = db_one("SELECT * FROM months WHERE name=? AND year=?", (name, year))
    if not row:
        await message.answer("Такого месяца ещё нет. Нажмите «Создать следующий месяц».")
        return
    await open_month_menu(message, state, row["id"])


@router.message(AdminStates.create_month)
async def admin_month_entered(message: Message, state: FSMContext):
    parsed = parse_month(message.text)
    if not parsed:
        await message.answer("Не понял месяц. Введите, например: Сентябрь 2026")
        return
    name, year, month_num = parsed
    row = db_one("SELECT * FROM months WHERE name=? AND year=?", (name, year))
    await state.update_data(month_name=name, month_year=year, month_num=month_num,
                            month_id=row["id"] if row else None)
    await state.set_state(AdminStates.create_schedule)
    await message.answer("Введите дни и время (формат: 1 15 30 16 30, 2 15 30):")


@router.message(AdminStates.create_schedule)
async def admin_schedule_entered(message: Message, state: FSMContext):
    try:
        parsed = parse_schedule(message.text)
    except Exception:
        parsed = None
    if not parsed:
        await message.answer("Не понял. Пример: 1 15 30 16 30 18 30, 2 15 30 16 30 18 30")
        return
    await state.update_data(schedule=parsed)
    data = await state.get_data()
    preview = format_schedule_preview(data["month_name"], data["month_year"],
                                      data["month_num"], parsed)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="admin_schedule_confirm")],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_schedule_edit")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="admin_schedule_cancel")],
    ])
    await message.answer(preview, reply_markup=kb)


@router.callback_query(F.data == "admin_schedule_confirm")
async def admin_schedule_confirm(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    data = await state.get_data()
    name = data["month_name"]; year = data["month_year"]; month_num = data["month_num"]
    schedule = data["schedule"]
    row = db_one("SELECT id FROM months WHERE name=? AND year=?", (name, year))
    if row:
        month_id = row["id"]
        db_exec("DELETE FROM slots WHERE month_id=? AND is_booked=0", (month_id,))
    else:
        cur = db_exec("INSERT INTO months(name, year, month_num) VALUES(?,?,?)",
                      (name, year, month_num))
        month_id = cur.lastrowid
    for day, times in schedule.items():
        for t in times:
            db_exec("INSERT OR IGNORE INTO slots(month_id, day, time) VALUES(?,?,?)",
                    (month_id, day, t))
    await cb.message.edit_text("Месяц создан.")
    await state.clear()
    await cb.message.answer(format_all_schedules())
    await cb.message.answer("Админ-меню:", reply_markup=admin_main_kb())
    await cb.answer()


@router.callback_query(F.data == "admin_schedule_edit")
async def admin_schedule_edit(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.set_state(AdminStates.create_schedule)
    await cb.message.answer("Введите заново дни и время:")
    await cb.answer()


@router.callback_query(F.data == "admin_schedule_cancel")
async def admin_schedule_cancel(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Админ-меню:", reply_markup=admin_main_kb())
    await cb.answer()


# ============ МЕНЮ МЕСЯЦА ============

@router.message(AdminStates.month_menu)
async def admin_month_menu_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    t = message.text
    if t == "Назад":
        await admin_create_record(message, state)
        return
    if t == "🗑 Удалить месяц":
        data = await state.get_data()
        mid = data.get("month_id")
        m = db_one("SELECT * FROM months WHERE id=?", (mid,)) if mid else None
        if not m:
            await message.answer("Месяц не найден.", reply_markup=admin_main_kb())
            await state.clear()
            return
        booking_rows = db_all("SELECT id, user_id FROM bookings WHERE month_id=?", (mid,))
        for b in booking_rows:
            db_exec("DELETE FROM pending_reschedules WHERE booking_id=?", (b["id"],))
            try:
                await message.bot.send_message(
                    b["user_id"],
                    f"❌ Ваша запись на {m['name']} {m['year']} отменена мастером."
                )
            except Exception:
                pass
        db_exec("DELETE FROM bookings WHERE month_id=?", (mid,))
        db_exec("DELETE FROM slots WHERE month_id=?", (mid,))
        db_exec("DELETE FROM months WHERE id=?", (mid,))
        await state.clear()
        await message.answer(f"🗑 Месяц {m['name']} {m['year']} удалён.",
                             reply_markup=admin_main_kb())
        await message.answer(format_all_schedules())
        return
    if t == "Добавить запись":
        await state.update_data(edit_action="add")
        await state.set_state(AdminStates.edit_input)
        await message.answer(
            "Введите день и время. Пример: 1 15 30 (несколько: 1 15 30 16 30):",
            reply_markup=ReplyKeyboardRemove())
        return
    if t == "Отменить запись":
        await state.update_data(edit_action="remove")
        await state.set_state(AdminStates.edit_input)
        await message.answer(
            "Введите день и время для удаления. Пример: 1 15 30:",
            reply_markup=ReplyKeyboardRemove())
        return
    if t == "Забронировать":
        await show_admin_days(message, state, "book")
        return
    if t == "Убрать бронь":
        await show_admin_days(message, state, "unbook")
        return
    await message.answer("Выберите кнопку из меню.", reply_markup=admin_month_menu_kb())


@router.message(AdminStates.edit_input)
async def admin_edit_input(message: Message, state: FSMContext):
    data = await state.get_data()
    mid = data["month_id"]; action = data.get("edit_action")
    try:
        parsed = parse_schedule(message.text)
    except Exception:
        parsed = None
    if not parsed:
        await message.answer("Не понял. Пример: 1 15 30 16 30")
        return
    log = []
    if action == "add":
        for day, times in parsed.items():
            for t in times:
                try:
                    db_exec("INSERT INTO slots(month_id, day, time) VALUES(?,?,?)",
                            (mid, day, t))
                    log.append(f"+ {day:02d}.{data['month_num']:02d} {t}")
                except sqlite3.IntegrityError:
                    log.append(f"= {day:02d}.{data['month_num']:02d} {t} (уже есть)")
    else:
        for day, times in parsed.items():
            for t in times:
                slot = db_one("SELECT * FROM slots WHERE month_id=? AND day=? AND time=?",
                              (mid, day, t))
                if slot:
                    db_exec("DELETE FROM slots WHERE id=?", (slot["id"],))
                    log.append(f"− {day:02d}.{data['month_num']:02d} {t}")
                else:
                    log.append(f"? {day:02d}.{data['month_num']:02d} {t} (нет)")
    await message.answer("Готово:\n" + "\n".join(log))
    await open_month_menu(message, state, mid)


async def show_admin_days(message: Message, state: FSMContext, mode: str):
    data = await state.get_data()
    mid = data["month_id"]
    month = db_one("SELECT * FROM months WHERE id=?", (mid,))
    slots = get_slots(mid)
    if mode == "book":
        slots = [s for s in slots if not s["is_booked"]]
    else:
        slots = [s for s in slots if s["is_booked"]]
    days = sorted(set(s["day"] for s in slots))
    if not days:
        await message.answer("Нет подходящих дней.")
        await open_month_menu(message, state, mid)
        return
    await state.update_data(admin_action=mode)
    buttons = [[KeyboardButton(text=f"{d:02d}.{month['month_num']:02d}")] for d in days]
    buttons.append([KeyboardButton(text="Назад")])
    if mode == "book":
        await state.set_state(AdminStates.book_day)
    else:
        await state.set_state(AdminStates.unbook_day)
    await message.answer("Выберите день:",
                         reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


async def show_admin_times(message: Message, state: FSMContext, mode: str, day: int):
    data = await state.get_data()
    mid = data["month_id"]
    month = db_one("SELECT * FROM months WHERE id=?", (mid,))
    if mode == "book":
        slots = db_all("SELECT * FROM slots WHERE month_id=? AND day=? AND is_booked=0 ORDER BY time",
                       (mid, day))
    else:
        slots = db_all("SELECT * FROM slots WHERE month_id=? AND day=? AND is_booked=1 ORDER BY time",
                       (mid, day))
    if not slots:
        await message.answer("Нет слотов.")
        await show_admin_days(message, state, mode)
        return
    buttons = [[KeyboardButton(text=s["time"])] for s in slots]
    buttons.append([KeyboardButton(text="Назад")])
    if mode == "book":
        await state.set_state(AdminStates.book_time)
    else:
        await state.set_state(AdminStates.unbook_time)
    await message.answer(f"Выберите время на {day:02d}.{month['month_num']:02d}:",
                         reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


@router.message(AdminStates.book_day)
async def admin_book_day(message: Message, state: FSMContext):
    if message.text == "Назад":
        data = await state.get_data()
        await open_month_menu(message, state, data["month_id"])
        return
    try:
        day = int(message.text.split(".")[0])
    except Exception:
        await message.answer("Выберите день из кнопок.")
        return
    await state.update_data(day=day)
    await show_admin_times(message, state, "book", day)


@router.message(AdminStates.book_time)
async def admin_book_time(message: Message, state: FSMContext):
    if message.text == "Назад":
        await show_admin_days(message, state, "book")
        return
    data = await state.get_data()
    mid = data["month_id"]; day = data["day"]; time = message.text
    slot = db_one("SELECT * FROM slots WHERE month_id=? AND day=? AND time=?",
                  (mid, day, time))
    if not slot:
        await message.answer("Выберите время из кнопок.")
        return
    db_exec("UPDATE slots SET is_booked=1 WHERE id=?", (slot["id"],))
    await message.answer(f"✅ Забронировано: {day:02d}.{data['month_num']:02d} {time}")
    await open_month_menu(message, state, mid)


@router.message(AdminStates.unbook_day)
async def admin_unbook_day(message: Message, state: FSMContext):
    if message.text == "Назад":
        data = await state.get_data()
        await open_month_menu(message, state, data["month_id"])
        return
    try:
        day = int(message.text.split(".")[0])
    except Exception:
        await message.answer("Выберите день из кнопок.")
        return
    await state.update_data(day=day)
    await show_admin_times(message, state, "unbook", day)


@router.message(AdminStates.unbook_time)
async def admin_unbook_time(message: Message, state: FSMContext):
    if message.text == "Назад":
        await show_admin_days(message, state, "unbook")
        return
    data = await state.get_data()
    mid = data["month_id"]; day = data["day"]; time = message.text
    slot = db_one("SELECT * FROM slots WHERE month_id=? AND day=? AND time=?",
                  (mid, day, time))
    if not slot:
        await message.answer("Выберите время из кнопок.")
        return
    db_exec("UPDATE slots SET is_booked=0 WHERE id=?", (slot["id"],))
    await message.answer(f"✅ Бронь снята: {day:02d}.{data['month_num']:02d} {time}")
    await open_month_menu(message, state, mid)


# ============ ЗАПИСИ (КЛИЕНТЫ) ============

@router.message(IsAdmin(), F.text == "Записи")
async def admin_records(message: Message, state: FSMContext):
    await state.clear()
    rows = db_all("SELECT DISTINCT user_id, username FROM bookings WHERE status='confirmed'")
    if not rows:
        await message.answer("Пока нет подтверждённых клиентов.", reply_markup=admin_main_kb())
        return
    buttons = []
    for r in rows:
        un = await resolve_username(message.bot, r["user_id"], r["username"])
        label = f"@{un}" if un else f"клиент #{r['user_id']}"
        buttons.append([KeyboardButton(text=label)])
    buttons.append([KeyboardButton(text="Назад")])
    await message.answer("Клиенты:",
                         reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


async def show_client_cards(message: Message, rows):
    if not rows:
        await message.answer("Записей нет.")
        return
    for r in rows:
        month = db_one("SELECT * FROM months WHERE id=?", (r["month_id"],))
        date_str = f"{r['day']:02d}.{month['month_num']:02d} {r['time']}"
        dn = await display_name(message.bot, r["user_id"], r["username"])
        caption = (f"👤 {dn}\n"
                   f"Имя: {r['name']}\n"
                   f"📞 {r['phone']}\n"
                   f"📅 {date_str}\n"
                   f"💅 Длина и форма: {r['length_shape']}")
        await message.answer_photo(r["hand_photo_file_id"], caption=caption)
        if r["reference"]:
            await message.answer_photo(r["reference"], caption="Референс")
    await message.answer("Админ-меню:", reply_markup=admin_main_kb())


@router.message(IsAdmin(), F.text.regexp(r"^@[A-Za-z0-9_]+$"))
async def admin_client_by_username(message: Message, state: FSMContext):
    un = message.text[1:].strip()
    rows = db_all("SELECT * FROM bookings WHERE username=? AND status='confirmed' "
                  "ORDER BY booking_datetime", (un,))
    if not rows:
        candidates = db_all("SELECT DISTINCT user_id FROM bookings WHERE status='confirmed'")
        for c in candidates:
            try:
                chat = await message.bot.get_chat(c["user_id"])
                if chat.username and chat.username.lower() == un.lower():
                    rows = db_all("SELECT * FROM bookings WHERE user_id=? AND status='confirmed' "
                                  "ORDER BY booking_datetime", (c["user_id"],))
                    break
            except Exception:
                pass
    await show_client_cards(message, rows)


@router.message(IsAdmin(), F.text.regexp(r"^клиент #\d+$"))
async def admin_client_by_id(message: Message, state: FSMContext):
    uid = int(re.search(r"\d+", message.text).group())
    rows = db_all("SELECT * FROM bookings WHERE user_id=? AND status='confirmed' "
                  "ORDER BY booking_datetime", (uid,))
    await show_client_cards(message, rows)


@router.message(IsAdmin(), F.text == "Назад")
async def admin_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Админ-меню:", reply_markup=admin_main_kb())


# ============= ПРОМОКОДЫ =============

async def show_promos_menu(target, state: FSMContext, edit: bool = False):
    promos = db_all("SELECT * FROM promos ORDER BY id DESC")
    print(f"[PROMO] Промокодов в базе: {len(promos)}")

    if not promos:
        text = "🎟 Промокодов пока нет."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать промокод", callback_data="promo_new")],
        ])
    else:
        lines = ["🎟 <b>Промокоды:</b>\n"]
        for p in promos:
            t = "персональный" if p["type"] == "personal" else "обычный"
            if p["type"] == "personal":
                uses_str = f"{p['used_count']}/{p['max_uses']}"
            else:
                uses_str = f"использован {p['used_count']} раз"
            lines.append(f"• <b>{p['code']}</b> — {p['discount']}% ({t}, {uses_str})")
        text = "\n".join(lines)
        buttons = []
        for p in promos:
            buttons.append([InlineKeyboardButton(
                text=f"🗑 Удалить {p['code']}",
                callback_data=f"promo_del:{p['id']}"
            )])
        buttons.append([InlineKeyboardButton(text="➕ Создать промокод", callback_data="promo_new")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if edit and hasattr(target, "edit_text"):
        try:
            await target.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=kb)


@router.message(IsAdmin(), F.text == "Промокод")
async def admin_promo(message: Message, state: FSMContext):
    await state.clear()
    print("[PROMO] Открыто меню промокодов")
    await show_promos_menu(message, state)


@router.callback_query(F.data == "promo_new")
async def promo_new(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обычный", callback_data="promo_type:ordinary")],
        [InlineKeyboardButton(text="Персональный", callback_data="promo_type:personal")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="promo_back")],
    ])
    await cb.message.answer("Выберите тип промокода:", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "promo_back")
async def promo_back(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.clear()
    await show_promos_menu(cb.message, state)
    await cb.answer()


@router.callback_query(F.data.startswith("promo_type:"))
async def promo_type_selected(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    ptype = cb.data.split(":")[1]
    await state.clear()
    await state.update_data(promo_type=ptype)
    await state.set_state(AdminStates.promo_code)
    print(f"[PROMO] Выбран тип: {ptype}, жду код промокода")
    await cb.message.answer("Введите код промокода:")
    await cb.answer()


@router.message(AdminStates.promo_code)
async def promo_code_entered(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    if not code:
        await message.answer("Введите код текстом:")
        return
    existing = db_one("SELECT id FROM promos WHERE code=?", (code,))
    if existing:
        await message.answer("Такой промокод уже есть. Введи другой код:")
        return
    await state.update_data(promo_code=code)
    await state.set_state(AdminStates.promo_discount)
    print(f"[PROMO] Код: {code}, жду скидку в %")
    await message.answer("Введите скидку в % (число от 1 до 100):")


@router.message(AdminStates.promo_discount)
async def promo_discount_entered(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Введите число (например 10):")
        return
    discount = int(text)
    if not (1 <= discount <= 100):
        await message.answer("Скидка должна быть от 1 до 100. Попробуйте снова:")
        return

    data = await state.get_data()
    code = data.get("promo_code")
    ptype = data.get("promo_type", "ordinary")

    if not code:
        await message.answer("Сессия потеряна, начните заново: Админ-меню → Промокод")
        await state.clear()
        await message.answer("Админ-меню:", reply_markup=admin_main_kb())
        return

    max_uses = 1 if ptype == "personal" else 999999
    try:
        db_exec("INSERT INTO promos(code, discount, type, max_uses) VALUES(?,?,?,?)",
                (code, discount, ptype, max_uses))
        print(f"[PROMO] ✅ Создан промокод: {code}, скидка {discount}%, тип {ptype}")
        await message.answer(
            f"✅ Промокод успешно создан!\n\n"
            f"Код: <b>{code}</b>\n"
            f"Скидка: <b>{discount}%</b>\n"
            f"Тип: <b>{'персональный' if ptype == 'personal' else 'обычный'}</b>"
        )
    except sqlite3.IntegrityError as e:
        print(f"[PROMO] IntegrityError: {e}")
        await message.answer("Такой промокод уже есть в базе.")
    except Exception as e:
        print(f"[PROMO] Ошибка создания: {e}")
        await message.answer(f"Ошибка при создании промокода: {e}")

    await state.clear()
    await show_promos_menu(message, state)
    await message.answer("Админ-меню:", reply_markup=admin_main_kb())


@router.callback_query(F.data.startswith("promo_del:"))
async def promo_delete(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    pid = int(cb.data.split(":")[1])
    promo = db_one("SELECT * FROM promos WHERE id=?", (pid,))
    if not promo:
        await cb.answer("Промокод не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"promo_del_ok:{pid}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="promo_back")],
    ])
    try:
        await cb.message.edit_text(
            f"Удалить промокод <b>{promo['code']}</b> ({promo['discount']}%)?",
            reply_markup=kb
        )
    except Exception:
        await cb.message.answer(
            f"Удалить промокод <b>{promo['code']}</b> ({promo['discount']}%)?",
            reply_markup=kb
        )
    await cb.answer()


@router.callback_query(F.data.startswith("promo_del_ok:"))
async def promo_delete_ok(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    pid = int(cb.data.split(":")[1])
    promo = db_one("SELECT * FROM promos WHERE id=?", (pid,))
    if not promo:
        await cb.answer("Промокод не найден", show_alert=True)
        return
    db_exec("DELETE FROM promos WHERE id=?", (pid,))
    print(f"[PROMO] Удалён промокод: {promo['code']}")
    await cb.answer(f"Промокод {promo['code']} удалён")
    await show_promos_menu(cb.message, state, edit=True)


# ============= РЕКВИЗИТЫ =============

@router.message(IsAdmin(), F.text == "Реквизиты")
async def admin_set_payment(message: Message, state: FSMContext):
    await state.set_state(AdminStates.set_payment)
    await message.answer("Введите реквизиты для оплаты предоплаты:")


@router.message(AdminStates.set_payment)
async def set_payment(message: Message, state: FSMContext):
    db_exec("INSERT OR REPLACE INTO settings(key, value) VALUES('payment_details', ?)",
            (message.text,))
    await message.answer("Реквизиты сохранены.")
    await state.clear()
    await message.answer("Админ-меню:", reply_markup=admin_main_kb())


# ============= ДОП ОКОШКО =============

@router.message(F.text == "Доп окошко")
async def extra_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ExtraSlotStates.agree_price)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="extra_agree:yes")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="extra_agree:no")],
    ])
    await message.answer(
        "🕐 Доп окошко — 300 рублей (плюс предоплата 200 руб).\n"
        "Согласна?", reply_markup=kb
    )


@router.callback_query(F.data.startswith("extra_agree:"))
async def extra_agree(cb: CallbackQuery, state: FSMContext):
    ans = cb.data.split(":")[1]
    if ans == "no":
        await state.clear()
        await cb.message.edit_text("Хорошо, отменено.")
        await cb.message.answer("Главное меню:", reply_markup=user_main_kb())
        await cb.answer()
        return
    await state.set_state(ExtraSlotStates.enter_date)
    await cb.message.edit_text("Введи дату в формате ДД.ММ (например 01.09):")
    await cb.answer()


@router.message(ExtraSlotStates.enter_date)
async def extra_enter_date(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    m = re.match(r"^(\d{1,2})[\.\s](\d{1,2})$", text)
    if not m:
        await message.answer("Введи дату в формате ДД.ММ (например 01.09):")
        return
    day, month = int(m.group(1)), int(m.group(2))
    if not (1 <= day <= 31 and 1 <= month <= 12):
        await message.answer("Некорректная дата. Попробуй снова:")
        return
    now = datetime.now()
    year = now.year
    if month < now.month or (month == now.month and day < now.day):
        year = now.year + 1
    await state.update_data(extra_day=day, extra_month=month, extra_year=year)
    await state.set_state(ExtraSlotStates.enter_time)
    await message.answer("Введи время в формате ЧЧ ММ (например 16 00):")


@router.message(ExtraSlotStates.enter_time)
async def extra_enter_time(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    m = re.match(r"^(\d{1,2})[:\s](\d{1,2})$", text)
    if not m:
        await message.answer("Введи время в формате ЧЧ ММ (например 16 00):")
        return
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        await message.answer("Некорректное время. Попробуй снова:")
        return

    data = await state.get_data()
    day = data["extra_day"]; month = data["extra_month"]; year = data["extra_year"]

    name = RU_MONTHS_NUM.get(month, "").capitalize()
    if name:
        m_row = db_one("SELECT id FROM months WHERE name=? AND year=?", (name, year))
        if m_row:
            existing = db_one(
                "SELECT id FROM slots WHERE month_id=? AND day=? LIMIT 1",
                (m_row["id"], day)
            )
            if existing:
                await message.answer(
                    "❌ На эту дату уже есть расписание мастера.\n"
                    "Выбери другой день или запишись через «Записаться»."
                )
                await state.set_state(ExtraSlotStates.enter_date)
                await message.answer("Введи дату в формате ДД.ММ (например 01.09):")
                return

    pending = db_one(
        "SELECT user_id FROM extra_requests "
        "WHERE day=? AND month=? AND year=? AND hh=? AND mm=? AND status='pending'",
        (day, month, year, hh, mm)
    )
    if pending:
        await message.answer(
            "❌ На это время уже есть заявка на доп окошко, ожидающая подтверждения.\n"
            "Выбери другое время."
        )
        await state.set_state(ExtraSlotStates.enter_time)
        await message.answer("Введи время в формате ЧЧ ММ (например 16 00):")
        return

    await state.update_data(extra_hh=hh, extra_mm=mm)
    await state.set_state(ExtraSlotStates.confirm)
    text_out = (
        f"Проверь данные:\n"
        f"📅 {day:02d}.{month:02d}.{year}\n"
        f"🕐 {hh:02d}:{mm:02d}\n"
        f"💰 300 руб"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="extra_date_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="extra_date_cancel")],
    ])
    await message.answer(text_out, reply_markup=kb)


@router.callback_query(F.data == "extra_date_cancel")
async def extra_date_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Главное меню:", reply_markup=user_main_kb())
    await cb.answer()


@router.callback_query(F.data == "extra_date_confirm")
async def extra_date_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    day = data["extra_day"]; month = data["extra_month"]; year = data["extra_year"]
    hh = data["extra_hh"]; mm = data["extra_mm"]
    db_exec(
        "INSERT OR REPLACE INTO extra_requests"
        "(user_id, username, day, month, year, hh, mm, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (cb.from_user.id, cb.from_user.username, day, month, year, hh, mm,
         "pending", datetime.now().isoformat())
    )
    dn = await display_name(cb.bot, cb.from_user.id, cb.from_user.username)
    text = (f"🕐 Запрос на доп окошко\n"
            f"Клиент: {dn}\n"
            f"Дата: {day:02d}.{month:02d}.{year}\n"
            f"Время: {hh:02d}:{mm:02d}\n"
            f"Цена: 300 руб")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить",
                              callback_data=f"admin_extra_ok:{cb.from_user.id}")],
        [InlineKeyboardButton(text="❌ Отменить",
                              callback_data=f"admin_extra_no:{cb.from_user.id}")],
    ])
    await send_to_admins(cb.bot, text=text, reply_markup=kb)
    await cb.message.edit_text(
        "Запрос отправлен мастеру. Ожидай ответа ✨\n"
        "Как только мастер ответит, я пришлю уведомление."
    )
    await state.clear()
    await cb.answer()


@router.callback_query(F.data.startswith("admin_extra_ok:"))
async def admin_extra_ok(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    uid = int(cb.data.split(":")[1])
    req = db_one("SELECT * FROM extra_requests WHERE user_id=?", (uid,))
    if not req or req["status"] != "pending":
        await cb.answer("Уже обработано")
        return
    name = RU_MONTHS_NUM.get(req["month"], "").capitalize() or f"{req['month']:02d}"
    row = db_one("SELECT * FROM months WHERE name=? AND year=?", (name, req["year"]))
    if row:
        month_id = row["id"]
    else:
        cur = db_exec("INSERT INTO months(name, year, month_num) VALUES(?,?,?)",
                      (name, req["year"], req["month"]))
        month_id = cur.lastrowid
    time_str = f"{req['hh']:02d}:{req['mm']:02d}"
    slot = db_one("SELECT * FROM slots WHERE month_id=? AND day=? AND time=?",
                  (month_id, req["day"], time_str))
    if slot:
        db_exec("UPDATE slots SET is_booked=1 WHERE id=?", (slot["id"],))
        slot_id = slot["id"]
    else:
        cur = db_exec("INSERT INTO slots(month_id, day, time, is_booked) VALUES(?,?,?,1)",
                      (month_id, req["day"], time_str))
        slot_id = cur.lastrowid
    db_exec("UPDATE extra_requests SET status='confirmed', slot_id=?, month_id=? "
            "WHERE user_id=?", (slot_id, month_id, uid))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Продолжить оформление",
                              callback_data="extra_continue")],
    ])
    try:
        await cb.bot.send_message(
            uid,
            "✅ Мастер подтвердил доп окошко!\n"
            f"📅 {req['day']:02d}.{req['month']:02d}.{req['year']} в {time_str}\n\n"
            "Нажми кнопку ниже, чтобы продолжить оформление.",
            reply_markup=kb
        )
    except Exception:
        pass
    try:
        await cb.message.edit_text((cb.message.text or "") + "\n\n✅ Подтверждено")
    except Exception:
        pass
    await cb.answer("OK")


@router.callback_query(F.data.startswith("admin_extra_no:"))
async def admin_extra_no(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    uid = int(cb.data.split(":")[1])
    orig = cb.message.text or cb.message.caption or ""
    await state.update_data(extra_cancel_uid=uid,
                            extra_cancel_msg_id=cb.message.message_id,
                            extra_cancel_chat_id=cb.message.chat.id,
                            extra_cancel_orig=orig)
    await state.set_state(AdminStates.extra_cancel_reason)
    await cb.message.answer("Введите причину отмены доп окошка:")
    await cb.answer()


@router.message(AdminStates.extra_cancel_reason)
async def admin_extra_cancel_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("extra_cancel_uid")
    reason = (message.text or "").strip() or "Без указания причины"
    db_exec("UPDATE extra_requests SET status='cancelled', reason=? WHERE user_id=?",
            (reason, uid))
    try:
        await message.bot.send_message(
            uid,
            f"❌ Мастер отклонил доп окошко.\nПричина: {reason}"
        )
    except Exception:
        pass
    orig = data.get("extra_cancel_orig", "")
    try:
        await message.bot.edit_message_text(
            chat_id=data["extra_cancel_chat_id"],
            message_id=data["extra_cancel_msg_id"],
            text=orig + f"\n\n❌ Отменено: {reason}"
        )
    except Exception:
        pass
    await state.clear()
    await message.answer("Причина отправлена клиентке.", reply_markup=admin_main_kb())


@router.callback_query(F.data == "extra_continue")
async def extra_continue(cb: CallbackQuery, state: FSMContext):
    req = db_one("SELECT * FROM extra_requests WHERE user_id=?", (cb.from_user.id,))
    if not req or req["status"] != "confirmed":
        await cb.answer("Заявка не найдена или ещё не подтверждена")
        return
    await state.clear()
    await state.update_data(
        is_extra=True,
        month_id=req["month_id"],
        day=req["day"],
        time=f"{req['hh']:02d}:{req['mm']:02d}",
        slot_id=req["slot_id"],
    )
    await state.set_state(UserStates.booking_hand_photo)
    try:
        await cb.message.edit_text("Продолжаем оформление ✨")
    except Exception:
        pass
    await cb.message.answer("Отправь фото ручек (ногтей):",
                            reply_markup=ReplyKeyboardRemove())
    await cb.answer()


# ============= USER =============

@router.message(F.text == "Мои записи")
async def user_my_records(message: Message, state: FSMContext):
    await state.clear()
    rows = db_all("SELECT * FROM bookings WHERE user_id=? AND status='confirmed' "
                  "ORDER BY booking_datetime", (message.from_user.id,))
    if not rows:
        await message.answer("У тебя пока нет записей.")
        return
    for r in rows:
        dt = datetime.fromisoformat(r["booking_datetime"])
        can_move = (dt - datetime.now()) >= timedelta(days=3)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔁 Перенести",
                                 callback_data=f"resched:{r['id']}")]]) if can_move else None
        await message.answer(f"📅 {dt.strftime('%d.%m %H:%M')}", reply_markup=kb)


@router.callback_query(F.data.startswith("resched:"))
async def user_resched_start(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split(":")[1])
    b = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("Не найдено")
        return
    dt = datetime.fromisoformat(b["booking_datetime"])
    if dt - datetime.now() < timedelta(days=3):
        await cb.answer()
        await cb.message.answer("❌ Перенести можно не позднее чем за 3 дня до записи.")
        return
    await state.update_data(mode="resched", resched_booking_id=bid)
    await cb.answer()
    months = get_months()
    if len(months) == 1:
        m = months[0]
        await state.update_data(month_id=m["id"], month_num=m["month_num"],
                                month_name=m["name"], month_year=m["year"])
        await show_user_days(cb.message, state)
    else:
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=f"{m['name']} {m['year']}")] for m in months]
                     + [[KeyboardButton(text="Назад")]], resize_keyboard=True)
        await state.set_state(UserStates.booking_month)
        await cb.message.answer("Выбери месяц:", reply_markup=kb)


@router.message(F.text == "Записаться")
async def user_book(message: Message, state: FSMContext):
    await state.clear()
    await state.update_data(mode="book")
    months = get_months()
    if not months:
        await message.answer("Пока нет доступных записей.")
        return
    if len(months) == 1:
        m = months[0]
        await state.update_data(month_id=m["id"], month_num=m["month_num"],
                                month_name=m["name"], month_year=m["year"])
        await show_user_days(message, state)
    else:
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=f"{m['name']} {m['year']}")] for m in months]
                     + [[KeyboardButton(text="Назад")]], resize_keyboard=True)
        await state.set_state(UserStates.booking_month)
        await message.answer("Выбери месяц:", reply_markup=kb)


async def show_user_days(message: Message, state: FSMContext):
    data = await state.get_data()
    mid = data["month_id"]
    month = db_one("SELECT * FROM months WHERE id=?", (mid,))
    slots = get_slots(mid)
    days = sorted(set(s["day"] for s in slots))
    if not days:
        await message.answer("Нет доступных дней.")
        return
    buttons = [[KeyboardButton(text=f"{d:02d}.{month['month_num']:02d}")] for d in days]
    buttons.append([KeyboardButton(text="Назад")])
    await state.set_state(UserStates.booking_day)
    await message.answer("Выбери день:",
                         reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


async def show_user_times(message: Message, state: FSMContext):
    data = await state.get_data()
    mid = data["month_id"]; day = data["day"]
    month = db_one("SELECT * FROM months WHERE id=?", (mid,))
    slots = db_all("SELECT * FROM slots WHERE month_id=? AND day=? AND is_booked=0 "
                   "ORDER BY time", (mid, day))
    if not slots:
        await message.answer("На этот день нет свободного времени. Выбери другой день.")
        await show_user_days(message, state)
        return
    buttons = [[KeyboardButton(text=s["time"])] for s in slots]
    buttons.append([KeyboardButton(text="Назад")])
    await state.set_state(UserStates.booking_time)
    await message.answer(f"Выбери время на {day:02d}.{month['month_num']:02d}:",
                         reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


@router.message(UserStates.booking_month)
async def user_month_selected(message: Message, state: FSMContext):
    if message.text == "Назад":
        await state.clear()
        await message.answer("Главное меню:", reply_markup=user_main_kb())
        return
    parts = message.text.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        year = int(parts[-1]); name = " ".join(parts[:-1])
    else:
        year = datetime.now().year; name = message.text
    row = db_one("SELECT * FROM months WHERE name=? AND year=?", (name, year))
    if not row:
        await message.answer("Выбери месяц из кнопок.")
        return
    await state.update_data(month_id=row["id"], month_num=row["month_num"],
                            month_name=row["name"], month_year=row["year"])
    await show_user_days(message, state)


@router.message(UserStates.booking_day)
async def user_day_selected(message: Message, state: FSMContext):
    if message.text == "Назад":
        data = await state.get_data()
        if data.get("mode") == "resched":
            await state.clear()
            await message.answer("Главное меню:", reply_markup=user_main_kb())
        else:
            await user_book(message, state)
        return
    try:
        day = int(message.text.split(".")[0])
    except Exception:
        await message.answer("Выбери день из кнопок.")
        return
    await state.update_data(day=day)
    await show_user_times(message, state)


@router.message(UserStates.booking_time)
async def user_time_selected(message: Message, state: FSMContext):
    if message.text == "Назад":
        await show_user_days(message, state)
        return
    data = await state.get_data()
    mid = data["month_id"]; day = data["day"]; time = message.text
    slot = db_one("SELECT * FROM slots WHERE month_id=? AND day=? AND time=?",
                  (mid, day, time))
    if not slot:
        await message.answer("Выбери время из кнопок.")
        return
    if slot["is_booked"]:
        await message.answer("Это время уже занято. Выбери другое.")
        await show_user_times(message, state)
        return
    await state.update_data(slot_id=slot["id"], time=time)
    if data.get("mode") == "resched":
        await user_resched_confirm(message, state)
        return
    await state.set_state(UserStates.booking_hand_photo)
    await message.answer("Отправь фото ручек (ногтей):",
                         reply_markup=ReplyKeyboardRemove())


async def user_resched_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    month = db_one("SELECT * FROM months WHERE id=?", (data["month_id"],))
    old = db_one("SELECT * FROM bookings WHERE id=?", (data["resched_booking_id"],))
    new_str = f"{data['day']:02d}.{month['month_num']:02d} {data['time']}"
    old_dt = datetime.fromisoformat(old["booking_datetime"])
    text = f"Перенести запись с {old_dt.strftime('%d.%m %H:%M')} на {new_str}?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="resched_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="resched_cancel")],
    ])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "resched_confirm")
async def resched_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    bid = data.get("resched_booking_id")
    if not bid:
        await cb.answer("Сессия устарела")
        return
    old = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    month = db_one("SELECT * FROM months WHERE id=?", (data["month_id"],))
    new_dt = datetime(month["year"], month["month_num"], data["day"],
                      *map(int, data["time"].split(":")))
    db_exec("INSERT OR REPLACE INTO pending_reschedules"
            "(booking_id, new_slot_id, new_month_id, new_day, new_time, new_datetime) "
            "VALUES(?,?,?,?,?,?)",
            (bid, data["slot_id"], data["month_id"], data["day"], data["time"], new_dt.isoformat()))
    dn = await display_name(cb.bot, cb.from_user.id, cb.from_user.username)
    old_dt = datetime.fromisoformat(old["booking_datetime"])
    text = (f"🔁 Запрос на перенос\n"
            f"Клиент: {dn}\n"
            f"Было: {old_dt.strftime('%d.%m %H:%M')}\n"
            f"Станет: {new_dt.strftime('%d.%m %H:%M')}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить",
                              callback_data=f"admin_resched_ok:{bid}")],
        [InlineKeyboardButton(text="❌ Отмена",
                              callback_data=f"admin_resched_no:{bid}")],
    ])
    await send_to_admins(cb.bot, text=text, reply_markup=kb)
    await cb.message.edit_text("Запрос отправлен мастеру. Ожидай подтверждения.")
    await state.clear()
    await cb.answer()


@router.callback_query(F.data == "resched_cancel")
async def resched_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Главное меню:", reply_markup=user_main_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("admin_resched_ok:"))
async def admin_resched_ok(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    bid = int(cb.data.split(":")[1])
    pr = db_one("SELECT * FROM pending_reschedules WHERE booking_id=?", (bid,))
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if not pr or not booking:
        await cb.answer("Не найдено")
        return
    db_exec("UPDATE slots SET is_booked=0 WHERE id=?", (booking["slot_id"],))
    db_exec("UPDATE slots SET is_booked=1 WHERE id=?", (pr["new_slot_id"],))
    db_exec("UPDATE bookings SET slot_id=?, month_id=?, day=?, time=?, booking_datetime=? "
            "WHERE id=?",
            (pr["new_slot_id"], pr["new_month_id"], pr["new_day"], pr["new_time"],
             pr["new_datetime"], bid))
    db_exec("DELETE FROM pending_reschedules WHERE booking_id=?", (bid,))
    new_dt = datetime.fromisoformat(pr["new_datetime"])
    try:
        await cb.bot.send_message(booking["user_id"],
                                  f"✅ Перенос подтверждён: {new_dt.strftime('%d.%m %H:%M')}")
    except Exception:
        pass
    try:
        await cb.message.edit_text((cb.message.text or "") + "\n\n✅ Перенос подтверждён")
    except Exception:
        pass
    await cb.answer("ОК")


@router.callback_query(F.data.startswith("admin_resched_no:"))
async def admin_resched_no(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    bid = int(cb.data.split(":")[1])
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    db_exec("DELETE FROM pending_reschedules WHERE booking_id=?", (bid,))
    if booking:
        try:
            await cb.bot.send_message(booking["user_id"], "❌ Перенос отклонён мастером.")
        except Exception:
            pass
    try:
        await cb.message.edit_text((cb.message.text or "") + "\n\n❌ Отклонён")
    except Exception:
        pass
    await cb.answer("Отклонено")


@router.message(UserStates.booking_hand_photo, F.photo)
async def user_hand_photo(message: Message, state: FSMContext):
    await state.update_data(hand_photo_file_id=message.photo[-1].file_id)
    await state.set_state(UserStates.booking_hand_confirm)
    await message.answer("Всё верно?", reply_markup=yes_reset_kb())


@router.message(UserStates.booking_hand_photo)
async def user_hand_photo_invalid(message: Message):
    await message.answer("Пожалуйста, отправь фото.")


@router.message(UserStates.booking_hand_confirm)
async def user_hand_confirm(message: Message, state: FSMContext):
    if message.text == "Да":
        await state.set_state(UserStates.booking_length)
        await message.answer("Введи длину и форму:", reply_markup=ReplyKeyboardRemove())
    elif message.text == "Сбросить":
        await state.update_data(hand_photo_file_id=None)
        await state.set_state(UserStates.booking_hand_photo)
        await message.answer("Отправь фото ручек заново:",
                             reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer("Нажми 'Да' или 'Сбросить'.", reply_markup=yes_reset_kb())


@router.message(UserStates.booking_length)
async def user_length(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Введи текстом длину и форму.")
        return
    await state.update_data(length_shape=message.text)
    await state.set_state(UserStates.booking_reference)
    await message.answer("Отправь референс или мудборд (только фото):")


@router.message(UserStates.booking_reference, F.photo)
async def user_reference(message: Message, state: FSMContext):
    await state.update_data(reference=message.photo[-1].file_id)
    await state.set_state(UserStates.booking_reference_confirm)
    await message.answer("Всё верно?", reply_markup=yes_reset_kb())


@router.message(UserStates.booking_reference)
async def user_reference_invalid(message: Message):
    await message.answer("Пожалуйста, отправь референс как фото.")


@router.message(UserStates.booking_reference_confirm)
async def user_reference_confirm(message: Message, state: FSMContext):
    if message.text == "Да":
        await state.set_state(UserStates.booking_phone)
        await message.answer("Введи номер телефона:", reply_markup=ReplyKeyboardRemove())
    elif message.text == "Сбросить":
        await state.update_data(reference=None)
        await state.set_state(UserStates.booking_reference)
        await message.answer("Отправь референс заново:", reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer("Нажми 'Да' или 'Сбросить'.", reply_markup=yes_reset_kb())


@router.message(UserStates.booking_phone)
async def user_phone(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Введи номер телефона текстом.")
        return
    await state.update_data(phone=message.text)
    await state.set_state(UserStates.booking_name)
    await message.answer("Введи имя:")


@router.message(UserStates.booking_name)
async def user_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Введи имя текстом.")
        return
    await state.update_data(name=message.text)
    await state.set_state(UserStates.booking_promo)
    await message.answer("Введи промокод, если есть, или напиши 'нет':")


@router.message(UserStates.booking_promo)
async def user_promo(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Введи промокод или 'нет'.")
        return
    text = message.text.strip()
    data = await state.get_data()
    discount = 0; promo_code = None
    if text.lower() not in ("нет", "-", "no"):
        promo = db_one("SELECT * FROM promos WHERE code=?", (text,))
        if not promo:
            await message.answer("Промокод не найден. Введи другой или 'нет'.")
            return
        if promo["type"] == "personal" and promo["used_count"] >= promo["max_uses"]:
            await message.answer("Этот персональный промокод уже использован. "
                                 "Введи другой или 'нет'.")
            return
        discount = promo["discount"]; promo_code = text
    await state.update_data(discount=discount, promo_code=promo_code)

    month = db_one("SELECT * FROM months WHERE id=?", (data["month_id"],))
    dt_str = f"{data['day']:02d}.{month['month_num']:02d} {data['time']}"
    text_sum = (f"Проверь данные:\n"
                f"Имя: {data['name']}\n"
                f"Телефон: {data['phone']}\n"
                f"Длина и форма: {data['length_shape']}\n"
                f"Дата: {dt_str}\n"
                f"Промокод: {promo_code or 'нет'} (скидка {discount}%)")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="user_confirm_booking")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="user_cancel_booking")],
    ])
    await state.set_state(UserStates.booking_confirm)
    await message.answer(text_sum, reply_markup=kb)


@router.callback_query(F.data == "user_confirm_booking")
async def user_confirm_booking(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    month = db_one("SELECT * FROM months WHERE id=?", (data["month_id"],))
    day = data["day"]; time = data["time"]
    h, m = map(int, time.split(":"))
    dt = datetime(month["year"], month["month_num"], day, h, m)
    cur = db_exec("""INSERT INTO bookings(
        user_id, username, name, phone, hand_photo_file_id, length_shape,
        reference, promo_code, discount, status, slot_id, month_id, day, time,
        booking_datetime, created_at
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        cb.from_user.id, cb.from_user.username, data["name"], data["phone"],
        data["hand_photo_file_id"], data["length_shape"], data["reference"],
        data.get("promo_code"), data.get("discount", 0), "pending_payment",
        data["slot_id"], data["month_id"], day, time, dt.isoformat(),
        datetime.now().isoformat()
    ))
    await state.update_data(booking_id=cur.lastrowid)
    await cb.message.edit_text("Данные приняты.")
    await cb.message.answer(f"Реквизиты для предоплаты (200 руб):\n{get_payment_details()}\n\n"
                            f"Оплати 200 руб и отправь скрин сюда.")
    await state.set_state(UserStates.booking_payment_screenshot)
    await cb.answer()


@router.callback_query(F.data == "user_cancel_booking")
async def user_cancel_booking(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Главное меню:", reply_markup=user_main_kb())
    await cb.answer()


@router.message(UserStates.booking_payment_screenshot, F.photo)
async def user_payment_screenshot(message: Message, state: FSMContext):
    data = await state.get_data()
    bid = data.get("booking_id")
    is_extra = data.get("is_extra", False)
    if not bid:
        await message.answer("Что-то пошло не так. Начни заново /start")
        await state.clear()
        return
    file_id = message.photo[-1].file_id
    db_exec("UPDATE bookings SET payment_screenshot_file_id=? WHERE id=?", (file_id, bid))
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))

    media = [InputMediaPhoto(media=booking["hand_photo_file_id"])]
    if booking["reference"]:
        media.append(InputMediaPhoto(media=booking["reference"]))
    media.append(InputMediaPhoto(media=file_id))

    caption = await build_booking_caption(message.bot, booking)

    if is_extra:
        db_exec("UPDATE bookings SET status='confirmed' WHERE id=?", (bid,))
        caption += "\n\n🕐 ДОП ОКОШКО (уже подтверждено)"
        try:
            await send_to_admins(message.bot, media=media, caption=caption)
        except Exception as e:
            print(f"send extra media to admins error: {e}")
            for aid in ADMIN_IDS:
                try:
                    await message.bot.send_photo(aid, booking["hand_photo_file_id"])
                    if booking["reference"]:
                        await message.bot.send_photo(aid, booking["reference"])
                    await message.bot.send_photo(aid, file_id)
                    await message.bot.send_message(aid, caption)
                except Exception as e2:
                    print(f"fallback send error {aid}: {e2}")
        dt = datetime.fromisoformat(booking["booking_datetime"])
        now = datetime.now()
        if dt - timedelta(days=1) > now:
            scheduler.add_job(send_reminder, "date",
                              run_date=dt - timedelta(days=1),
                              args=[message.bot, booking["user_id"],
                                    f"Напоминание: завтра у вас запись на {dt.strftime('%d.%m %H:%M')}"])
        if dt - timedelta(hours=1) > now:
            scheduler.add_job(send_reminder, "date",
                              run_date=dt - timedelta(hours=1),
                              args=[message.bot, booking["user_id"],
                                    f"Напоминание: через час у вас запись на {dt.strftime('%d.%m %H:%M')}"])
        await message.answer(
            "Скрин получен! ✅ Запись на доп. окошко подтверждена.\n"
            "Ждём тебя 💅"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить",
                                  callback_data=f"admin_confirm_booking:{bid}")],
            [InlineKeyboardButton(text="❌ Отмена",
                                  callback_data=f"admin_cancel_booking:{bid}")],
        ])
        try:
            await send_to_admins(message.bot, media=media, caption=caption, reply_markup=kb)
        except Exception as e:
            print(f"send media to admins error: {e}")
            for aid in ADMIN_IDS:
                try:
                    await message.bot.send_photo(aid, booking["hand_photo_file_id"])
                    if booking["reference"]:
                        await message.bot.send_photo(aid, booking["reference"])
                    await message.bot.send_photo(aid, file_id)
                    await message.bot.send_message(aid, caption, reply_markup=kb)
                except Exception as e2:
                    print(f"fallback send error {aid}: {e2}")
        await message.answer("Скрин отправлен. Ожидай подтверждения мастера.")

    await state.clear()


@router.callback_query(F.data.startswith("admin_confirm_booking:"))
async def admin_confirm_booking(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    bid = int(cb.data.split(":")[1])
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if not booking:
        await cb.answer("Не найдено")
        return
    db_exec("UPDATE bookings SET status='confirmed' WHERE id=?", (bid,))
    db_exec("UPDATE slots SET is_booked=1 WHERE id=?", (booking["slot_id"],))
    if booking["promo_code"]:
        db_exec("UPDATE promos SET used_count = used_count + 1 WHERE code=?",
                (booking["promo_code"],))
    dt = datetime.fromisoformat(booking["booking_datetime"])
    now = datetime.now()
    if dt - timedelta(days=1) > now:
        scheduler.add_job(send_reminder, "date", run_date=dt - timedelta(days=1),
                          args=[cb.bot, booking["user_id"],
                                f"Напоминание: завтра у вас запись на {dt.strftime('%d.%m %H:%M')}"])
    if dt - timedelta(hours=1) > now:
        scheduler.add_job(send_reminder, "date", run_date=dt - timedelta(hours=1),
                          args=[cb.bot, booking["user_id"],
                                f"Напоминание: через час у вас запись на {dt.strftime('%d.%m %H:%M')}"])
    try:
        await cb.bot.send_message(booking["user_id"],
                                  f"✅ Твоя запись подтверждена: {dt.strftime('%d.%m %H:%M')}")
    except Exception:
        pass
    await edit_admin_message(cb, "✅ Подтверждено")
    await cb.answer("Подтверждено")


@router.callback_query(F.data.startswith("admin_cancel_booking:"))
async def admin_cancel_booking(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    bid = int(cb.data.split(":")[1])
    booking = db_one("SELECT * FROM bookings WHERE id=?", (bid,))
    if booking:
        db_exec("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
        try:
            await cb.bot.send_message(booking["user_id"], "❌ Твоя запись отменена мастером.")
        except Exception:
            pass
    await edit_admin_message(cb, "❌ Отменено")
    await cb.answer("Отменено")


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    scheduler.start()
    scheduler.add_job(daily_report, "cron", hour=8, minute=0, args=[bot])
    await schedule_existing_reminders(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
