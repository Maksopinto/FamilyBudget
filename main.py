import asyncio
import logging
import os
import sqlite3
from datetime import datetime

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
    "/undo - видалити свій останній запис\n"
    "/reset - видалити всі дані\n\n"
    "Або просто пиши текстом: «купив каву за 85», «покажи підсумок», "
    "«видали останній запис», «форматни дані»."
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
    "input_schema": {"type": "object", "properties": {}},
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


def build_report() -> str:
    month = datetime.now().strftime("%Y-%m")
    rows = db.execute(
        "SELECT kind, category, SUM(amount) FROM transactions "
        "WHERE created_at LIKE ? GROUP BY kind, category ORDER BY SUM(amount) DESC",
        (month + "%",),
    ).fetchall()
    if not rows:
        return "За цей місяць записів ще немає."
    total_in = sum(r[2] for r in rows if r[0] == "income")
    total_out = sum(r[2] for r in rows if r[0] == "expense")
    lines = [f"📊 Звіт за {month}", f"Надходження: {total_in:.2f}",
             f"Витрати: {total_out:.2f}", f"Залишок: {total_in - total_out:.2f}", "",
             "Витрати за категоріями:"]
    lines += [f"- {cat}: {s:.2f}" for kind, cat, s in rows if kind == "expense"]
    return "\n".join(lines)


async def interpret(text: str) -> tuple[str, dict]:
    response = await client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=500,
        system=(
            "Ти асистент сімейного бюджету, користувачі пишуть українською. "
            "Якщо повідомлення містить суми витрат чи надходжень, викликай "
            "save_transactions і не вигадуй сум, яких немає в тексті. Категорію "
            "пиши одним-двома словами з малої літери. Якщо користувач просить "
            "звіт, підсумок, баланс або питає про свої витрати, викликай "
            "show_report. Якщо користувач хоче видалити, очистити, обнулити чи "
            "відформатувати ВСІ дані (наприклад: «форматни дані», «очисти все», "
            "«почнемо з нуля»), викликай reset_data. Якщо користувач хоче "
            "видалити чи скасувати лише останній запис (наприклад: «видали "
            "останній запис», «скасуй останню витрату», «я помилився»), викликай "
            "undo_last. Якщо немає нічого з цього, викликай save_transactions "
            "з порожнім списком."
        ),
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
async def report(message: Message):
    await message.answer(build_report())


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
        await message.answer(build_report())
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

    now = datetime.now().isoformat(timespec="seconds")
    lines = []
    for r in valid:
        db.execute(
            "INSERT INTO transactions (user_id, created_at, kind, amount, category) "
            "VALUES (?, ?, ?, ?, ?)",
            (message.from_user.id, now, r["kind"], r["amount"], r["category"]),
        )
        sign = "➖" if r["kind"] == "expense" else "➕"
        lines.append(f"{sign} {r['amount']:.2f} ({r['category']})")
    db.commit()
    await message.answer("Записав:\n" + "\n".join(lines))


async def main():
    bot = Bot(token=TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())