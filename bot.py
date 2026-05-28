import json
import os
import datetime
from datetime import date, timedelta
from pathlib import Path
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ── Хранилище ─────────────────────────────────────────────────────────────────
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

def tomorrow_key() -> str:
    return get_day_key(date.today() + timedelta(days=1))

def get_user(data: dict, user_id: int) -> dict:
    key = str(user_id)
    if key not in data:
        data[key] = {"days": {}, "finance_goal": None}
    if "finance_goal" not in data[key]:
        data[key]["finance_goal"] = None
    return data[key]

# ── Постоянная Reply-клавиатура ────────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📋 Сегодня"),      KeyboardButton("📅 Завтра")],
        [KeyboardButton("✏️ План сегодня"), KeyboardButton("✏️ План завтра")],
        [KeyboardButton("💰 Выручка"),      KeyboardButton("📊 Отчёт")],
        [KeyboardButton("🎯 Цель"),         KeyboardButton("📈 История")],
        [KeyboardButton("🗑 Сброс задач сегодня"), KeyboardButton("🗑 Сброс задач завтра")],
        [KeyboardButton("💸 Сброс финансов")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# ── Состояния диалогов ─────────────────────────────────────────────────────────
(
    PLAN_TODAY, PLAN_TOMORROW,
    ADDING_REVENUE, ADDING_NET,
    GOAL_AMOUNT, GOAL_DATE,
) = range(6)

# ─────────────────────────────────────────────────────────────────────────────
# ФОРМАТИРОВАНИЕ
# ─────────────────────────────────────────────────────────────────────────────

def fmt_finance_goal(goal: dict) -> str:
    if not goal:
        return ""
    target    = goal.get("amount", 0)
    end_str   = goal.get("end_date", "")
    actual    = goal.get("actual", 0)
    pct       = round(actual / target * 100) if target else 0
    bar_filled = round(pct / 10)
    bar        = "█" * bar_filled + "░" * (10 - bar_filled)
    return (
        f"\n💎 *Финансовая цель:*\n"
        f"Цель: *{target:,.0f}* до *{end_str}*\n"
        f"Факт: *{actual:,.0f}* ({pct}%)\n"
        f"`[{bar}]`\n"
    )

def fmt_day_report(day_data: dict, day_key: str, goal: dict = None) -> str:
    revenue  = day_data.get("revenue", 0)
    net      = day_data.get("net", 0)
    tasks    = day_data.get("tasks", [])
    prev_rev = day_data.get("period_revenue", revenue)
    prev_net = day_data.get("period_net", net)

    done  = sum(1 for t in tasks if t.get("done"))
    total = len(tasks)
    pct   = round(done / total * 100) if total else 0

    lines = [f"📅 *{day_key}*\n"]
    if goal:
        lines.append(fmt_finance_goal(goal).strip() + "\n")
    lines += [
        f"Факт по выручке: *{revenue:,}*",
        f"Факт по чистым: *{net:,}*",
        f"Итого за период (выручка): *{prev_rev:,}*",
        f"Итого за период (чистые): *{prev_net:,}*",
        f"Прогресс задач: *{pct}%* ({done}/{total})\n",
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
        label = f"{mark} {i+1}. {t['text'][:28]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"toggle:{day_key}:{i}")])
    rows.append([InlineKeyboardButton("💾 Сохранить отчёт", callback_data=f"save:{day_key}")])
    return InlineKeyboardMarkup(rows)

# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я твой ежедневный планировщик.\n"
        "Используй кнопки внизу экрана 👇",
        reply_markup=MAIN_KB
    )


async def setup_commands(app: Application):
    """Регистрирует команды в меню Telegram (кнопка / у поля ввода)."""
    commands = [
        BotCommand("start",         "Главное меню"),
        BotCommand("plantoday",     "Добавить задачи на сегодня"),
        BotCommand("plan",          "Добавить задачи на завтра"),
        BotCommand("today",         "Задачи на сегодня"),
        BotCommand("tomorrow",      "Задачи на завтра"),
        BotCommand("revenue",       "Внести выручку за сегодня"),
        BotCommand("goal",          "Установить финансовую цель"),
        BotCommand("report",        "Отчёт за сегодня"),
        BotCommand("history",       "История за 7 дней"),
        BotCommand("resettoday",    "Очистить задачи на сегодня"),
        BotCommand("resettomorrow", "Очистить задачи на завтра"),
        BotCommand("resetfinance",  "Обнулить финансы и цель"),
        BotCommand("cancel",        "Отменить текущее действие"),
    ]
    await app.bot.set_my_commands(commands)

# ─────────────────────────────────────────────────────────────────────────────
# ДОБАВЛЕНИЕ ЗАДАЧ — общая логика
# ─────────────────────────────────────────────────────────────────────────────

async def receive_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return ctx.user_data.get("plan_state", PLAN_TOMORROW)
    ctx.user_data["plan_tasks"].append({"text": text, "done": False})
    count = len(ctx.user_data["plan_tasks"])
    await update.message.reply_text(f"✔️ Задача {count}: «{text}»")
    return ctx.user_data.get("plan_state", PLAN_TOMORROW)


async def done_adding(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tasks = ctx.user_data.get("plan_tasks", [])
    key   = ctx.user_data.get("plan_key")
    label = ctx.user_data.get("plan_label", key)

    if not tasks:
        await update.message.reply_text("Ты не добавил ни одной задачи.")
        return ConversationHandler.END

    data = load_data()
    user = get_user(data, update.effective_user.id)
    day  = user["days"].setdefault(key, {})
    day["tasks"] = tasks
    save_data(data)

    lines = [f"*План на {label}:*"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t['text']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# /plantoday — план на СЕГОДНЯ
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_plan_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = get_day_key()
    ctx.user_data["plan_key"]   = key
    ctx.user_data["plan_label"] = "сегодня"
    ctx.user_data["plan_tasks"] = []
    ctx.user_data["plan_state"] = PLAN_TODAY
    await update.message.reply_text(
        f"📝 Введи задачи на *сегодня* ({key}) — по одной.\n"
        "Когда закончишь — /done",
        parse_mode="Markdown"
    )
    return PLAN_TODAY

# ─────────────────────────────────────────────────────────────────────────────
# /plan — план на ЗАВТРА
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = tomorrow_key()
    ctx.user_data["plan_key"]   = key
    ctx.user_data["plan_label"] = "завтра"
    ctx.user_data["plan_tasks"] = []
    ctx.user_data["plan_state"] = PLAN_TOMORROW
    await update.message.reply_text(
        f"📝 Введи задачи на *завтра* ({key}) — по одной.\n"
        "Когда закончишь — /done",
        parse_mode="Markdown"
    )
    return PLAN_TOMORROW

# ─────────────────────────────────────────────────────────────────────────────
# /today — задачи на сегодня
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    day  = user["days"].get(key)

    if not day or not day.get("tasks"):
        await update.message.reply_text(
            f"На сегодня ({key}) нет задач.\n"
            "Добавь через /plantoday"
        )
        return

    goal = user.get("finance_goal")
    text = fmt_day_report(day, key, goal)
    kb   = build_task_keyboard(day["tasks"], key)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# /tomorrow — задачи на завтра (просмотр)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = tomorrow_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    day  = user["days"].get(key)

    if not day or not day.get("tasks"):
        await update.message.reply_text(
            f"На завтра ({key}) пока нет задач.\n"
            "Добавь через /plan"
        )
        return

    tasks = day["tasks"]
    lines = [f"📋 *План на завтра ({key}):*"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t['text']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# /resettoday / /resettomorrow — обнуление задач
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_reset_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    if key in user["days"]:
        user["days"][key]["tasks"] = []
        save_data(data)
    await update.message.reply_text(
        f"🗑 Задачи на сегодня ({key}) очищены.\n"
        "Добавь новые через /plantoday"
    )


async def cmd_reset_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = tomorrow_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    if key in user["days"]:
        user["days"][key]["tasks"] = []
        save_data(data)
    await update.message.reply_text(
        f"🗑 Задачи на завтра ({key}) очищены.\n"
        "Добавь новые через /plan"
    )

# ─────────────────────────────────────────────────────────────────────────────
# Callback: toggle / save
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
        goal = user.get("finance_goal")
        await query.edit_message_text(
            fmt_day_report(day, day_key, goal),
            reply_markup=build_task_keyboard(day["tasks"], day_key),
            parse_mode="Markdown"
        )

    elif query.data.startswith("save:"):
        _, day_key = query.data.split(":")
        day = user["days"].get(day_key)
        if not day:
            return
        goal = user.get("finance_goal")
        await query.edit_message_text(
            fmt_day_report(day, day_key, goal) + "\n\n✅ *Отчёт сохранён!*",
            parse_mode="Markdown"
        )

# ─────────────────────────────────────────────────────────────────────────────
# /revenue — ввести выручку и чистые
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_revenue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Введи *факт по выручке* за сегодня:", parse_mode="Markdown")
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

    # Период — сумма по всем дням, кроме сегодня (чтоб не задвоить)
    other_days = {k: v for k, v in user["days"].items() if k != key}
    total_rev  = sum(d.get("revenue", 0) for d in other_days.values()) + revenue
    total_net  = sum(d.get("net",     0) for d in other_days.values()) + val

    day = user["days"].setdefault(key, {})
    day["revenue"]        = revenue
    day["net"]            = val
    day["period_revenue"] = total_rev
    day["period_net"]     = total_net

    # Обновляем прогресс финансовой цели
    goal = user.get("finance_goal")
    if goal:
        goal["actual"] = total_rev
        user["finance_goal"] = goal

    save_data(data)

    reply = (
        f"✅ Записано!\n"
        f"Выручка: *{revenue:,}* | Чистые: *{val:,}*\n"
        f"Итого за период: выручка *{total_rev:,}* / чистые *{total_net:,}*"
    )
    if goal:
        reply += "\n" + fmt_finance_goal(goal)
    await update.message.reply_text(reply, parse_mode="Markdown")
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# /goal — финансовая цель (сумма + срок)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 Введи *целевую сумму* (выручка за период), например: *1000000*",
        parse_mode="Markdown"
    )
    return GOAL_AMOUNT


async def receive_goal_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("Введи число, например: 1000000")
        return GOAL_AMOUNT
    ctx.user_data["goal_amount"] = val
    await update.message.reply_text(
        "📅 Теперь введи *дату окончания* в формате ДД.ММ.ГГ\n"
        "Например: *31.05.25*",
        parse_mode="Markdown"
    )
    return GOAL_DATE


async def receive_goal_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        datetime.datetime.strptime(text, "%d.%m.%y")
    except ValueError:
        await update.message.reply_text("Неверный формат. Введи дату как ДД.ММ.ГГ, например: 31.05.25")
        return GOAL_DATE

    uid    = update.effective_user.id
    amount = ctx.user_data.get("goal_amount", 0)
    data   = load_data()
    user   = get_user(data, uid)

    # Текущий накопленный факт выручки
    current = sum(d.get("revenue", 0) for d in user["days"].values())

    user["finance_goal"] = {
        "amount":   amount,
        "end_date": text,
        "actual":   current,
    }
    save_data(data)

    await update.message.reply_text(
        f"✅ Цель установлена!\n"
        f"Цель: *{amount:,.0f}* до *{text}*\n"
        f"Текущий факт: *{current:,.0f}*\n\n"
        + fmt_finance_goal(user["finance_goal"]),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# /resetfinance — обнулить доходы и цель
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_reset_finance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    data = load_data()
    user = get_user(data, uid)

    # Обнуляем финансовые поля во всех днях
    for day in user["days"].values():
        day["revenue"]        = 0
        day["net"]            = 0
        day["period_revenue"] = 0
        day["period_net"]     = 0

    user["finance_goal"] = None
    save_data(data)

    await update.message.reply_text(
        "🗑 Все финансовые данные и цель обнулены.\n"
        "Установи новую цель через /goal"
    )

# ─────────────────────────────────────────────────────────────────────────────
# /report — отчёт за сегодня
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    day  = user["days"].get(key)
    if not day:
        await update.message.reply_text("Данных за сегодня нет.")
        return
    goal = user.get("finance_goal")
    await update.message.reply_text(fmt_day_report(day, key, goal), parse_mode="Markdown")

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
            f"📅 *{k}* — задачи {pct}% ({done}/{total}) | "
            f"Выручка: {rev:,} | Чистые: {net:,}"
        )

    goal = user.get("finance_goal")
    header = ""
    if goal:
        header = fmt_finance_goal(goal) + "\n"

    await update.message.reply_text(header + "\n".join(parts), parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# Обработчик кнопок Reply-клавиатуры
# ─────────────────────────────────────────────────────────────────────────────

BUTTON_MAP = {
    "📋 Сегодня":            "today",
    "📅 Завтра":             "tomorrow",
    "✏️ План сегодня":      "plantoday",
    "✏️ План завтра":       "plan",
    "💰 Выручка":           "revenue",
    "📊 Отчёт":             "report",
    "🎯 Цель":              "goal",
    "📈 История":           "history",
    "🗑 Сброс задач сегодня": "resettoday",
    "🗑 Сброс задач завтра":  "resettomorrow",
    "💸 Сброс финансов":    "resetfinance",
}

BUTTON_HANDLERS = {
    "today":         cmd_today,
    "tomorrow":      cmd_tomorrow,
    "report":        cmd_report,
    "history":       cmd_history,
    "resettoday":    cmd_reset_today,
    "resettomorrow": cmd_reset_tomorrow,
    "resetfinance":  cmd_reset_finance,
}

async def handle_reply_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text
    command = BUTTON_MAP.get(text)
    if not command:
        return

    # Команды без диалога — вызываем напрямую
    if command in BUTTON_HANDLERS:
        await BUTTON_HANDLERS[command](update, ctx)
        return

    # Команды с диалогом — имитируем команду через фейковый update
    # Для plantoday / plan / revenue / goal просто вызываем entry-point
    if command == "plantoday":
        await cmd_plan_today(update, ctx)
    elif command == "plan":
        await cmd_plan(update, ctx)
    elif command == "revenue":
        await cmd_revenue(update, ctx)
    elif command == "goal":
        await cmd_goal(update, ctx)

# ─────────────────────────────────────────────────────────────────────────────
# Утреннее напоминание
# ─────────────────────────────────────────────────────────────────────────────

async def morning_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    data = load_data()
    for user_id_str, user_data in data.items():
        day = user_data.get("days", {}).get(key)
        if day and day.get("tasks"):
            tasks = day["tasks"]
            text  = f"☀️ *Доброе утро! План на {key}:*\n"
            for i, t in enumerate(tasks, 1):
                text += f"\n{i}. {t['text']}"
            goal = user_data.get("finance_goal")
            if goal:
                text += "\n" + fmt_finance_goal(goal)
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

    app = Application.builder().token(token).post_init(setup_commands).build()

    plan_today_conv = ConversationHandler(
        entry_points=[CommandHandler("plantoday", cmd_plan_today)],
        states={PLAN_TODAY: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_task),
            CommandHandler("done", done_adding),
        ]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    plan_conv = ConversationHandler(
        entry_points=[CommandHandler("plan", cmd_plan)],
        states={PLAN_TOMORROW: [
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

    goal_conv = ConversationHandler(
        entry_points=[CommandHandler("goal", cmd_goal)],
        states={
            GOAL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_goal_amount)],
            GOAL_DATE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_goal_date)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("today",         cmd_today))
    app.add_handler(CommandHandler("tomorrow",      cmd_tomorrow))
    app.add_handler(CommandHandler("resettoday",    cmd_reset_today))
    app.add_handler(CommandHandler("resettomorrow", cmd_reset_tomorrow))
    app.add_handler(CommandHandler("report",        cmd_report))
    app.add_handler(CommandHandler("history",       cmd_history))
    app.add_handler(CommandHandler("resetfinance",  cmd_reset_finance))
    app.add_handler(plan_today_conv)
    app.add_handler(plan_conv)
    app.add_handler(rev_conv)
    app.add_handler(goal_conv)
    app.add_handler(CallbackQueryHandler(callback_handler))
    # Кнопки Reply-клавиатуры — регистрируем последними, низкий приоритет
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(
            "^(" + "|".join(map(lambda s: s.replace(".", r"\."), BUTTON_MAP.keys())) + ")$"
        ),
        handle_reply_button,
        block=False,
    ))

    moscow = pytz.timezone("Europe/Moscow")
    app.job_queue.run_daily(
        morning_reminder,
        time=datetime.time(hour=9, minute=0, tzinfo=moscow),
        name="morning_reminder"
    )

    print("🤖 Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
