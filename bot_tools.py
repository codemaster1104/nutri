from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date
from database import User, Log, Reminder

def log_food(db: Session, user_id: int, description: str, calories: float, protein: float = 0.0, carbs: float = 0.0, fat: float = 0.0, amount: float = 1.0, unit: str = "portion"):
    """Records nutritional intake for a user."""
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
        timestamp=datetime.utcnow()
    )
    db.add(new_log)
    db.commit()
    db.refresh(new_log)
    return f"Successfully logged {description}: {calories} kcal, P:{protein}g, C:{carbs}g, F:{fat}g."

def log_activity(db: Session, user_id: int, description: str, calories_burned: float, duration: float, unit: str = "min"):
    """Records exercise and calories burned."""
    new_log = Log(
        user_id=user_id,
        entry_type="activity",
        description=description,
        calories=-calories_burned, # Calories burned are negative
        amount=duration,
        unit=unit,
        timestamp=datetime.utcnow()
    )
    db.add(new_log)
    db.commit()
    db.refresh(new_log)
    return f"Successfully logged activity {description}: burned {calories_burned} kcal over {duration} {unit}."

def get_daily_summary(db: Session, user_id: int, target_date: date = None):
    """Returns totals for the current day's calories and macros."""
    if target_date is None:
        target_date = date.today()

    start_of_day = datetime.combine(target_date, datetime.min.time())
    end_of_day = datetime.combine(target_date, datetime.max.time())

    logs = db.query(Log).filter(
        Log.user_id == user_id,
        Log.timestamp >= start_of_day,
        Log.timestamp <= end_of_day
    ).all()

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
        "entry_count": len(logs)
    }

def update_user_goals(db: Session, user_id: int, calorie_goal: int = None, protein_goal: int = None, carb_goal: int = None, fat_goal: int = None):
    """Updates nutritional targets in the users table."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        return "User not found."

    if calorie_goal is not None: user.daily_calorie_goal = calorie_goal
    if protein_goal is not None: user.daily_protein_goal = protein_goal
    if carb_goal is not None: user.daily_carb_goal = carb_goal
    if fat_goal is not None: user.daily_fat_goal = fat_goal

    db.commit()
    return f"Goals updated: Cal:{user.daily_calorie_goal}, P:{user.daily_protein_goal}, C:{user.daily_carb_goal}, F:{user.daily_fat_goal}."

def get_user_profile(db: Session, user_id: int):
    """Retrieves the user's current nutritional targets and profile."""
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
            "fat": user.daily_fat_goal
        }
    }

def query_logs(db: Session, user_id: int, start_date: datetime, end_date: datetime):
    """Fetches raw logs for a specific window."""
    logs = db.query(Log).filter(
        Log.user_id == user_id,
        Log.timestamp >= start_date,
        Log.timestamp <= end_date
    ).all()

    result = []
    for log in logs:
        result.append({
            "timestamp": log.timestamp,
            "type": log.entry_type,
            "desc": log.description,
            "cals": log.calories,
            "protein": log.protein,
            "carbs": log.carbs,
            "fat": log.fat,
            "amount": log.amount,
            "unit": log.unit,
        })
    return result


def set_reminder(db: Session, user_id: int, trigger_time: datetime, message: str):
    """Schedules a reminder for a user."""
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
