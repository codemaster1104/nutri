from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime

# Use SQLite for local persistence
DATABASE_URL = "sqlite:///./nutri.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True) # Telegram user ID
    username = Column(String)
    timezone = Column(String, default="UTC")

    daily_calorie_goal = Column(Integer, default=2000)
    daily_protein_goal = Column(Integer, default=150)
    daily_carb_goal = Column(Integer, default=250)
    daily_fat_goal = Column(Integer, default=70)

    created_at = Column(DateTime, default=datetime.utcnow)

    logs = relationship("Log", back_populates="user")
    reminders = relationship("Reminder", back_populates="user")

class Log(Base):
    __tablename__ = "logs"

    log_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.user_id"))
    timestamp = Column(DateTime, default=datetime.utcnow)

    entry_type = Column(String) # 'food' or 'activity'
    description = Column(String)

    calories = Column(Float) # Net calories
    protein = Column(Float, default=0.0)
    carbs = Column(Float, default=0.0)
    fat = Column(Float, default=0.0)

    amount = Column(Float)
    unit = Column(String)

    user = relationship("User", back_populates="logs")

class Reminder(Base):
    __tablename__ = "reminders"

    reminder_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.user_id"))
    trigger_time = Column(DateTime)
    message = Column(String)
    is_sent = Column(Boolean, default=False)

    user = relationship("User", back_populates="reminders")

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
