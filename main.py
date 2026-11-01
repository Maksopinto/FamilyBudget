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
    "Або просто пиши текстом:\n"
    "«вчора купив каву за 85»\n"
    "«звіт за жовтень»\n"
    "«порівняй жовтень і листопад»\n"
    "«скільки пішло на каву в жовтні?»\n"
    "«видали останній запис», «форматни дані»"
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
MAX_MONTHS = 12

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
    "description": "Показати повний звіт за ОДИН місяць (без порівняння і без "
                   "конкретної категорії): підсумок, баланс, статистика",
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

ANALYZE_TOOL = {
    "name": "analyze_spending",
    "description": "Порівняти витрати й надходження за кілька місяців, АБО "
                   "дізнатися, скільки грошей пішло чи надійшло на конкретну "
                   "категорію за один чи кілька місяців",
    "input_schema": {
        "type": "object",
        "properties": {
            "months": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Усі потрібні місяці у форматі YYYY-MM. Для "
                               "порівняння перелічи кожен місяць. Якщо місяць не "
                               "названо, залиш порожнім (буде поточний).",
            },
            "category": {
                "type": "string",
                "description": "Категорія, якщо питають про конкретну статтю "
                               "(кава, таксі). Бери назву, найближчу до наявних "
                               "категорій. Без категорії поле не заповнюй.",
            },
            "kind": {
                "type": "string",
                "enum": ["expense", "income"],
                "description": "expense, якщо питають про витрати («скільки пішло», "
                               "«скільки витратили»); income, якщо про надходження "
                               "(«скільки отримали»). Якщо незрозуміло, не заповнюй.",
            },
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


def known_categories(limit: int = 40) -> list[str]:
    rows = db.execute(
        "SELECT category, COUNT(*) AS n FROM transactions "
        "GROUP BY category ORDER BY n DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r[0].strip()[:40] for r in rows if r[0].strip()]


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


def build_analysis(months: list[str], category: str | None, kind: str | None) -> str:
    """Порівняння місяців або підсумок за категорією. Усі суми рахує код."""
    months = months or [date.today().strftime("%Y-%m")]
    placeholders = ",".join("?" * len(months))  # лише знаки питання, значення - параметрами
    rows = db.execute(
        "SELECT substr(created_at, 1, 7), kind, category, amount FROM transactions "
        f"WHERE substr(created_at, 1, 7) IN ({placeholders})",
        months,
    ).fetchall()

    cat = category.strip().lower() if category else None
    stats = {m: {"expense": 0.0, "income": 0.0} for m in months}
    for month, k, c, amount in rows:
        if cat and cat not in c.lower():
            continue
        stats[month][k] += amount

    # --- Запит про конкретну категорію ---
    if cat:
        kinds = [kind] if kind else ["expense", "income"]
        if all(stats[m][k] == 0 for m in months for k in kinds):
            text = f"За запитом «{category}» записів у вказаних місяцях не знайшов."
            cats = known_categories(15)
            if cats:
                text += "\nНаявні категорії: " + ", ".join(cats)
            return text
        lines = [f"🔎 «{category}»"]
        for m in months:
            parts = []
            for k in kinds:
                if stats[m][k] or kind:
                    label = "витрати" if k == "expense" else "надходження"
                    parts.append(f"{label} {stats[m][k]:.2f}")
            lines.append(f"{month_title(m)}: " + (", ".join(parts) if parts else "0.00"))
        if len(months) > 1:
            totals = []
            for k in kinds:
                total = sum(stats[m][k] for m in months)
                if total or kind:
                    label = "витрати" if k == "expense" else "надходження"
                    totals.append(f"{label} {total:.2f}")
            lines.append("Разом: " + ", ".join(totals))
        return "\n".join(lines)

    # --- Загальне порівняння місяців ---
    title = "📈 Порівняння місяців" if len(months) > 1 else f"📊 {month_title(months[0])}"
    lines = [title]
    for m in months:
        inc, exp = stats[m]["income"], stats[m]["expense"]
        lines.append(
            f"{month_title(m)}: надходження {inc:.2f}, витрати {exp:.2f}, "
            f"залишок {inc - exp:.2f}"
        )
    if len(months) > 1:
        first, last = months[0], months[-1]
        lines.append("")
        for label, k in (("Витрати", "expense"), ("Надходження", "income")):
            a, b = stats[first][k], stats[last][k]
            diff = b - a
            pct = f" ({diff / a * 100:+.0f}%)" if a else ""
            lines.append(
                f"{label}: {diff:+.2f}{pct} "
                f"({month_title(first)} → {month_title(last)})"
            )
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
    cats = known_categories()
    cats_text = ("Наявні категорії в обліку: " + ", ".join(cats) + ". ") if cats else ""
    return (
        f"Сьогодні {today.isoformat()} ({WEEKDAYS[today.weekday()]}). "
        "Ти асистент сімейного бюджету, користувачі пишуть українською. "
        + cats_text +
        "Якщо повідомлення містить суми витрат чи надходжень, викликай "
        "save_transactions і не вигадуй сум, яких немає в тексті. Категорію "
        "пиши одним-двома словами з малої літери у називному відмінку, за "
        "можливості використовуй наявні категорії. Якщо користувач вказує, коли "
        "це було («вчора», «позавчора», «у п'ятницю», «15 жовтня»), обчисли "
        "дату у форматі YYYY-MM-DD і запиши в поле date. Якщо рік не вказано, "
        "бери поточний. Якщо дату не згадано, не заповнюй поле date. "
        "Якщо користувач просить звіт чи підсумок за ОДИН місяць без категорії, "
        "викликай show_report; якщо назвав конкретний місяць, обчисли його у "
        "форматі YYYY-MM і запиши в поле month, для поточного місяця поле не "
        "заповнюй. Якщо користувач хоче порівняти місяці або питає, скільки "
        "грошей пішло чи надійшло на конкретну категорію (за один чи кілька "
        "місяців), викликай analyze_spending: у months перелічи всі потрібні "
        "місяці у форматі YYYY-MM (для «цей місяць» і «минулий місяць» обчисли "
        "від сьогоднішньої дати; «останні 3 місяці» - три місяці, включно з "
        "поточним), у category - назву категорії, найближчу до наявних, у kind "
        "- expense для витрат чи income для надходжень. Якщо такий місяць цього "
        "року ще не настав, бери минулий рік. "
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
        tools=[RECORD_TOOL, REPORT_TOOL, ANALYZE_TOOL, RESET_TOOL, UNDO_TOOL],
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

    if action == "analyze_spending":
        raw_months = data.get("months") or []
        months = sorted({m for m in (valid_month(x) for x in raw_months[:MAX_MONTHS]) if m})
        note = ""
        if raw_months and not months:
            note = "⚠️ Місяці не вдалося визначити, показую поточний.\n\n"
        category = data.get("category")
        if not isinstance(category, str) or not category.strip():
            category = None
        kind = data.get("kind") if data.get("kind") in ("expense", "income") else None
        await message.answer(note + build_analysis(months, category, kind))
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