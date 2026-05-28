import json
import os
import datetime
from datetime import date, timedelta
from pathlib import Path
import pytz

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
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

# ── Состояния (хранятся в ctx.user_data["mode"]) ──────────────────────────────
MODE_PLAN_TODAY    = "plan_today"
MODE_PLAN_TOMORROW = "plan_tomorrow"
MODE_REVENUE       = "revenue"
MODE_NET           = "net"
MODE_GOAL_AMOUNT   = "goal_amount"
MODE_GOAL_DATE     = "goal_date"

def clear_mode(ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("mode", None)
    ctx.user_data.pop("plan_key", None)
    ctx.user_data.pop("plan_label", None)
    ctx.user_data.pop("plan_tasks", None)
    ctx.user_data.pop("revenue_val", None)
    ctx.user_data.pop("goal_amount_val", None)

# ── Reply-клавиатура ───────────────────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📋 Сегодня"),           KeyboardButton("📅 Завтра")],
        [KeyboardButton("✏️ План сегодня"),      KeyboardButton("✏️ План завтра")],
        [KeyboardButton("💰 Выручка"),           KeyboardButton("📊 Отчёт")],
        [KeyboardButton("🎯 Цель"),              KeyboardButton("📈 История")],
        [KeyboardButton("🗑 Сброс сегодня"),     KeyboardButton("🗑 Сброс завтра")],
        [KeyboardButton("💸 Сброс финансов")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# ── Кнопки клавиатуры (тексты) ────────────────────────────────────────────────
BTN_TODAY        = "📋 Сегодня"
BTN_TOMORROW     = "📅 Завтра"
BTN_PLAN_TODAY   = "✏️ План сегодня"
BTN_PLAN_TMR     = "✏️ План завтра"
BTN_REVENUE      = "💰 Выручка"
BTN_REPORT       = "📊 Отчёт"
BTN_GOAL         = "🎯 Цель"
BTN_HISTORY      = "📈 История"
BTN_RESET_TODAY  = "🗑 Сброс сегодня"
BTN_RESET_TMR    = "🗑 Сброс завтра"
BTN_RESET_FIN    = "💸 Сброс финансов"

ALL_BUTTONS = {
    BTN_TODAY, BTN_TOMORROW, BTN_PLAN_TODAY, BTN_PLAN_TMR,
    BTN_REVENUE, BTN_REPORT, BTN_GOAL, BTN_HISTORY,
    BTN_RESET_TODAY, BTN_RESET_TMR, BTN_RESET_FIN,
}

# ─────────────────────────────────────────────────────────────────────────────
# ФОРМАТИРОВАНИЕ
# ─────────────────────────────────────────────────────────────────────────────

def fmt_finance_goal(goal: dict) -> str:
    if not goal:
        return ""
    target     = goal.get("amount", 0)
    end_str    = goal.get("end_date", "")
    actual     = goal.get("actual", 0)
    pct        = round(actual / target * 100) if target else 0
    bar_filled = min(round(pct / 10), 10)
    bar        = "█" * bar_filled + "░" * (10 - bar_filled)
    return (
        f"💎 *Финансовая цель:*\n"
        f"Цель: *{target:,.0f}* до *{end_str}*\n"
        f"Факт: *{actual:,.0f}* ({pct}%)\n"
        f"`[{bar}]`"
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
        lines.append(fmt_finance_goal(goal) + "\n")
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
# SETUP
# ─────────────────────────────────────────────────────────────────────────────

async def setup_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",         "Главное меню / показать кнопки"),
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
    ])

# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_mode(ctx)
    await update.message.reply_text(
        "👋 Привет! Я твой ежедневный планировщик.\n"
        "Используй кнопки внизу 👇",
        reply_markup=MAIN_KB
    )

# ─────────────────────────────────────────────────────────────────────────────
# ПЛАН — вход
# ─────────────────────────────────────────────────────────────────────────────

async def start_plan_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = get_day_key()
    ctx.user_data["mode"]       = MODE_PLAN_TODAY
    ctx.user_data["plan_key"]   = key
    ctx.user_data["plan_label"] = "сегодня"
    # Загружаем уже существующие задачи, чтобы добавлять к ним
    data  = load_data()
    user  = get_user(data, update.effective_user.id)
    existing = user["days"].get(key, {}).get("tasks", [])
    ctx.user_data["plan_tasks"] = list(existing)
    hint = f" (уже есть {len(existing)} задач)" if existing else ""
    await update.message.reply_text(
        f"📝 Добавляю задачи на *сегодня* ({key}){hint}.\n"
        "Пиши по одной задаче. Когда закончишь — нажми /done или /cancel",
        parse_mode="Markdown",
        reply_markup=MAIN_KB
    )

async def start_plan_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key = tomorrow_key()
    ctx.user_data["mode"]       = MODE_PLAN_TOMORROW
    ctx.user_data["plan_key"]   = key
    ctx.user_data["plan_label"] = "завтра"
    data  = load_data()
    user  = get_user(data, update.effective_user.id)
    existing = user["days"].get(key, {}).get("tasks", [])
    ctx.user_data["plan_tasks"] = list(existing)
    hint = f" (уже есть {len(existing)} задач)" if existing else ""
    await update.message.reply_text(
        f"📝 Добавляю задачи на *завтра* ({key}){hint}.\n"
        "Пиши по одной задаче. Когда закончишь — /done или /cancel",
        parse_mode="Markdown",
        reply_markup=MAIN_KB
    )

# ─────────────────────────────────────────────────────────────────────────────
# /done — завершить добавление задач
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mode = ctx.user_data.get("mode")
    if mode not in (MODE_PLAN_TODAY, MODE_PLAN_TOMORROW):
        await update.message.reply_text("Нечего завершать.", reply_markup=MAIN_KB)
        return

    tasks = ctx.user_data.get("plan_tasks", [])
    key   = ctx.user_data["plan_key"]
    label = ctx.user_data["plan_label"]

    if not tasks:
        await update.message.reply_text("Ты не добавил ни одной задачи.", reply_markup=MAIN_KB)
        clear_mode(ctx)
        return

    data = load_data()
    user = get_user(data, update.effective_user.id)
    user["days"].setdefault(key, {})["tasks"] = tasks
    save_data(data)

    lines = [f"✅ *План на {label} ({key}):*"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t['text']}")
    clear_mode(ctx)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_KB)

# ─────────────────────────────────────────────────────────────────────────────
# /cancel
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_mode(ctx)
    await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)

# ─────────────────────────────────────────────────────────────────────────────
# /today, /tomorrow
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    day  = user["days"].get(key)
    if not day or not day.get("tasks"):
        await update.message.reply_text(
            f"На сегодня ({key}) нет задач. Добавь через «✏️ План сегодня»",
            reply_markup=MAIN_KB
        )
        return
    goal = user.get("finance_goal")
    await update.message.reply_text(
        fmt_day_report(day, key, goal),
        reply_markup=build_task_keyboard(day["tasks"], key),
        parse_mode="Markdown"
    )

async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = tomorrow_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    day  = user["days"].get(key)
    if not day or not day.get("tasks"):
        await update.message.reply_text(
            f"На завтра ({key}) нет задач. Добавь через «✏️ План завтра»",
            reply_markup=MAIN_KB
        )
        return
    tasks = day["tasks"]
    lines = [f"📋 *План на завтра ({key}):*"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t['text']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_KB)

# ─────────────────────────────────────────────────────────────────────────────
# Сброс задач
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_reset_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    if key in user["days"]:
        user["days"][key]["tasks"] = []
        save_data(data)
    await update.message.reply_text(
        f"🗑 Задачи на сегодня ({key}) очищены.", reply_markup=MAIN_KB
    )

async def cmd_reset_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = tomorrow_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    if key in user["days"]:
        user["days"][key]["tasks"] = []
        save_data(data)
    await update.message.reply_text(
        f"🗑 Задачи на завтра ({key}) очищены.", reply_markup=MAIN_KB
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
        day["tasks"][int(idx_str)]["done"] = not day["tasks"][int(idx_str)]["done"]
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
# ВЫРУЧКА
# ─────────────────────────────────────────────────────────────────────────────

async def start_revenue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["mode"] = MODE_REVENUE
    await update.message.reply_text(
        "💰 Введи *факт по выручке* за сегодня:", parse_mode="Markdown", reply_markup=MAIN_KB
    )

# ─────────────────────────────────────────────────────────────────────────────
# ФИНАНСОВАЯ ЦЕЛЬ
# ─────────────────────────────────────────────────────────────────────────────

async def start_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["mode"] = MODE_GOAL_AMOUNT
    await update.message.reply_text(
        "🎯 Введи *целевую сумму* (выручка за период), например: *1000000*",
        parse_mode="Markdown", reply_markup=MAIN_KB
    )

async def cmd_reset_finance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    data = load_data()
    user = get_user(data, uid)
    for day in user["days"].values():
        day["revenue"] = 0; day["net"] = 0
        day["period_revenue"] = 0; day["period_net"] = 0
    user["finance_goal"] = None
    save_data(data)
    await update.message.reply_text(
        "🗑 Финансовые данные и цель обнулены.", reply_markup=MAIN_KB
    )

# ─────────────────────────────────────────────────────────────────────────────
# /report, /history
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    key  = get_day_key()
    data = load_data()
    user = get_user(data, update.effective_user.id)
    day  = user["days"].get(key)
    if not day:
        await update.message.reply_text("Данных за сегодня нет.", reply_markup=MAIN_KB)
        return
    goal = user.get("finance_goal")
    await update.message.reply_text(fmt_day_report(day, key, goal), parse_mode="Markdown", reply_markup=MAIN_KB)

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    days = user["days"]
    if not days:
        await update.message.reply_text("История пуста.", reply_markup=MAIN_KB)
        return
    keys  = sorted(days.keys(), reverse=True)[:7]
    parts = []
    for k in keys:
        day   = days[k]
        tasks = day.get("tasks", [])
        done  = sum(1 for t in tasks if t.get("done"))
        total = len(tasks)
        pct   = round(done / total * 100) if total else 0
        parts.append(
            f"📅 *{k}* — {pct}% ({done}/{total}) | "
            f"Выручка: {day.get('revenue',0):,} | Чистые: {day.get('net',0):,}"
        )
    goal   = user.get("finance_goal")
    header = fmt_finance_goal(goal) + "\n\n" if goal else ""
    await update.message.reply_text(header + "\n".join(parts), parse_mode="Markdown", reply_markup=MAIN_KB)

# ─────────────────────────────────────────────────────────────────────────────
# ЕДИНЫЙ ОБРАБОТЧИК ТЕКСТА — ловит ввод во всех режимах
# ─────────────────────────────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mode = ctx.user_data.get("mode")
    uid  = update.effective_user.id

    # ── Кнопки меню — обрабатываем всегда, даже в режиме ввода ──────────────
    if text == BTN_TODAY:
        await cmd_today(update, ctx); return
    if text == BTN_TOMORROW:
        await cmd_tomorrow(update, ctx); return
    if text == BTN_PLAN_TODAY:
        await start_plan_today(update, ctx); return
    if text == BTN_PLAN_TMR:
        await start_plan_tomorrow(update, ctx); return
    if text == BTN_REVENUE:
        await start_revenue(update, ctx); return
    if text == BTN_REPORT:
        await cmd_report(update, ctx); return
    if text == BTN_GOAL:
        await start_goal(update, ctx); return
    if text == BTN_HISTORY:
        await cmd_history(update, ctx); return
    if text == BTN_RESET_TODAY:
        await cmd_reset_today(update, ctx); return
    if text == BTN_RESET_TMR:
        await cmd_reset_tomorrow(update, ctx); return
    if text == BTN_RESET_FIN:
        await cmd_reset_finance(update, ctx); return

    # ── Режим добавления задач ───────────────────────────────────────────────
    if mode in (MODE_PLAN_TODAY, MODE_PLAN_TOMORROW):
        if not text:
            return
        ctx.user_data["plan_tasks"].append({"text": text, "done": False})
        count = len(ctx.user_data["plan_tasks"])
        await update.message.reply_text(
            f"✔️ Задача {count}: «{text}»\n_Ещё задачу или /done_",
            parse_mode="Markdown"
        )
        return

    # ── Режим ввода выручки ──────────────────────────────────────────────────
    if mode == MODE_REVENUE:
        try:
            val = float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            await update.message.reply_text("Введи число, например: 150000")
            return
        ctx.user_data["revenue_val"] = val
        ctx.user_data["mode"] = MODE_NET
        await update.message.reply_text(
            "💵 Теперь введи *факт по чистым*:", parse_mode="Markdown"
        )
        return

    if mode == MODE_NET:
        try:
            val = float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            await update.message.reply_text("Введи число, например: 50000")
            return
        key     = get_day_key()
        revenue = ctx.user_data.get("revenue_val", 0)
        data    = load_data()
        user    = get_user(data, uid)
        other   = {k: v for k, v in user["days"].items() if k != key}
        total_rev = sum(d.get("revenue", 0) for d in other.values()) + revenue
        total_net = sum(d.get("net",     0) for d in other.values()) + val
        day = user["days"].setdefault(key, {})
        day["revenue"]        = revenue
        day["net"]            = val
        day["period_revenue"] = total_rev
        day["period_net"]     = total_net
        goal = user.get("finance_goal")
        if goal:
            goal["actual"] = total_rev
        save_data(data)
        clear_mode(ctx)
        reply = (
            f"✅ Записано!\n"
            f"Выручка: *{revenue:,}* | Чистые: *{val:,}*\n"
            f"Итого за период: выручка *{total_rev:,}* / чистые *{total_net:,}*"
        )
        if goal:
            reply += "\n\n" + fmt_finance_goal(goal)
        await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=MAIN_KB)
        return

    # ── Режим ввода цели ─────────────────────────────────────────────────────
    if mode == MODE_GOAL_AMOUNT:
        try:
            val = float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            await update.message.reply_text("Введи число, например: 1000000")
            return
        ctx.user_data["goal_amount_val"] = val
        ctx.user_data["mode"] = MODE_GOAL_DATE
        await update.message.reply_text(
            "📅 Введи *дату окончания* в формате ДД.ММ.ГГ, например: *31.05.25*",
            parse_mode="Markdown"
        )
        return

    if mode == MODE_GOAL_DATE:
        try:
            datetime.datetime.strptime(text, "%d.%m.%y")
        except ValueError:
            await update.message.reply_text("Неверный формат. Пример: 31.05.25")
            return
        amount  = ctx.user_data.get("goal_amount_val", 0)
        data    = load_data()
        user    = get_user(data, uid)
        current = sum(d.get("revenue", 0) for d in user["days"].values())
        user["finance_goal"] = {"amount": amount, "end_date": text, "actual": current}
        save_data(data)
        clear_mode(ctx)
        await update.message.reply_text(
            f"✅ Цель установлена!\n\n" + fmt_finance_goal(user["finance_goal"]),
            parse_mode="Markdown", reply_markup=MAIN_KB
        )
        return

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
                text += "\n\n" + fmt_finance_goal(goal)
            try:
                await ctx.bot.send_message(
                    chat_id=int(user_id_str),
                    text=text,
                    reply_markup=build_task_keyboard(tasks, key),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Ошибка напоминания {user_id_str}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Установи переменную окружения BOT_TOKEN")

    app = Application.builder().token(token).post_init(setup_commands).build()

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("plantoday",     start_plan_today))
    app.add_handler(CommandHandler("plan",          start_plan_tomorrow))
    app.add_handler(CommandHandler("done",          cmd_done))
    app.add_handler(CommandHandler("cancel",        cmd_cancel))
    app.add_handler(CommandHandler("today",         cmd_today))
    app.add_handler(CommandHandler("tomorrow",      cmd_tomorrow))
    app.add_handler(CommandHandler("revenue",       start_revenue))
    app.add_handler(CommandHandler("goal",          start_goal))
    app.add_handler(CommandHandler("report",        cmd_report))
    app.add_handler(CommandHandler("history",       cmd_history))
    app.add_handler(CommandHandler("resettoday",    cmd_reset_today))
    app.add_handler(CommandHandler("resettomorrow", cmd_reset_tomorrow))
    app.add_handler(CommandHandler("resetfinance",  cmd_reset_finance))
    app.add_handler(CallbackQueryHandler(callback_handler))
    # Единый обработчик всего текста — последним
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

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
