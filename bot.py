import json
import os
import datetime
from datetime import date, timedelta
from pathlib import Path
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ── Хранилище данных ───────────────────────────────────────────────────────────
# На Bothost папка /app/data сохраняется между перезапусками
DATA_DIR  = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "planner.json"

def load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {}

def save_data(data: dict):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_day_key(d: date = None) -> str:
    return (d or date.today()).strftime("%d.%m.%y")

def get_user(data: dict, user_id: int) -> dict:
    key = str(user_id)
    if key not in data:
        data[key] = {"days": {}}
    return data[key]

# ── Состояния диалогов ─────────────────────────────────────────────────────────
ADDING_TASKS, ADDING_REVENUE, ADDING_NET = range(3)

# ─────────────────────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────────────────────────────────────

def fmt_day_report(day_data: dict, day_key: str) -> str:
    revenue  = day_data.get("revenue", 0)
    net      = day_data.get("net", 0)
    tasks    = day_data.get("tasks", [])
    prev_rev = day_data.get("period_revenue", revenue)
    prev_net = day_data.get("period_net", net)

    done  = sum(1 for t in tasks if t.get("done"))
    total = len(tasks)
    pct   = round(done / total * 100) if total else 0

    lines = [
        f"📅 *{day_key}*\n",
        f"Факт по выручке: *{revenue:,}*",
        f"Факт по чистым: *{net:,}*",
        f"Итого за период (выручка): *{prev_rev:,}*",
        f"Итого за период (чистые): *{prev_net:,}*",
        f"Прогресс: *{pct}%* ({done}/{total})\n",
        "Факт:"
    ]
    for i, t in enumerate(tasks, 1):
        mark = "✅" if t.get("done") else "❌"
        lines.append(f"{i}. {t['text']}{mark}")
    return "\n".join(lines)


def build_task_keyboard(tasks: list, day_key: str) -> InlineKeyboardMarkup:
    rows = []
    for i, t in enumerate(tasks):
        mark  = "✅" if t.get("done") else "❌"
        label = f"{mark} {i+1}. {t['text'][:30]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"toggle:{day_key}:{i}")])
    rows.append([InlineKeyboardButton("💾 Сохранить отчёт", callback_data=f"save:{day_key}")])
    return InlineKeyboardMarkup(rows)

# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я твой ежедневный планировщик.\n\n"
        "Команды:\n"
        "  /plan — добавить план на завтра\n"
        "  /today — открыть задачи на сегодня\n"
        "  /revenue — ввести выручку за сегодня\n"
        "  /report — итоговый отчёт за сегодня\n"
        "  /history — история за последние 7 дней\n"
    )

# ─────────────────────────────────────────────────────────────────────────────
# /plan — добавить задачи на завтра
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tomorrow = date.today() + timedelta(days=1)
    key = get_day_key(tomorrow)
    ctx.user_data["plan_key"]   = key
    ctx.user_data["plan_tasks"] = []
    await update.message.reply_text(
        f"📝 Введи задачи на *{key}* — по одной на каждое сообщение.\n"
        "Когда закончишь — напиши /done",
        parse_mode="Markdown"
    )
    return ADDING_TASKS


async def receive_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return ADDING_TASKS
    ctx.user_data["plan_tasks"].append({"text": text, "done": False})
    count = len(ctx.user_data["plan_tasks"])
    await update.message.reply_text(f"✔️ Задача {count} добавлена: «{text}»")
    return ADDING_TASKS


async def done_adding(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tasks = ctx.user_data.get("plan_tasks", [])
    key   = ctx.user_data.get("plan_key")
    if not tasks:
        await update.message.reply_text("Ты не добавил ни одной задачи. Попробуй ещё раз /plan")
        return ConversationHandler.END

    data = load_data()
    user = get_user(data, update.effective_user.id)
    day  = user["days"].setdefault(key, {})
    day["tasks"] = tasks
    save_data(data)

    lines = [f"*План на {key}:*"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t['text']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# /today — задачи на сегодня с кнопками
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    day  = user["days"].get(key)

    if not day or not day.get("tasks"):
        await update.message.reply_text(
            f"На сегодня ({key}) нет задач.\nДобавь план через /plan вечером."
        )
        return

    text = fmt_day_report(day, key)
    kb   = build_task_keyboard(day["tasks"], key)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# Callback: отметить задачу / сохранить
# ─────────────────────────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "noop":
        return

    data = load_data()
    user = get_user(data, query.from_user.id)

    if query.data.startswith("toggle:"):
        _, day_key, idx_str = query.data.split(":")
        day = user["days"].get(day_key)
        if not day:
            return
        idx = int(idx_str)
        day["tasks"][idx]["done"] = not day["tasks"][idx]["done"]
        save_data(data)
        await query.edit_message_text(
            fmt_day_report(day, day_key),
            reply_markup=build_task_keyboard(day["tasks"], day_key),
            parse_mode="Markdown"
        )

    elif query.data.startswith("save:"):
        _, day_key = query.data.split(":")
        day = user["days"].get(day_key)
        if not day:
            return
        await query.edit_message_text(
            fmt_day_report(day, day_key) + "\n\n✅ *Отчёт сохранён!*",
            parse_mode="Markdown"
        )

# ─────────────────────────────────────────────────────────────────────────────
# /revenue — ввести выручку и чистые
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_revenue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Введи *факт по выручке* за сегодня (число):", parse_mode="Markdown")
    return ADDING_REVENUE


async def receive_revenue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("Введи число, например: 150000")
        return ADDING_REVENUE
    ctx.user_data["revenue"] = val
    await update.message.reply_text("💵 Теперь введи *факт по чистым*:", parse_mode="Markdown")
    return ADDING_NET


async def receive_net(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("Введи число, например: 50000")
        return ADDING_NET

    uid     = update.effective_user.id
    key     = get_day_key()
    revenue = ctx.user_data.get("revenue", 0)
    data    = load_data()
    user    = get_user(data, uid)

    total_rev = sum(d.get("revenue", 0) for d in user["days"].values()) + revenue
    total_net = sum(d.get("net", 0)     for d in user["days"].values()) + val

    day = user["days"].setdefault(key, {})
    day["revenue"]        = revenue
    day["net"]            = val
    day["period_revenue"] = total_rev
    day["period_net"]     = total_net
    save_data(data)

    await update.message.reply_text(
        f"✅ Записано!\nВыручка: *{revenue:,}* | Чистые: *{val:,}*\n"
        f"Итого за период: выручка *{total_rev:,}* / чистые *{total_net:,}*",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# /report — итоговый отчёт за сегодня
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    day  = user["days"].get(key)
    if not day:
        await update.message.reply_text("Данных за сегодня нет.")
        return
    await update.message.reply_text(fmt_day_report(day, key), parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# /history — последние 7 дней
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    days = user["days"]
    if not days:
        await update.message.reply_text("История пуста.")
        return
    keys  = sorted(days.keys(), reverse=True)[:7]
    parts = []
    for k in keys:
        day   = days[k]
        tasks = day.get("tasks", [])
        done  = sum(1 for t in tasks if t.get("done"))
        total = len(tasks)
        pct   = round(done / total * 100) if total else 0
        rev   = day.get("revenue", 0)
        net   = day.get("net", 0)
        parts.append(
            f"📅 *{k}* — {pct}% ({done}/{total}) | "
            f"Выручка: {rev:,} | Чистые: {net:,}"
        )
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# Утреннее напоминание (09:00 МСК)
# ─────────────────────────────────────────────────────────────────────────────

async def morning_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    data = load_data()
    for user_id_str, user_data in data.items():
        day = user_data.get("days", {}).get(key)
        if day and day.get("tasks"):
            tasks = day["tasks"]
            text  = f"☀️ *Доброе утро! Вот твой план на {key}:*\n"
            for i, t in enumerate(tasks, 1):
                text += f"\n{i}. {t['text']}"
            try:
                await ctx.bot.send_message(
                    chat_id=int(user_id_str),
                    text=text,
                    reply_markup=build_task_keyboard(tasks, key),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Ошибка напоминания для {user_id_str}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Установи переменную окружения BOT_TOKEN")

    app = Application.builder().token(token).build()

    plan_conv = ConversationHandler(
        entry_points=[CommandHandler("plan", cmd_plan)],
        states={ADDING_TASKS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_task),
            CommandHandler("done", done_adding),
        ]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    rev_conv = ConversationHandler(
        entry_points=[CommandHandler("revenue", cmd_revenue)],
        states={
            ADDING_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_revenue)],
            ADDING_NET:     [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_net)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("today",   cmd_today))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(plan_conv)
    app.add_handler(rev_conv)
    app.add_handler(CallbackQueryHandler(callback_handler))

    moscow = pytz.timezone("Europe/Moscow")
    app.job_queue.run_daily(
        morning_reminder,
        time=datetime.time(hour=9, minute=0, tzinfo=moscow),
        name="morning_reminder"
    )

    print("🤖 Бот запущен (Bothost)...")
    app.run_polling()


if __name__ == "__main__":
    main()
