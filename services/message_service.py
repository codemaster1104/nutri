import base64
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from config import SYSTEM_PROMPT
from database import SessionLocal, User
from services.llm_service import (
    parse_requested_date,
    parse_structured_reply,
    parse_tool_call,
    run_llm_with_optional_images,
    summarize_memory,
)
from tools import bot_tools


PENDING_CLARIFICATIONS: Dict[int, Dict[str, Any]] = {}


def encode_image_bytes(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def pop_pending_clarification(user_id: int) -> Optional[Dict[str, Any]]:
    return PENDING_CLARIFICATIONS.pop(user_id, None)


def build_tool_reply(tool_name: str, tool_response: Any, args: Dict[str, Any]) -> str:
    if tool_name == "log_food":
        description = args.get("description", "that meal")
        return f"Logged {description} for you."

    if tool_name == "log_activity":
        description = args.get("description", "that activity")
        return f"Logged {description}."

    if tool_name == "set_reminder":
        trigger_time = args.get("trigger_time")
        if trigger_time:
            return f"Reminder set for {trigger_time}."
        return "Reminder set."

    if tool_name == "update_user_goals":
        return str(tool_response)

    if tool_name == "get_daily_summary" and isinstance(tool_response, dict):
        return (
            f"Today's summary: {tool_response.get('total_calories', 0)} kcal net, "
            f"{tool_response.get('protein', 0)} g protein, {tool_response.get('carbs', 0)} g carbs, "
            f"{tool_response.get('fat', 0)} g fat."
        )

    if tool_name == "get_user_profile" and isinstance(tool_response, dict):
        goals = tool_response.get("goals", {})
        return (
            f"Your current goals are {goals.get('calories', 0)} kcal, "
            f"{goals.get('protein', 0)} g protein, {goals.get('carbs', 0)} g carbs, and {goals.get('fat', 0)} g fat."
        )

    if tool_name == "query_logs":
        return "I pulled your recent logs."

    return "Done."


def _parse_optional_datetime(raw_value: Optional[str], default_value: datetime) -> datetime:
    if not raw_value:
        return default_value
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return default_value


def _is_today_request(user_text: str) -> bool:
    normalized = (user_text or "").lower()
    return "today" in normalized or "todays" in normalized or "today's" in normalized


async def process_user_message(
    user_id: int,
    username: str,
    user_text: str,
    image_b64s: Optional[list[str]] = None,
    system_prompt: str = SYSTEM_PROMPT,
    model: Optional[str] = None,
) -> str:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            user = User(user_id=user_id, username=username or "Unknown")
            db.add(user)
            db.commit()
            db.refresh(user)

        memory_summary = user.memory_summary or ""

        llm_reply = await run_llm_with_optional_images(
            system_prompt=system_prompt,
            user_text=user_text,
            memory_summary=memory_summary,
            image_b64s=image_b64s,
            model=model,
        )
        structured_reply = parse_structured_reply(llm_reply)

        if image_b64s and isinstance(structured_reply, dict):
            if structured_reply.get("action") == "clarify" or structured_reply.get("needs_clarification") is True:
                clarification_question = str(
                    structured_reply.get("question")
                    or structured_reply.get("clarification_question")
                    or "I am not fully sure what this is. Can you clarify what it was and roughly how much there was?"
                ).strip()
                options = structured_reply.get("options")
                if isinstance(options, list) and options:
                    option_text = "\n".join(f"- {option}" for option in options if option)
                    clarification_question = f"{clarification_question}\n\nPossible options:\n{option_text}"

                PENDING_CLARIFICATIONS[user_id] = {
                    "image_b64s": image_b64s,
                    "system_prompt": system_prompt,
                    "model": model,
                    "original_text": user_text,
                    "question": clarification_question,
                }
                return clarification_question

        tool_call = parse_tool_call(llm_reply)
        if not tool_call:
            final_reply = llm_reply
        else:
            tool_name = tool_call["tool"]
            args = tool_call.get("args", {})

            if tool_name == "log_food":
                tool_response = bot_tools.log_food(
                    db,
                    user_id,
                    description=args.get("description", ""),
                    calories=float(args.get("calories", 0)),
                    protein=float(args.get("protein", 0)),
                    carbs=float(args.get("carbs", 0)),
                    fat=float(args.get("fat", 0)),
                    amount=float(args.get("amount", 1)),
                    unit=args.get("unit", "portion"),
                )
            elif tool_name == "log_activity":
                tool_response = bot_tools.log_activity(
                    db,
                    user_id,
                    description=args.get("description", ""),
                    calories_burned=float(args.get("calories_burned", 0)),
                    duration=float(args.get("duration", 0)),
                    unit=args.get("unit", "min"),
                )
            elif tool_name == "get_daily_summary":
                if _is_today_request(user_text):
                    target_date = None
                else:
                    target_date = parse_requested_date(args.get("date"))
                tool_response = bot_tools.get_daily_summary(db, user_id, target_date)
            elif tool_name == "update_user_goals":
                tool_response = bot_tools.update_user_goals(
                    db,
                    user_id,
                    calorie_goal=args.get("calorie_goal"),
                    protein_goal=args.get("protein_goal"),
                    carb_goal=args.get("carb_goal"),
                    fat_goal=args.get("fat_goal"),
                )
            elif tool_name == "get_user_profile":
                tool_response = bot_tools.get_user_profile(db, user_id)
            elif tool_name == "query_logs":
                start_date = _parse_optional_datetime(
                    args.get("start_date"), datetime.utcnow() - timedelta(days=7)
                )
                end_date = _parse_optional_datetime(args.get("end_date"), datetime.utcnow())
                tool_response = bot_tools.query_logs(db, user_id, start_date, end_date)
            elif tool_name == "set_reminder":
                trigger_time = _parse_optional_datetime(args.get("trigger_time"), datetime.utcnow())
                tool_response = bot_tools.set_reminder(
                    db,
                    user_id,
                    trigger_time,
                    args.get("message", "Reminder from Nutri"),
                )
            else:
                tool_response = f"I don't have a tool named {tool_name}."

            final_reply = build_tool_reply(tool_name, tool_response, args)

        updated_memory = await summarize_memory(memory_summary, user_text, final_reply)
        if updated_memory and updated_memory != memory_summary:
            bot_tools.update_memory_summary(db, user_id, updated_memory)

        return final_reply
