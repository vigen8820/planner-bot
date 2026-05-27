import json
import os
import datetime
from datetime import date, timedelta
import pytz

from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ── Supabase client ────────────────────────────────────────────────────────────
def get_supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)

# ── DB helpers ─────────────────────────────────────────────────────────────────

def load_data() -> dict:
    """Load all rows from 'planner' table into the same dict structure as before."""
    sb = get_supabase()
    rows = sb.table("planner").select("*").execute().data
    result = {}
    for row in rows:
        uid = str(row["user_id"])
        if uid not in result:
            result[uid] = {"days": {}}
        result[uid]["days"][row["day_key"]] = row["day_data"]
    return result

def load_user(user_id: int) -> dict:
    """Load a single user's data."""
    sb = get_supabase()
    rows = sb.table("planner").select("*").eq("user_id", user_id).execute().data
    user = {"days": {}}
    for row in rows:
        user["days"][row["day_key"]] = row["day_data"]
    return user

def save_day(user_id: int, day_key: str, day_data: dict):
    """Upsert one day's record for a user."""
    sb = get_supabase()
    sb.table("planner").upsert({
        "user_id": user_id,
        "day_key": day_key,
        "day_data": day_data,
    }, on_conflict="user_id,day_key").execute()

def get_day_key(d: date = None) -> str:
    return (d or date.today()).strftime("%d.%m.%y")

# ── Conversation states ────────────────────────────────────────────────────────
ADDING_TASKS, ADDING_REVENUE, ADDING_NET = range(3)

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
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
    text = (
        "👋 Привет! Я твой ежедневный планировщик.\n\n"
        "Команды:\n"
        "  /plan — добавить план на завтра\n"
        "  /today — открыть задачи на сегодня\n"
        "  /revenue — ввести выручку за сегодня\n"
        "  /report — итоговый отчёт за сегодня\n"
        "  /history — история за последние 7 дней\n"
    )
    await update.message.reply_text(text)

# ─────────────────────────────────────────────────────────────────────────────
# /plan
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

    uid = update.effective_user.id
    # Load existing day data (may already have revenue etc.)
    user = load_user(uid)
    day  = user["days"].get(key, {})
    day["tasks"] = tasks
    save_day(uid, key, day)

    lines = [f"*План на {key}:*"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t['text']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# /today
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    user = load_user(update.effective_user.id)
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
# Callback: toggle / save
# ─────────────────────────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "noop":
        return

    uid  = query.from_user.id
    user = load_user(uid)

    if query.data.startswith("toggle:"):
        _, day_key, idx_str = query.data.split(":")
        idx = int(idx_str)
        day = user["days"].get(day_key)
        if not day:
            return
        day["tasks"][idx]["done"] = not day["tasks"][idx]["done"]
        save_day(uid, day_key, day)
        text = fmt_day_report(day, day_key)
        kb   = build_task_keyboard(day["tasks"], day_key)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

    elif query.data.startswith("save:"):
        _, day_key = query.data.split(":")
        day = user["days"].get(day_key)
        if not day:
            return
        text = fmt_day_report(day, day_key)
        await query.edit_message_text(
            text + "\n\n✅ *Отчёт сохранён!*",
            parse_mode="Markdown"
        )

# ─────────────────────────────────────────────────────────────────────────────
# /revenue
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
    user    = load_user(uid)

    # Period totals across all days
    all_days = user["days"]
    total_rev = sum(d.get("revenue", 0) for d in all_days.values()) + revenue
    total_net = sum(d.get("net", 0)     for d in all_days.values()) + val

    day = all_days.get(key, {})
    day["revenue"]        = revenue
    day["net"]            = val
    day["period_revenue"] = total_rev
    day["period_net"]     = total_net
    save_day(uid, key, day)

    await update.message.reply_text(
        f"✅ Записано!\nВыручка: *{revenue:,}* | Чистые: *{val:,}*\n"
        f"Итого за период: выручка *{total_rev:,}* / чистые *{total_net:,}*",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# /report
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    user = load_user(update.effective_user.id)
    day  = user["days"].get(key)
    if not day:
        await update.message.reply_text("Данных за сегодня нет.")
        return
    await update.message.reply_text(fmt_day_report(day, key), parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# /history
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = load_user(update.effective_user.id)
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
# Morning reminder
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
            kb = build_task_keyboard(tasks, key)
            try:
                await ctx.bot.send_message(
                    chat_id=int(user_id_str),
                    text=text,
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Reminder error for {user_id_str}: {e}")

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

    print("🤖 Бот запущен (Supabase)...")
    app.run_polling()


if __name__ == "__main__":
    main()
