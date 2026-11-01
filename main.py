import logging
import anthropic
import asyncio
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv

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
# Бот відповідає лише дозволеним користувачам
dp.message.filter(F.from_user.id.in_(ALLOWED))

HELP = (
    "Команди:\n"
    "/expense 85 кава - записати витрату\n"
    "/income 30000 зарплата - записати надходження\n"
    "/report - звіт за поточний місяць"
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


async def parse_transactions(text: str) -> list[dict]:
    response = await client.messages.create(
        model="claude-haiku-5-5",
        max_tokens=500,
        system=(
            "Ти витягуєш фінансові записи з повідомлень українською. "
            "Не вигадуй суми, яких немає в тексті. Категорію пиши одним-двома "
            "словами з малої літери. Якщо записів немає, поверни порожній список."
        ),
        tools=[RECORD_TOOL],
        tool_choice={"type": "tool", "name": "save_transactions"},
        messages=[{"role": "user", "content": text}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.input.get("transactions", [])
    return []


@dp.message(Command("start", "help"))
async def start(message: Message):
    await message.answer(HELP)


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


@dp.message(Command("expense"))
async def expense(message: Message, command: CommandObject):
    await add_record(message, command, "expense")


@dp.message(Command("income"))
async def income(message: Message, command: CommandObject):
    await add_record(message, command, "income")


@dp.message(Command("report"))
async def report(message: Message):
    month = datetime.now().strftime("%Y-%m")
    rows = db.execute(
        "SELECT kind, category, SUM(amount) FROM transactions "
        "WHERE created_at LIKE ? GROUP BY kind, category ORDER BY SUM(amount) DESC",
        (month + "%",),
    ).fetchall()
    if not rows:
        await message.answer("За цей місяць записів ще немає.")
        return
    total_in = sum(r[2] for r in rows if r[0] == "income")
    total_out = sum(r[2] for r in rows if r[0] == "expense")
    lines = [f"📊 Звіт за {month}", f"Надходження: {total_in:.2f}",
             f"Витрати: {total_out:.2f}", f"Залишок: {total_in - total_out:.2f}", "",
             "Витрати за категоріями:"]
    lines += [f"- {cat}: {s:.2f}" for kind, cat, s in rows if kind == "expense"]
    await message.answer("\n".join(lines))

@dp.message(F.text & ~F.text.startswith("/"))
async def free_text(message: Message):
    try:
        records = await parse_transactions(message.text)
    except Exception:
        logging.exception("Помилка звернення до ШІ")
        await message.answer("Не вдалося звернутися до ШІ. Спробуй пізніше або використай /expense.")
        return

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