import asyncio
import logging
import os
import re
import sqlite3
from datetime import date, datetime

import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)

load_dotenv()
client = anthropic.AsyncAnthropic()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}

if not TOKEN or not ALLOWED:
    raise SystemExit("Заповни TELEGRAM_BOT_TOKEN і ALLOWED_USER_IDS у .env")

db = sqlite3.connect("budget.db")
db.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    category TEXT NOT NULL
)
""")
db.commit()

dp = Dispatcher()
# Бот відповідає лише дозволеним користувачам (і на повідомлення, і на кнопки)
dp.message.filter(F.from_user.id.in_(ALLOWED))
dp.callback_query.filter(F.from_user.id.in_(ALLOWED))

HELP = (
    "Команди:\n"
    "/expense 85 кава - записати витрату\n"
    "/income 30000 зарплата - записати надходження\n"
    "/report - звіт за поточний місяць\n"
    "/report жовтень - звіт за інший місяць (або /report 2026-10)\n"
    "/undo - видалити свій останній запис\n"
    "/reset - видалити всі дані\n\n"
    "Або просто пиши текстом: «вчора купив каву за 85», «покажи підсумок», "
    "«звіт за жовтень», «видали останній запис», «форматни дані»."
)

KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📊 Звіт")]],
    resize_keyboard=True,
)

CONFIRM_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🗑 Так, видалити все", callback_data="reset_yes"),
    InlineKeyboardButton(text="Скасувати", callback_data="reset_no"),
]])

# Фрази, за якими бот розуміє намір без звернення до ШІ
UNDO_WORDS = (
    "останній запис", "останню витрат", "останнє надходж", "видали останн",
    "скасуй останн", "відміни останн", "прибери останн", "undo",
)
RESET_WORDS = (
    "форматн", "очист", "обнул", "видали все", "видалити все",
    "видали дані", "стерти", "з нуля", "скинь дані",
)

WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]
MONTH_NAMES = [
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
]

RECORD_TOOL = {
    "name": "save_transactions",
    "description": "Зберегти всі витрати та надходження з повідомлення користувача",
    "input_schema": {
        "type": "object",
        "properties": {
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["expense", "income"]},
                        "amount": {"type": "number"},
                        "category": {"type": "string"},
                        "date": {
                            "type": "string",
                            "description": "Дата запису у форматі YYYY-MM-DD. "
                                           "Вказуй лише якщо користувач сам назвав "
                                           "дату чи день («вчора», «15 жовтня»). "
                                           "Інакше не заповнюй це поле.",
                        },
                    },
                    "required": ["kind", "amount", "category"],
                },
            }
        },
        "required": ["transactions"],
    },
}

REPORT_TOOL = {
    "name": "show_report",
    "description": "Показати звіт, коли користувач просить підсумок, баланс, "
                   "статистику або питає, скільки витрачено чи залишилось",
    "input_schema": {
        "type": "object",
        "properties": {
            "month": {
                "type": "string",
                "description": "Місяць звіту у форматі YYYY-MM. Вказуй лише якщо "
                               "користувач назвав конкретний місяць («за жовтень», "
                               "«минулий місяць»). Для поточного місяця не заповнюй.",
            }
        },
    },
}

RESET_TOOL = {
    "name": "reset_data",
    "description": "Користувач хоче видалити, очистити, обнулити або відформатувати "
                   "ВСІ дані і почати облік з нуля",
    "input_schema": {"type": "object", "properties": {}},
}

UNDO_TOOL = {
    "name": "undo_last",
    "description": "Користувач хоче видалити, скасувати або відмінити лише "
                   "ОСТАННІЙ запис (одну останню витрату чи надходження)",
    "input_schema": {"type": "object", "properties": {}},
}


def valid_month(raw) -> str | None:
    """Перевіряє місяць у форматі YYYY-MM: не з майбутнього, не раніше 2000 року."""
    try:
        d = datetime.strptime(str(raw), "%Y-%m")
    except ValueError:
        return None
    month = d.strftime("%Y-%m")
    if d.year < 2000 or month > date.today().strftime("%Y-%m"):
        return None
    return month


def parse_month_text(text: str) -> str | None:
    """Розбирає ручне введення: «жовтень», «жовтень 2025», «2026-10», «10.2026»."""
    text = text.strip().lower()
    if not text:
        return None
    month = valid_month(text)
    if month:
        return month
    try:
        return valid_month(datetime.strptime(text, "%m.%Y").strftime("%Y-%m"))
    except ValueError:
        pass
    today = date.today()
    year_match = re.search(r"\b(20\d\d)\b", text)
    for i, name in enumerate(MONTH_NAMES, start=1):
        if text.startswith(name[:3]):
            if year_match:
                year = int(year_match.group(1))
            else:
                year = today.year if i <= today.month else today.year - 1
            return valid_month(f"{year}-{i:02d}")
    return None


def month_title(month: str) -> str:
    year, num = month.split("-")
    return f"{MONTH_NAMES[int(num) - 1]} {year}"


def available_months() -> list[str]:
    rows = db.execute(
        "SELECT DISTINCT substr(created_at, 1, 7) FROM transactions ORDER BY 1 DESC"
    ).fetchall()
    return [r[0] for r in rows]


def build_report(month: str | None = None) -> str:
    month = month or date.today().strftime("%Y-%m")
    rows = db.execute(
        "SELECT kind, category, SUM(amount) FROM transactions "
        "WHERE created_at LIKE ? GROUP BY kind, category ORDER BY SUM(amount) DESC",
        (month + "%",),
    ).fetchall()
    if not rows:
        text = f"Записів за {month_title(month)} немає."
        months = available_months()
        if months:
            text += "\nДані є за: " + ", ".join(month_title(m) for m in months[:6])
        return text
    total_in = sum(r[2] for r in rows if r[0] == "income")
    total_out = sum(r[2] for r in rows if r[0] == "expense")
    lines = [f"📊 Звіт: {month_title(month)}", f"Надходження: {total_in:.2f}",
             f"Витрати: {total_out:.2f}", f"Залишок: {total_in - total_out:.2f}", "",
             "Витрати за категоріями:"]
    lines += [f"- {cat}: {s:.2f}" for kind, cat, s in rows if kind == "expense"]
    return "\n".join(lines)


def resolve_date(raw) -> tuple[date, bool]:
    """Перевіряє дату від ШІ. Повертає (дата, чи_була_проблема)."""
    today = date.today()
    if not raw:
        return today, False
    try:
        d = datetime.strptime(str(raw), "%Y-%m-%d").date()
    except ValueError:
        return today, True
    if d > today or (today - d).days > 366:
        return today, True
    return d, False


def build_system_prompt() -> str:
    today = date.today()
    return (
        f"Сьогодні {today.isoformat()} ({WEEKDAYS[today.weekday()]}). "
        "Ти асистент сімейного бюджету, користувачі пишуть українською. "
        "Якщо повідомлення містить суми витрат чи надходжень, викликай "
        "save_transactions і не вигадуй сум, яких немає в тексті. Категорію "
        "пиши одним-двома словами з малої літери. Якщо користувач вказує, коли "
        "це було («вчора», «позавчора», «у п'ятницю», «15 жовтня»), обчисли "
        "дату у форматі YYYY-MM-DD і запиши в поле date. Якщо рік не вказано, "
        "бери поточний. Якщо дату не згадано, не заповнюй поле date. "
        "Якщо користувач просить звіт, підсумок, баланс або питає про свої "
        "витрати, викликай show_report. Якщо він назвав конкретний місяць "
        "(«звіт за жовтень», «що було минулого місяця»), обчисли його у форматі "
        "YYYY-MM і запиши в поле month; якщо такий місяць цього року ще не "
        "настав, бери минулий рік. Для поточного місяця поле month не заповнюй. "
        "Якщо користувач хоче видалити, очистити, обнулити чи відформатувати "
        "ВСІ дані (наприклад: «форматни дані», «очисти все», «почнемо з нуля»), "
        "викликай reset_data. Якщо користувач хоче видалити чи скасувати лише "
        "останній запис (наприклад: «видали останній запис», «скасуй останню "
        "витрату», «я помилився»), викликай undo_last. Якщо немає нічого з "
        "цього, викликай save_transactions з порожнім списком."
    )


async def interpret(text: str) -> tuple[str, dict]:
    response = await client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=500,
        system=build_system_prompt(),
        tools=[RECORD_TOOL, REPORT_TOOL, RESET_TOOL, UNDO_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": text}],
    )
    logging.info("Токени: вхід=%s, вихід=%s",
                 response.usage.input_tokens, response.usage.output_tokens)
    for block in response.content:
        if block.type == "tool_use":
            return block.name, block.input
    return "save_transactions", {"transactions": []}


async def add_record(message: Message, command: CommandObject, kind: str):
    if not command.args:
        await message.answer("Формат: /expense 85 кава")
        return
    parts = command.args.split(maxsplit=1)
    try:
        amount = float(parts[0].replace(",", "."))
    except ValueError:
        await message.answer("Сума має бути числом. Приклад: /expense 85 кава")
        return
    if amount <= 0:
        await message.answer("Сума має бути більшою за нуль.")
        return
    category = parts[1].strip() if len(parts) > 1 else "без категорії"
    db.execute(
        "INSERT INTO transactions (user_id, created_at, kind, amount, category) "
        "VALUES (?, ?, ?, ?, ?)",
        (message.from_user.id, datetime.now().isoformat(timespec="seconds"),
         kind, amount, category),
    )
    db.commit()
    label = "Витрата" if kind == "expense" else "Надходження"
    await message.answer(f"✅ {label}: {amount:.2f} ({category})")


async def undo_last(message: Message):
    row = db.execute(
        "SELECT id, kind, amount, category FROM transactions "
        "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (message.from_user.id,),
    ).fetchone()
    if row is None:
        await message.answer("У тебе ще немає записів, видаляти нічого.")
        return
    record_id, kind, amount, category = row
    db.execute("DELETE FROM transactions WHERE id = ?", (record_id,))
    db.commit()
    sign = "➖" if kind == "expense" else "➕"
    await message.answer(f"↩️ Видалено останній запис: {sign} {amount:.2f} ({category})")


async def ask_reset(message: Message):
    count = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    if count == 0:
        await message.answer("Даних і так немає, видаляти нічого.")
        return
    await message.answer(
        f"⚠️ Видалити всі записи ({count})? Це незворотно. "
        "Перед видаленням я збережу резервну копію.",
        reply_markup=CONFIRM_KB,
    )


@dp.message(Command("start", "help"))
async def start(message: Message):
    await message.answer(HELP, reply_markup=KEYBOARD)


@dp.message(Command("expense"))
async def expense(message: Message, command: CommandObject):
    await add_record(message, command, "expense")


@dp.message(Command("income"))
async def income(message: Message, command: CommandObject):
    await add_record(message, command, "income")


@dp.message(Command("report"))
async def report(message: Message, command: CommandObject):
    month = None
    if command.args:
        month = parse_month_text(command.args)
        if not month:
            await message.answer(
                "Не зрозумів місяць. Приклади: /report жовтень, /report 2026-10"
            )
            return
    await message.answer(build_report(month))


@dp.message(F.text == "📊 Звіт")
async def report_button(message: Message):
    await message.answer(build_report())


@dp.message(Command("undo"))
async def undo_command(message: Message):
    await undo_last(message)


@dp.message(Command("reset"))
async def reset_command(message: Message):
    await ask_reset(message)


@dp.callback_query(F.data == "reset_yes")
async def reset_yes(callback: CallbackQuery):
    backup = f"budget_backup_{datetime.now():%Y%m%d_%H%M%S}.db"
    dst = sqlite3.connect(backup)
    db.backup(dst)
    dst.close()
    db.execute("DELETE FROM transactions")
    db.execute("DELETE FROM sqlite_sequence WHERE name = 'transactions'")
    db.commit()
    await callback.message.edit_text(f"✅ Усі дані видалено. Резервна копія: {backup}")
    await callback.answer()


@dp.callback_query(F.data == "reset_no")
async def reset_no(callback: CallbackQuery):
    await callback.message.edit_text("Скасовано, дані збережено.")
    await callback.answer()


@dp.message(F.text & ~F.text.startswith("/"))
async def free_text(message: Message):
    text_lower = message.text.lower()

    # Спочатку специфічна фраза про останній запис, потім про повне очищення
    if any(w in text_lower for w in UNDO_WORDS):
        await undo_last(message)
        return
    if any(w in text_lower for w in RESET_WORDS):
        await ask_reset(message)
        return

    try:
        action, data = await interpret(message.text)
    except Exception:
        logging.exception("Помилка звернення до ШІ")
        await message.answer("Не вдалося звернутися до ШІ. Спробуй пізніше або використай /expense.")
        return

    if action == "show_report":
        raw_month = data.get("month")
        month = valid_month(raw_month) if raw_month else None
        note = ""
        if raw_month and not month:
            note = "⚠️ Місяць не вдалося визначити, показую поточний.\n\n"
        await message.answer(note + build_report(month))
        return
    if action == "undo_last":
        await undo_last(message)
        return
    if action == "reset_data":
        await ask_reset(message)
        return

    records = data.get("transactions", [])
    valid = [
        r for r in records
        if r.get("kind") in ("expense", "income") and r.get("amount", 0) > 0
    ]
    if not valid:
        await message.answer("Не знайшов у повідомленні сум. Спробуй ще раз.")
        return

    lines = []
    date_problem = False
    for r in valid:
        d, bad = resolve_date(r.get("date"))
        date_problem = date_problem or bad
        created_at = f"{d.isoformat()}T{datetime.now():%H:%M:%S}"
        db.execute(
            "INSERT INTO transactions (user_id, created_at, kind, amount, category) "
            "VALUES (?, ?, ?, ?, ?)",
            (message.from_user.id, created_at, r["kind"], r["amount"], r["category"]),
        )
        sign = "➖" if r["kind"] == "expense" else "➕"
        suffix = "" if d == date.today() else f" — {d:%d.%m.%Y}"
        lines.append(f"{sign} {r['amount']:.2f} ({r['category']}){suffix}")
    db.commit()

    answer = "Записав:\n" + "\n".join(lines)
    if date_problem:
        answer += "\n\n⚠️ Дату не вдалося визначити, записано сьогоднішньою."
    await message.answer(answer)


async def main():
    bot = Bot(token=TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())