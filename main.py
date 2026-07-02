import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from sqlalchemy import select
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from config import (
    DAILY_RECAP_HOUR_UTC,
    OLLAMA_MODEL,
    OLLAMA_VISION_MODEL,
    PROACTIVE_HOURS,
    TELEGRAM_BOT_TOKEN,
    VISION_SYSTEM_PROMPT,
)
from data_processor import generate_weekly_report
from database import Log, Reminder, SessionLocal, User, init_db
from services.message_service import encode_image_bytes, pop_pending_clarification, process_user_message
from tools import bot_tools


logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Nutri Omnipresent Nutritionist", version="1.0.0")
bot_app = None
scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def typing_action(bot, chat_id, action="typing", interval=4.0):
    """Keep sending typing chat action in the background while processing."""
    async def keep_typing():
        try:
            while True:
                await bot.send_chat_action(chat_id=chat_id, action=action)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Error in typing loop: %s", e)

    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def markdown_to_html(text: str) -> str:
    """Safely convert basic Markdown formatting (bold, inline code) to Telegram HTML."""
    if not text:
        return ""
    # 1. Escape HTML special characters
    html = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 2. Convert **bold** to <b>bold</b>
    html = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", html)
    # 3. Convert `code` to <code>code</code>
    html = re.sub(r"`(.*?)`", r"<code>\1</code>", html)
    return html


async def send_bot_message(chat_id: int, text: str):
    if bot_app is None:
        logger.warning("Bot application is not initialized yet.")
        return
    try:
        await bot_app.bot.send_message(chat_id=chat_id, text=markdown_to_html(text), parse_mode="HTML")
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

    if update.effective_message:
        await update.effective_message.reply_text(
            f"Hello {user.first_name}! I am your omnipresent nutritionist. 🍎\n"
            "I can track your calories, activities, and help you stay on target. "
            "Tell me what you ate or what exercise you completed, or try /summary to see today's totals."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
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
    if update.effective_message:
        await update.effective_message.reply_text(
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
    if not update.effective_message:
        return
    if isinstance(profile, str):
        await update.effective_message.reply_text(profile)
        return
    await update.effective_message.reply_text(
        f"Profile for @{profile['username']}:\n"
        f"Timezone: {profile['timezone']}\n"
        f"Goals - Calories: {profile['goals']['calories']} kcal, Protein: {profile['goals']['protein']} g, "
        f"Carbs: {profile['goals']['carbs']} g, Fat: {profile['goals']['fat']} g"
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    report_text = generate_weekly_report(user_id)
    if update.effective_message:
        await update.effective_message.reply_text(report_text)


async def setgoal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.effective_message
    if not message:
        return
    text = message.text or ""
    args = text.replace("/setgoal", "").strip().split()
    parsed = {}
    for i in range(0, len(args) - 1, 2):
        key = args[i].lower()
        try:
            value = int(args[i + 1])
        except ValueError:
            continue
        if key in ("calorie", "calories"):
            parsed["calorie_goal"] = value
        elif key == "protein":
            parsed["protein_goal"] = value
        elif key in ("carb", "carbs"):
            parsed["carb_goal"] = value
        elif key == "fat":
            parsed["fat_goal"] = value

    if not parsed:
        await message.reply_text("Usage: /setgoal calorie 2000 protein 120 carbs 220 fat 70")
        return

    with SessionLocal() as db:
        result = bot_tools.update_user_goals(db, user_id, **parsed)
    await message.reply_text(result)


async def reminder_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.effective_message
    if not message:
        return
    text = message.text or ""
    parts = text.replace("/reminder", "").strip().split(" ", 1)
    if len(parts) < 2:
        await message.reply_text("Usage: /reminder <ISO timestamp> <message>")
        return

    try:
        trigger_time = datetime.fromisoformat(parts[0])
        message_text = parts[1].strip()
    except Exception:
        await message.reply_text("Please use ISO format like 2026-06-28T18:30:00 for the reminder time.")
        return

    with SessionLocal() as db:
        result = bot_tools.set_reminder(db, user_id, trigger_time, message_text)
    await message.reply_text(result)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return
    user_text = message.text
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name or "Unknown"

    async with typing_action(context.bot, message.chat_id, "typing"):
        pending_clarification = pop_pending_clarification(user_id)
        if pending_clarification:
            clarification_text = (
                f"Previous question: {pending_clarification.get('question', '')}\n"
                f"User clarification: {user_text}\n"
                "Please finalize the most likely food log using the image and this clarification. "
                "If it still is not clear, ask one short follow-up question."
            )
            reply_text = await process_user_message(
                user_id,
                username,
                clarification_text,
                image_b64s=pending_clarification.get("image_b64s"),
                system_prompt=pending_clarification.get("system_prompt", VISION_SYSTEM_PROMPT),
                model=pending_clarification.get("model", OLLAMA_VISION_MODEL),
            )
            await message.reply_text(markdown_to_html(reply_text), parse_mode="HTML")
            return

        reply_text = await process_user_message(user_id, username, user_text)
        await message.reply_text(markdown_to_html(reply_text), parse_mode="HTML")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if message is None or not message.photo:
        return

    async with typing_action(context.bot, message.chat_id, "typing"):
        caption = message.caption or ""
        photo = message.photo[-1]
        telegram_file = await photo.get_file()
        image_bytes = await telegram_file.download_as_bytearray()
        image_b64 = encode_image_bytes(bytes(image_bytes))

        reply_text = await process_user_message(
            user_id=user.id,
            username=user.username or user.first_name or "Unknown",
            user_text=caption or "Please analyze the attached image.",
            image_b64s=[image_b64],
            system_prompt=VISION_SYSTEM_PROMPT,
            model=OLLAMA_VISION_MODEL,
        )
        await message.reply_text(markdown_to_html(reply_text), parse_mode="HTML")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled Telegram exception", exc_info=context.error)


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
                    Reminder.message.like("%log your latest meal%"),
                    Reminder.trigger_time >= cutoff,
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
                "Keep going — you're doing great!"
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
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    bot_app.add_error_handler(handle_error)

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Telegram bot started successfully.")


@app.on_event("startup")
async def startup_event():
    init_db()
    if TELEGRAM_BOT_TOKEN in (None, "", "YOUR_TELEGRAM_BOT_TOKEN"):
        logger.error("TELEGRAM_BOT_TOKEN is not set. Please set TELEGRAM_BOT_TOKEN and restart.")
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable")
    if not OLLAMA_MODEL:
        logger.error("OLLAMA_MODEL is not set. Please set OLLAMA_MODEL and restart.")
        raise RuntimeError("Missing OLLAMA_MODEL environment variable")

    await start_bot()
    scheduler.add_job(
        process_scheduled_reminders,
        "interval",
        minutes=1,
        id="scheduled_reminders",
        replace_existing=True,
    )
    scheduler.add_job(
        check_proactive_logging,
        "interval",
        minutes=10,
        id="proactive_logging",
        replace_existing=True,
    )
    scheduler.add_job(
        send_daily_recap,
        "cron",
        hour=DAILY_RECAP_HOUR_UTC,
        minute=0,
        id="daily_recap",
        replace_existing=True,
    )
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
