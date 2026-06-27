import asyncio
import logging
import json
import os
from datetime import datetime, timedelta, date
from typing import Optional, Dict, Any

import ollama
from fastapi import FastAPI
from sqlalchemy.orm import Session
from sqlalchemy import select
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import init_db, SessionLocal, engine, User, Log, Reminder
import bot_tools
from data_processor import generate_weekly_report

# Configuration
# Read sensitive config from environment to avoid committing secrets.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
OLLAMA_MODEL = "gemma4"
PROACTIVE_HOURS = 6
DAILY_RECAP_HOUR_UTC = 20

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Nutri Omnipresent Nutritionist", version="1.0.0")
bot_app = None
scheduler = AsyncIOScheduler(timezone="UTC")

TOOL_INSTRUCTIONS = (
    "You are a professional, friendly, and empathetic nutritionist."
    " Use the available tools whenever the user asks to log food or activity, update goals, or query summaries."
    " When you decide a tool must be called, return only valid JSON with keys 'tool' and 'args'."
    " Example: {\"tool\": \"log_food\", \"args\": {\"description\": \"salad\", \"calories\": 180}}."
    " If no tool is needed, answer naturally without JSON."
    " The supported tools are:"
    " log_food(description, calories, protein, carbs, fat, amount, unit),"
    " log_activity(description, calories_burned, duration, unit),"
    " get_daily_summary(date),"
    " update_user_goals(calorie_goal, protein_goal, carb_goal, fat_goal),"
    " get_user_profile(),"
    " query_logs(start_date, end_date),"
    " set_reminder(trigger_time, message)."
)

SYSTEM_PROMPT = (
    "You are an omnipresent nutritionist who is caring, practical, and supportive. "
    "You know when to call tools to log nutrition, fetch summaries, or update user plans. "
    "If you call a tool, the response must be pure JSON with fields tool and args. "
    "If you do not need a tool, answer conversationally. "
    + TOOL_INSTRUCTIONS
)


async def chat_with_llm(messages: list[Dict[str, str]]) -> str:
    try:
        response = await asyncio.to_thread(
            ollama.chat,
            model=OLLAMA_MODEL,
            messages=messages,
        )
        if isinstance(response, dict):
            if 'message' in response and isinstance(response['message'], dict):
                return response['message'].get('content', '')
            return response.get('content', '') or str(response)
        return str(response)
    except Exception as e:
        logger.error("Ollama error: %s", e)
        return "I'm having trouble connecting to my brain right now. Please try again later!"


def parse_tool_call(response_text: str) -> Optional[Dict[str, Any]]:
    text = response_text.strip()
    if not text:
        return None
    try:
        candidate = json.loads(text)
        if isinstance(candidate, dict) and 'tool' in candidate and 'args' in candidate:
            return candidate
    except json.JSONDecodeError:
        pass

    # Simple fallback for messy JSON
    if '"tool"' in text and '"args"' in text:
        try:
            start = text.index('{')
            end = text.rindex('}')
            candidate = json.loads(text[start:end+1])
            if isinstance(candidate, dict) and 'tool' in candidate and 'args' in candidate:
                return candidate
        except Exception:
            return None
    return None


async def process_user_message(user_id: int, username: str, user_text: str) -> str:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            user = User(user_id=user_id, username=username or "Unknown")
            db.add(user)
            db.commit()
            db.refresh(user)

        messages = [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_text},
        ]
        llm_reply = await chat_with_llm(messages)
        tool_call = parse_tool_call(llm_reply)
        if not tool_call:
            return llm_reply

        tool_name = tool_call['tool']
        args = tool_call.get('args', {})
        tool_response = ""

        if tool_name == 'log_food':
            tool_response = bot_tools.log_food(
                db,
                user_id,
                description=args.get('description', ''),
                calories=float(args.get('calories', 0)),
                protein=float(args.get('protein', 0)),
                carbs=float(args.get('carbs', 0)),
                fat=float(args.get('fat', 0)),
                amount=float(args.get('amount', 1)),
                unit=args.get('unit', 'portion'),
            )
        elif tool_name == 'log_activity':
            tool_response = bot_tools.log_activity(
                db,
                user_id,
                description=args.get('description', ''),
                calories_burned=float(args.get('calories_burned', 0)),
                duration=float(args.get('duration', 0)),
                unit=args.get('unit', 'min'),
            )
        elif tool_name == 'get_daily_summary':
            requested_date = args.get('date')
            target_date = date.fromisoformat(requested_date) if requested_date else date.today()
            tool_response = bot_tools.get_daily_summary(db, user_id, target_date)
        elif tool_name == 'update_user_goals':
            tool_response = bot_tools.update_user_goals(
                db,
                user_id,
                calorie_goal=args.get('calorie_goal'),
                protein_goal=args.get('protein_goal'),
                carb_goal=args.get('carb_goal'),
                fat_goal=args.get('fat_goal'),
            )
        elif tool_name == 'get_user_profile':
            tool_response = bot_tools.get_user_profile(db, user_id)
        elif tool_name == 'query_logs':
            start_date = datetime.fromisoformat(args.get('start_date')) if args.get('start_date') else datetime.utcnow() - timedelta(days=7)
            end_date = datetime.fromisoformat(args.get('end_date')) if args.get('end_date') else datetime.utcnow()
            tool_response = bot_tools.query_logs(db, user_id, start_date, end_date)
        elif tool_name == 'set_reminder':
            trigger_time = datetime.fromisoformat(args.get('trigger_time')) if args.get('trigger_time') else datetime.utcnow()
            tool_response = bot_tools.set_reminder(db, user_id, trigger_time, args.get('message', 'Reminder from Nutri'))
        else:
            tool_response = f"I don't have a tool named {tool_name}."

        followup_messages = [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_text},
            {'role': 'assistant', 'content': str(tool_response)},
        ]
        final_reply = await chat_with_llm(followup_messages)
        return final_reply


async def send_bot_message(chat_id: int, text: str):
    if bot_app is None:
        logger.warning("Bot application is not initialized yet.")
        return
    try:
        await bot_app.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error("Failed to send Telegram message: %s", e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with SessionLocal() as db:
        user_record = db.get(User, user.id)
        if not user_record:
            user_record = User(user_id=user.id, username=user.username or user.first_name or "Unknown")
            db.add(user_record)
            db.commit()
            db.refresh(user_record)

    await update.message.reply_text(
        f"Hello {user.first_name}! I am your omnipresent nutritionist. 🍎\n"
        "I can track your calories, activities, and help you stay on target. "
        "Tell me what you ate or what exercise you completed, or try /summary to see today's totals."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start - Begin or refresh your profile\n"
        "/summary - Today's nutrition summary\n"
        "/profile - View your goals and settings\n"
        "/report - Generate a 7-day trend report\n"
        "/setgoal calorie 2000 protein 120 carbs 220 fat 70\n"
        "/reminder 2026-06-28T18:30:00 Your afternoon snack reminder"
    )


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with SessionLocal() as db:
        summary = bot_tools.get_daily_summary(db, user_id)
    await update.message.reply_text(
        f"Today's summary ({summary['date']}):\n"
        f"Calories net: {summary['total_calories']} kcal\n"
        f"Protein: {summary['protein']} g\n"
        f"Carbs: {summary['carbs']} g\n"
        f"Fat: {summary['fat']} g\n"
        f"Entries: {summary['entry_count']}"
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with SessionLocal() as db:
        profile = bot_tools.get_user_profile(db, user_id)
    if isinstance(profile, str):
        await update.message.reply_text(profile)
        return
    await update.message.reply_text(
        f"Profile for @{profile['username']}:\n"
        f"Timezone: {profile['timezone']}\n"
        f"Goals - Calories: {profile['goals']['calories']} kcal, Protein: {profile['goals']['protein']} g, "
        f"Carbs: {profile['goals']['carbs']} g, Fat: {profile['goals']['fat']} g"
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    report_text = generate_weekly_report(user_id)
    await update.message.reply_text(report_text)


async def setgoal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    args = text.replace('/setgoal', '').strip().split()
    parsed = {}
    for i in range(0, len(args) - 1, 2):
        key = args[i].lower()
        try:
            value = int(args[i + 1])
        except ValueError:
            continue
        if key in ('calorie', 'calories'):
            parsed['calorie_goal'] = value
        elif key in ('protein',):
            parsed['protein_goal'] = value
        elif key in ('carb', 'carbs'):
            parsed['carb_goal'] = value
        elif key in ('fat',):
            parsed['fat_goal'] = value

    if not parsed:
        await update.message.reply_text("Usage: /setgoal calorie 2000 protein 120 carbs 220 fat 70")
        return

    with SessionLocal() as db:
        result = bot_tools.update_user_goals(db, user_id, **parsed)
    await update.message.reply_text(result)


async def reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    parts = text.replace('/reminder', '').strip().split(' ', 1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /reminder <ISO timestamp> <message>")
        return

    try:
        trigger_time = datetime.fromisoformat(parts[0])
        message = parts[1].strip()
    except Exception:
        await update.message.reply_text("Please use ISO format like 2026-06-28T18:30:00 for the reminder time.")
        return

    with SessionLocal() as db:
        result = bot_tools.set_reminder(db, user_id, trigger_time, message)
    await update.message.reply_text(result)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name or "Unknown"
    reply_text = await process_user_message(user_id, username, user_text)
    await update.message.reply_text(reply_text)


async def process_scheduled_reminders():
    with SessionLocal() as db:
        now = datetime.utcnow()
        due_reminders = db.execute(
            select(Reminder).where(Reminder.trigger_time <= now, Reminder.is_sent == False)
        ).scalars().all()
        for reminder in due_reminders:
            await send_bot_message(reminder.user_id, f"⏰ Reminder: {reminder.message}")
            reminder.is_sent = True
        db.commit()


async def check_proactive_logging():
    with SessionLocal() as db:
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=PROACTIVE_HOURS)
        users = db.execute(select(User)).scalars().all()
        for user in users:
            last_log = db.query(Log).filter(Log.user_id == user.user_id).order_by(Log.timestamp.desc()).first()
            if not last_log or last_log.timestamp < cutoff:
                recent_check = db.query(Reminder).filter(
                    Reminder.user_id == user.user_id,
                    Reminder.message.like('%log your latest meal%'),
                    Reminder.trigger_time >= cutoff
                ).order_by(Reminder.trigger_time.desc()).first()
                if recent_check:
                    continue
                prompt = (
                    "Hey there! I noticed you haven't logged any meals or activity in a while. "
                    "Would you like to update your nutrition log now?"
                )
                await send_bot_message(user.user_id, prompt)
                proactive = Reminder(
                    user_id=user.user_id,
                    trigger_time=now,
                    message="Proactive prompt: log your latest meal or activity.",
                    is_sent=True,
                )
                db.add(proactive)
        db.commit()


async def send_daily_recap():
    with SessionLocal() as db:
        today = date.today()
        users = db.execute(select(User)).scalars().all()
        for user in users:
            summary = bot_tools.get_daily_summary(db, user.user_id, today)
            recap = (
                f"Daily recap for {today.isoformat()}:\n"
                f"Net calories: {summary['total_calories']} kcal\n"
                f"Protein: {summary['protein']} g\n"
                f"Carbs: {summary['carbs']} g\n"
                f"Fat: {summary['fat']} g\n"
                f"Entries: {summary['entry_count']}\n"
                "Keep going—you’re doing great! 💪"
            )
            await send_bot_message(user.user_id, recap)


async def start_bot() -> None:
    global bot_app
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", help_command))
    bot_app.add_handler(CommandHandler("summary", summary_command))
    bot_app.add_handler(CommandHandler("profile", profile_command))
    bot_app.add_handler(CommandHandler("report", report_command))
    bot_app.add_handler(CommandHandler("setgoal", setgoal_command))
    bot_app.add_handler(CommandHandler("reminder", reminder_command))
    bot_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Telegram bot started successfully.")


@app.on_event("startup")
async def startup_event():
    init_db()
    # Validate required configuration before starting the bot
    if TELEGRAM_BOT_TOKEN in (None, "", "YOUR_TELEGRAM_BOT_TOKEN"):
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set. Please set the environment variable TELEGRAM_BOT_TOKEN and restart."
        )
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")

    await start_bot()
    scheduler.add_job(process_scheduled_reminders, 'interval', minutes=1, id='scheduled_reminders', replace_existing=True)
    scheduler.add_job(check_proactive_logging, 'interval', minutes=10, id='proactive_logging', replace_existing=True)
    scheduler.add_job(send_daily_recap, 'cron', hour=DAILY_RECAP_HOUR_UTC, minute=0, id='daily_recap', replace_existing=True)
    scheduler.start()


@app.on_event("shutdown")
async def shutdown_event():
    if bot_app:
        await bot_app.updater.stop_polling()
        await bot_app.stop()
        await bot_app.shutdown()
    scheduler.shutdown(wait=False)


@app.get("/")
async def root():
    return {"status": "ok", "message": "Nutri Orchestrator is running!"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
