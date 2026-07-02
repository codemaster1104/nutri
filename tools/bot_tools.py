import json
from datetime import date, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from database import Log, Reminder, User


OPENFOODFACTS_BASE_URL = "https://world.openfoodfacts.org"
OPENFOODFACTS_USER_AGENT = "NutriBot/1.0 (local-dev)"


def _format_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "unknown time"


def _get_tzinfo(timezone_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _day_window(target_date: date, timezone_name: str | None):
    tzinfo = _get_tzinfo(timezone_name)
    start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=tzinfo)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start_utc, end_utc


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _off_get_json(path: str, params: dict[str, Any]) -> dict[str, Any]:
    query_string = urlencode(params, doseq=True)
    url = f"{OPENFOODFACTS_BASE_URL}{path}?{query_string}"
    request = Request(
        url,
        headers={
            "User-Agent": OPENFOODFACTS_USER_AGENT,
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {"error": str(exc), "url": url}

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        return {"error": f"Invalid JSON response: {exc}", "url": url}


def _extract_nutriments(product: dict[str, Any]) -> dict[str, float | None]:
    nutriments = product.get("nutriments") or {}
    energy_kcal = _safe_float(nutriments.get("energy-kcal_100g") or nutriments.get("energy-kcal"))
    if energy_kcal is None:
        energy_kj = _safe_float(nutriments.get("energy_100g") or nutriments.get("energy"))
        if energy_kj is not None:
            energy_kcal = energy_kj / 4.184

    return {
        "calories": energy_kcal,
        "protein": _safe_float(nutriments.get("proteins_100g") or nutriments.get("proteins")),
        "carbs": _safe_float(nutriments.get("carbohydrates_100g") or nutriments.get("carbohydrates")),
        "fat": _safe_float(nutriments.get("fat_100g") or nutriments.get("fat")),
        "fiber": _safe_float(nutriments.get("fiber_100g") or nutriments.get("fiber")),
        "sugars": _safe_float(nutriments.get("sugars_100g") or nutriments.get("sugars")),
        "salt": _safe_float(nutriments.get("salt_100g") or nutriments.get("salt")),
    }


def _scale_nutriments(nutriments: dict[str, float | None], portion_grams: float | None) -> dict[str, float | None]:
    if not portion_grams:
        return nutriments

    factor = portion_grams / 100.0
    scaled: dict[str, float | None] = {}
    for key, value in nutriments.items():
        scaled[key] = None if value is None else value * factor
    return scaled


def _product_title(product: dict[str, Any]) -> str:
    return (
        product.get("product_name")
        or product.get("product_name_en")
        or product.get("generic_name")
        or product.get("generic_name_en")
        or product.get("product_name_fr")
        or "Unknown product"
    )


def _product_match(product: dict[str, Any], portion_grams: float | None) -> dict[str, Any]:
    nutriments = _extract_nutriments(product)
    scaled_nutriments = _scale_nutriments(nutriments, portion_grams)

    return {
        "name": _product_title(product),
        "brand": product.get("brands") or "",
        "url": product.get("url") or "",
        "category": product.get("categories") or "",
        "serving_size": product.get("serving_size") or "",
        "serving_quantity": product.get("serving_quantity"),
        "nutriments_per_100g": nutriments,
        "nutriments_for_portion": scaled_nutriments,
        "nutriscore": product.get("nutriscore_grade") or product.get("nutrition_grade_fr") or "",
        "portion_grams": portion_grams,
    }


def search_online_nutrition(db: Session, user_id: int, query: str, portion_grams: float | None = None, max_results: int = 3):
    del db, user_id

    cleaned_query = (query or "").strip()
    if not cleaned_query:
        return {
            "query": "",
            "source": "openfoodfacts",
            "error": "Missing search query.",
            "results": [],
        }

    limit = max(1, min(int(max_results), 5))
    search_payload = _off_get_json(
        "/cgi/search.pl",
        {
            "search_terms": cleaned_query,
            "search_simple": 1,
            "action": "process",
            "json": 1,
            "page_size": limit,
        },
    )

    if search_payload.get("error"):
        return {
            "query": cleaned_query,
            "source": "openfoodfacts",
            "error": search_payload["error"],
            "url": search_payload.get("url", ""),
            "results": [],
        }

    products = search_payload.get("products") or []
    results = [_product_match(product, portion_grams) for product in products[:limit]]

    return {
        "query": cleaned_query,
        "source": "openfoodfacts",
        "search_url": f"{OPENFOODFACTS_BASE_URL}/cgi/search.pl?{urlencode({'search_terms': cleaned_query, 'search_simple': 1, 'action': 'process', 'json': 1})}",
        "result_count": len(results),
        "portion_grams": portion_grams,
        "results": results,
    }


def log_food(
    db: Session,
    user_id: int,
    description: str,
    calories: float,
    protein: float = 0.0,
    carbs: float = 0.0,
    fat: float = 0.0,
    amount: float = 1.0,
    unit: str = "portion",
):
    new_log = Log(
        user_id=user_id,
        entry_type="food",
        description=description,
        calories=calories,
        protein=protein,
        carbs=carbs,
        fat=fat,
        amount=amount,
        unit=unit,
        timestamp=datetime.utcnow(),
    )
    db.add(new_log)
    db.commit()
    db.refresh(new_log)
    return (
        f"Successfully logged {description} at {_format_timestamp(new_log.timestamp)}: "
        f"{calories} kcal, P:{protein}g, C:{carbs}g, F:{fat}g."
    )


def log_activity(db: Session, user_id: int, description: str, calories_burned: float, duration: float, unit: str = "min"):
    new_log = Log(
        user_id=user_id,
        entry_type="activity",
        description=description,
        calories=-calories_burned,
        amount=duration,
        unit=unit,
        timestamp=datetime.utcnow(),
    )
    db.add(new_log)
    db.commit()
    db.refresh(new_log)
    return (
        f"Successfully logged activity {description} at {_format_timestamp(new_log.timestamp)}: "
        f"burned {calories_burned} kcal over {duration} {unit}."
    )


def get_daily_summary(db: Session, user_id: int, target_date: date = None):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        return {
            "date": (target_date or date.today()).isoformat(),
            "total_calories": 0,
            "protein": 0,
            "carbs": 0,
            "fat": 0,
            "entry_count": 0,
        }

    if target_date is None:
        target_date = datetime.now(_get_tzinfo(user.timezone)).date()

    start_of_day, end_of_day = _day_window(target_date, user.timezone)

    logs = (
        db.query(Log)
        .filter(
            Log.user_id == user_id,
            Log.timestamp >= start_of_day,
            Log.timestamp <= end_of_day,
        )
        .all()
    )

    total_calories = sum(log.calories for log in logs)
    total_protein = sum(log.protein for log in logs if log.entry_type == "food")
    total_carbs = sum(log.carbs for log in logs if log.entry_type == "food")
    total_fat = sum(log.fat for log in logs if log.entry_type == "food")

    return {
        "date": target_date.isoformat(),
        "total_calories": total_calories,
        "protein": total_protein,
        "carbs": total_carbs,
        "fat": total_fat,
        "entry_count": len(logs),
    }


def update_user_goals(
    db: Session,
    user_id: int,
    calorie_goal: int = None,
    protein_goal: int = None,
    carb_goal: int = None,
    fat_goal: int = None,
):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        return "User not found."

    if calorie_goal is not None:
        user.daily_calorie_goal = calorie_goal
    if protein_goal is not None:
        user.daily_protein_goal = protein_goal
    if carb_goal is not None:
        user.daily_carb_goal = carb_goal
    if fat_goal is not None:
        user.daily_fat_goal = fat_goal

    db.commit()
    return (
        f"Goals updated: Cal:{user.daily_calorie_goal}, P:{user.daily_protein_goal}, "
        f"C:{user.daily_carb_goal}, F:{user.daily_fat_goal}."
    )


def get_user_profile(db: Session, user_id: int):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        return "Profile not found. Please set your goals first."

    return {
        "username": user.username,
        "timezone": user.timezone,
        "goals": {
            "calories": user.daily_calorie_goal,
            "protein": user.daily_protein_goal,
            "carbs": user.daily_carb_goal,
            "fat": user.daily_fat_goal,
        },
        "memory_summary": user.memory_summary or "",
    }


def get_memory_summary(db: Session, user_id: int) -> str:
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        return ""
    return user.memory_summary or ""


def update_memory_summary(db: Session, user_id: int, summary: str):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        return "User not found."

    user.memory_summary = (summary or "").strip()
    db.commit()
    return "Memory summary updated."


def query_logs(db: Session, user_id: int, start_date: datetime, end_date: datetime):
    logs = (
        db.query(Log)
        .filter(
            Log.user_id == user_id,
            Log.timestamp >= start_date,
            Log.timestamp <= end_date,
        )
        .all()
    )

    result = []
    for log in logs:
        result.append(
            {
                "timestamp": log.timestamp,
                "logged_at": log.timestamp.isoformat() if log.timestamp else None,
                "type": log.entry_type,
                "desc": log.description,
                "cals": log.calories,
                "protein": log.protein,
                "carbs": log.carbs,
                "fat": log.fat,
                "amount": log.amount,
                "unit": log.unit,
            }
        )
    return result


def set_reminder(db: Session, user_id: int, trigger_time: datetime, message: str):
    reminder = Reminder(
        user_id=user_id,
        trigger_time=trigger_time,
        message=message,
        is_sent=False,
    )
    db.add(reminder)
    db.commit()
    db.refresh(reminder)
    return f"Reminder scheduled for {trigger_time.isoformat()}. I will remind you then."
