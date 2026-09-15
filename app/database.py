import csv
import datetime
import os
from pathlib import Path
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, Text, DateTime, ForeignKey, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DB_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "whooshcoach.db")

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Workout(Base):
    __tablename__ = "workouts"

    id = Column(Integer, primary_key=True, index=True)
    workout_id = Column(String(50), index=True)
    title = Column(String(255), index=True)
    category = Column(String(100), index=True)
    all_categories = Column(String(255))
    primary_zone = Column(String(50), index=True)
    all_zones = Column(String(255))
    duration = Column(String(50))
    duration_minutes = Column(Float, default=0.0)
    duration_seconds = Column(Integer, default=0)
    tss = Column(Integer, default=0)
    intensity_factor = Column(Float, default=0.0)
    coach = Column(String(100))
    has_instructions = Column(Boolean, default=False)
    has_rpm_target = Column(Boolean, default=False)
    description = Column(Text)
    workout_steps = Column(Text)
    slug = Column(String(255), unique=True, index=True)
    url = Column(String(500))

class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(String(10), index=True) # YYYY-MM-DD
    title = Column(String(255))
    workout_id = Column(Integer, ForeignKey("workouts.id"), nullable=True)
    primary_zone = Column(String(50))
    planned_duration_minutes = Column(Float, default=0.0)
    planned_tss = Column(Integer, default=0)
    status = Column(String(50), default="scheduled") # scheduled, completed, skipped, modified, rest
    
    # Event Type & Race Attributes
    event_type = Column(String(50), default="workout") # workout, race, rest, custom
    is_manual = Column(Boolean, default=False)
    race_priority = Column(String(10), nullable=True) # A, B, C
    race_type = Column(String(100), nullable=True) # Road Race, Criterium, Gran Fondo, Time Trial, Gravel, MTB, Virtual Race, Other
    target_distance_km = Column(Float, nullable=True)

    # Multi-User & Scoping
    athlete_id = Column(Integer, ForeignKey("athlete_profile.id"), default=1, index=True)

    activity_id = Column(String(50), nullable=True)
    actual_duration_minutes = Column(Float, nullable=True)
    actual_tss = Column(Integer, nullable=True)
    actual_avg_watts = Column(Float, nullable=True)
    actual_avg_hr = Column(Float, nullable=True)
    
    athlete_notes = Column(Text, nullable=True)
    coach_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    workout = relationship("Workout")

class RideActivity(Base):
    __tablename__ = "activities"

    id = Column(String(50), primary_key=True)
    athlete_id = Column(Integer, ForeignKey("athlete_profile.id"), default=1, index=True)
    name = Column(String(255))
    type = Column(String(50), default="VirtualRide")
    start_date = Column(String(50))
    start_date_local = Column(String(50), index=True)
    elapsed_time = Column(Integer)
    moving_time = Column(Integer)
    distance = Column(Float)
    total_elevation_gain = Column(Float)
    average_watts = Column(Float, nullable=True)
    weighted_average_watts = Column(Float, nullable=True)
    max_watts = Column(Float, nullable=True)
    average_heartrate = Column(Float, nullable=True)
    max_heartrate = Column(Float, nullable=True)
    average_cadence = Column(Float, nullable=True)
    calculated_tss = Column(Integer, nullable=True)
    matched_event_id = Column(Integer, nullable=True)
    raw_json = Column(Text, nullable=True)
    synced_at = Column(DateTime, default=datetime.datetime.utcnow)

class AthleteProfile(Base):
    __tablename__ = "athlete_profile"

    id = Column(Integer, primary_key=True, default=1)
    name = Column(String(100), default="Xander")
    platform = Column(String(50), default="mywhoosh") # mywhoosh, zwift, both
    ftp = Column(Integer, default=220)
    max_hr = Column(Integer, default=185)
    weight_kg = Column(Float, default=75.0)
    coaching_mode = Column(String(50), default="autonomous")
    target_weekly_hours = Column(Float, nullable=True, default=None)
    target_weekly_tss = Column(Integer, nullable=True, default=None)
    primary_goal = Column(String(255), default="FTP Growth & Endurance")
    available_days = Column(String(255), default="Tuesday,Thursday,Saturday,Sunday")
    
    gemini_api_key = Column(String(255), nullable=True)
    gemini_model = Column(String(50), default="gemini-3.6-flash")

    # MyWhoosh credentials & sync state
    mywhoosh_email = Column(String(255), nullable=True)
    mywhoosh_password = Column(String(255), nullable=True)
    mywhoosh_token = Column(Text, nullable=True)
    mywhoosh_id = Column(String(100), nullable=True)
    last_mywhoosh_sync = Column(String(50), nullable=True)

    # Zwift credentials & sync state
    zwift_username = Column(String(255), nullable=True)
    zwift_password = Column(String(255), nullable=True)
    zwift_token = Column(Text, nullable=True)
    zwift_id = Column(String(100), nullable=True)
    last_zwift_sync = Column(String(50), nullable=True)

class CoachLog(Base):
    __tablename__ = "coach_logs"

    id = Column(Integer, primary_key=True, index=True)
    athlete_id = Column(Integer, ForeignKey("athlete_profile.id"), default=1, index=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    role = Column(String(50)) # user, assistant, system
    message = Column(Text)
    context_type = Column(String(50), default="chat") # chat, adaptation, plan_generation

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    Base.metadata.create_all(bind=engine)

    # Auto-migrate SQLite columns if table already existed
    with engine.connect() as conn:
        for col, col_type in [
            ("coaching_mode", "VARCHAR(50) DEFAULT 'autonomous'"),
            ("gemini_model", "VARCHAR(50) DEFAULT 'gemini-3.6-flash'"),
            ("platform", "VARCHAR(50) DEFAULT 'mywhoosh'"),
            ("mywhoosh_email", "VARCHAR(255)"),
            ("mywhoosh_password", "VARCHAR(255)"),
            ("mywhoosh_token", "TEXT"),
            ("mywhoosh_id", "VARCHAR(100)"),
            ("last_mywhoosh_sync", "VARCHAR(50)"),
            ("zwift_username", "VARCHAR(255)"),
            ("zwift_password", "VARCHAR(255)"),
            ("zwift_token", "TEXT"),
            ("zwift_id", "VARCHAR(100)"),
            ("last_zwift_sync", "VARCHAR(50)")
        ]:
            try:
                conn.execute(text(f"ALTER TABLE athlete_profile ADD COLUMN {col} {col_type}"))
                conn.commit()
            except Exception:
                pass

        # Migrate CalendarEvent columns for race, event support, and athlete scoping
        for col, col_type in [
            ("event_type", "VARCHAR(50) DEFAULT 'workout'"),
            ("is_manual", "BOOLEAN DEFAULT 0"),
            ("race_priority", "VARCHAR(10)"),
            ("race_type", "VARCHAR(100)"),
            ("target_distance_km", "FLOAT"),
            ("activity_id", "VARCHAR(50)"),
            ("athlete_id", "INTEGER DEFAULT 1")
        ]:
            try:
                conn.execute(text(f"ALTER TABLE calendar_events ADD COLUMN {col} {col_type}"))
                conn.commit()
            except Exception:
                pass

        # Migrate activities and coach_logs for athlete scoping
        for tbl, col, col_type in [
            ("activities", "athlete_id", "INTEGER DEFAULT 1"),
            ("coach_logs", "athlete_id", "INTEGER DEFAULT 1")
        ]:
            try:
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_type}"))
                conn.commit()
            except Exception:
                pass

        # Backfill athlete_id to 1 for existing records
        try:
            conn.execute(text("UPDATE calendar_events SET athlete_id = 1 WHERE athlete_id IS NULL"))
            conn.execute(text("UPDATE activities SET athlete_id = 1 WHERE athlete_id IS NULL"))
            conn.execute(text("UPDATE coach_logs SET athlete_id = 1 WHERE athlete_id IS NULL"))
            conn.commit()
        except Exception:
            pass

        # Backfill is_manual for existing user-created events (races, custom events, or events with athlete notes)
        try:
            conn.execute(text("UPDATE calendar_events SET is_manual = 1 WHERE athlete_notes IS NOT NULL OR event_type IN ('race', 'custom') OR race_priority IS NOT NULL"))
            conn.commit()
        except Exception:
            pass

        # Migrate existing activity linkage from legacy strava_activity_id if present
        try:
            conn.execute(text("UPDATE calendar_events SET activity_id = strava_activity_id WHERE activity_id IS NULL AND strava_activity_id IS NOT NULL"))
            conn.commit()
        except Exception:
            pass

        # Migrate legacy strava_activities to activities table if present
        try:
            conn.execute(text("""
                INSERT OR IGNORE INTO activities (
                    id, athlete_id, name, type, start_date, start_date_local, elapsed_time, moving_time,
                    distance, total_elevation_gain, average_watts, weighted_average_watts, max_watts,
                    average_heartrate, max_heartrate, average_cadence, calculated_tss,
                    matched_event_id, raw_json, synced_at
                )
                SELECT 
                    id, 1, name, type, start_date, start_date_local, elapsed_time, moving_time,
                    distance, total_elevation_gain, average_watts, weighted_average_watts, max_watts,
                    average_heartrate, max_heartrate, average_cadence, calculated_tss,
                    matched_event_id, raw_json, synced_at 
                FROM strava_activities
            """))
            conn.commit()
        except Exception:
            pass

    db = SessionLocal()
    try:
        # Check if default athlete profile exists
        profile = db.query(AthleteProfile).filter(AthleteProfile.id == 1).first()
        if not profile:
            profile = AthleteProfile(
                id=1,
                name="Xander",
                platform="mywhoosh",
                gemini_api_key=os.environ.get("GEMINI_API_KEY", "")
            )
            db.add(profile)
            db.commit()
        else:
            if profile.name == "Athlete":
                profile.name = "Xander"
            if not profile.platform:
                profile.platform = "mywhoosh"
            db.commit()

        # Seed secondary profile (Mom - Zwift) if not exists
        profile2 = db.query(AthleteProfile).filter(AthleteProfile.id == 2).first()
        if not profile2:
            profile2 = AthleteProfile(
                id=2,
                name="Mom",
                platform="zwift",
                ftp=150,
                max_hr=175,
                weight_kg=65.0,
                coaching_mode="autonomous",
                primary_goal="Cardio Fitness & Zwift Group Rides",
                available_days="Monday,Wednesday,Friday,Saturday",
                gemini_api_key=profile.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
            )
            db.add(profile2)
            db.commit()

        # Seed MyWhoosh workouts if empty
        count = db.query(Workout).count()
        if count == 0:
            csv_path = Path(__file__).resolve().parent.parent / "mywhoosh_workouts_unique.csv"
            if not csv_path.exists():
                csv_path = Path("mywhoosh_workouts_unique.csv")
            
            if csv_path.exists():
                print(f"Seeding MyWhoosh workouts from {csv_path}...")
                workouts_to_add = []
                with open(csv_path, mode="r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        w = Workout(
                            workout_id=row.get("workout_id", ""),
                            title=row.get("title", ""),
                            category=row.get("category", ""),
                            all_categories=row.get("all_categories", ""),
                            primary_zone=row.get("primary_zone", "Z2 - Endurance"),
                            all_zones=row.get("all_zones", ""),
                            duration=row.get("duration", ""),
                            duration_minutes=float(row.get("duration_minutes") or 0.0),
                            duration_seconds=int(row.get("duration_seconds") or 0),
                            tss=int(row.get("tss") or 0),
                            intensity_factor=float(row.get("intensity_factor") or 0.0),
                            coach=row.get("coach", ""),
                            has_instructions=row.get("has_instructions", "False").lower() == "true",
                            has_rpm_target=row.get("has_rpm_target", "False").lower() == "true",
                            description=row.get("description", ""),
                            workout_steps=row.get("workout_steps", ""),
                            slug=row.get("slug", ""),
                            url=row.get("url", "")
                        )
                        workouts_to_add.append(w)
                db.bulk_save_objects(workouts_to_add)
                db.commit()
                print(f"Successfully seeded {len(workouts_to_add)} MyWhoosh workouts!")
    finally:
        db.close()
