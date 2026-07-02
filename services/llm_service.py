import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional

import ollama

from config import OLLAMA_MODEL


logger = logging.getLogger(__name__)


async def chat_with_llm(messages: list[Dict[str, Any]], model: Optional[str] = None) -> str:
    try:
        resolved_model = model or OLLAMA_MODEL
        if not resolved_model:
            raise RuntimeError("OLLAMA_MODEL is not set")

        response = await asyncio.to_thread(
            ollama.chat,
            model=resolved_model,
            messages=messages,
        )

        if isinstance(response, dict):
            message = response.get("message")
            if isinstance(message, dict):
                return message.get("content", "")
            return response.get("content", "") or ""

        message = getattr(response, "message", None)
        if message is not None:
            content = getattr(message, "content", None)
            if content:
                return content

        content = getattr(response, "content", None)
        if content:
            return content

        if hasattr(response, "model_dump"):
            dumped = response.model_dump()
            message = dumped.get("message") if isinstance(dumped, dict) else None
            if isinstance(message, dict):
                return message.get("content", "")
            return dumped.get("content", "") if isinstance(dumped, dict) else ""

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
        if isinstance(candidate, dict) and "tool" in candidate and "args" in candidate:
            return candidate
    except json.JSONDecodeError:
        pass

    if '"tool"' in text and '"args"' in text:
        try:
            start = text.index("{")
            end = text.rindex("}")
            candidate = json.loads(text[start : end + 1])
            if isinstance(candidate, dict) and "tool" in candidate and "args" in candidate:
                return candidate
        except Exception:
            return None
    return None


def parse_structured_reply(response_text: str) -> Optional[Dict[str, Any]]:
    text = response_text.strip()
    if not text:
        return None
    try:
        candidate = json.loads(text)
        if isinstance(candidate, dict):
            return candidate
    except json.JSONDecodeError:
        pass

    if "{" in text and "}" in text:
        try:
            start = text.index("{")
            end = text.rindex("}")
            candidate = json.loads(text[start : end + 1])
            if isinstance(candidate, dict):
                return candidate
        except Exception:
            return None
    return None


def parse_requested_date(requested_date: Optional[str]) -> Optional[date]:
    if not requested_date:
        return None

    normalized = str(requested_date).strip().lower()
    if normalized in {"today", "now"}:
        return None
    if normalized == "yesterday":
        return date.today() - timedelta(days=1)
    if normalized == "tomorrow":
        return date.today() + timedelta(days=1)

    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


async def run_llm_with_optional_images(
    system_prompt: str,
    user_text: str,
    memory_summary: str,
    chat_history: Optional[list[Dict[str, Any]]] = None,
    image_b64s: Optional[list[str]] = None,
    model: Optional[str] = None,
) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "system",
            "content": f"User memory summary:\n{memory_summary or 'No long-term memory yet.'}",
        },
    ]

    if chat_history:
        messages.extend(chat_history)

    new_user_message: Dict[str, Any] = {"role": "user", "content": user_text or "Analyze the image and respond helpfully."}
    if image_b64s:
        new_user_message["images"] = image_b64s

    messages.append(new_user_message)

    return await chat_with_llm(messages, model=model)


async def summarize_memory(memory_summary: str, user_message: str, assistant_reply: str) -> str:
    prompt = (
        "Update the long-term memory for a nutrition assistant. Keep it short, factual, and useful. "
        "Capture stable preferences, goals, habits, and important reminders only. "
        "Do not include casual chit-chat. Return at most 4 bullet-like sentences as plain text.\n\n"
        f"Existing memory:\n{memory_summary or 'None'}\n\n"
        f"Latest user message:\n{user_message}\n\n"
        f"Assistant reply:\n{assistant_reply}"
    )
    messages = [
        {
            "role": "system",
            "content": "You are a concise memory summarizer for a nutrition chatbot.",
        },
        {"role": "user", "content": prompt},
    ]
    return (await chat_with_llm(messages)).strip()
