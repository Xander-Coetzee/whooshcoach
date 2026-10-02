import collections
import datetime
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_

# --- IN-MEMORY & CONSOLE LOGGING SETUP ---
recent_logs = collections.deque(maxlen=150)

class MemoryLogHandler(logging.Handler):
    def emit(self, record):
        try:
            recent_logs.append({
                "time": datetime.datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage()
            })
        except Exception:
            pass

mem_handler = MemoryLogHandler()
mem_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(mem_handler)

logger = logging.getLogger("whooshcoach")

from app.database import (
    init_db, get_db, AthleteProfile, Workout, CalendarEvent, RideActivity, CoachLog
)
from app.gemini_coach import (
    generate_training_plan, adapt_calendar_after_activity, chat_with_coach, test_gemini_connection
)
from app.scheduler import start_scheduler, stop_scheduler, sync_all_athletes
from app.mywhoosh_service import mywhoosh_service
from app.zwift_service import zwift_service

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("WhooshCoach server starting up...")
    init_db()
    start_scheduler()
    yield
    # Shutdown
    logger.info("WhooshCoach server shutting down...")
    stop_scheduler()

app = FastAPI(title="WhooshCoach", lifespan=lifespan)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled server error at {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": str(exc), "path": request.url.path})

# Static Files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ----------------- PYDANTIC SCHEMAS -----------------

class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    platform: Optional[str] = None # mywhoosh, zwift, both
    ftp: Optional[int] = None
    max_hr: Optional[int] = None
    weight_kg: Optional[float] = None
    coaching_mode: Optional[str] = None
    target_weekly_hours: Optional[float] = None
    target_weekly_tss: Optional[int] = None
    primary_goal: Optional[str] = None
    available_days: Optional[str] = None
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None
    # MyWhoosh
    mywhoosh_email: Optional[str] = None
    mywhoosh_password: Optional[str] = None
    # Zwift
    zwift_username: Optional[str] = None
    zwift_password: Optional[str] = None

class AthleteCreate(BaseModel):
    name: str
    platform: Optional[str] = "mywhoosh"
    ftp: Optional[int] = 200
    max_hr: Optional[int] = 180
    weight_kg: Optional[float] = 70.0
    coaching_mode: Optional[str] = "autonomous"
    primary_goal: Optional[str] = "Cardio Fitness & Group Rides"
    available_days: Optional[str] = "Monday,Wednesday,Friday,Saturday"

class PlanRequest(BaseModel):
    athlete_id: Optional[int] = 1
    start_date: str # YYYY-MM-DD
    days: int = 7
    user_instructions: Optional[str] = ""
    overwrite_existing: bool = True

class GeminiTestRequest(BaseModel):
    athlete_id: Optional[int] = None
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None

class ZwiftTestRequest(BaseModel):
    athlete_id: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None

class EventCreate(BaseModel):
    athlete_id: Optional[int] = 1
    date: str
    title: str
    workout_id: Optional[int] = None
    event_type: Optional[str] = "workout" # workout, race, rest, custom
    race_priority: Optional[str] = None # A, B, C
    race_type: Optional[str] = None # Road Race, Criterium, Gran Fondo, Time Trial, Gravel, MTB, Virtual Race, Other
    target_distance_km: Optional[float] = None
    primary_zone: Optional[str] = "Z2 - Endurance"
    planned_duration_minutes: float = 60.0
    planned_tss: int = 50
    coach_notes: Optional[str] = ""
    athlete_notes: Optional[str] = ""

class EventUpdate(BaseModel):
    date: Optional[str] = None
    title: Optional[str] = None
    event_type: Optional[str] = None
    race_priority: Optional[str] = None
    race_type: Optional[str] = None
    target_distance_km: Optional[float] = None
    status: Optional[str] = None # scheduled, completed, skipped, modified, rest
    primary_zone: Optional[str] = None
    planned_duration_minutes: Optional[float] = None
    planned_tss: Optional[int] = None
    actual_duration_minutes: Optional[float] = None
    actual_tss: Optional[int] = None
    actual_avg_watts: Optional[float] = None
    actual_avg_hr: Optional[float] = None
    coach_notes: Optional[str] = None
    athlete_notes: Optional[str] = None

class ChatRequest(BaseModel):
    athlete_id: Optional[int] = 1
    message: str

# ----------------- ROOT & GUI -----------------

@app.get("/", response_class=HTMLResponse)
async def read_index():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>WhooshCoach server running! Uploading GUI...</h1>")

# ----------------- ATHLETES API -----------------

@app.get("/api/athletes")
def list_athletes(db: Session = Depends(get_db)):
    """List all registered athlete profiles."""
    athletes = db.query(AthleteProfile).all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "platform": getattr(a, "platform", "mywhoosh") or "mywhoosh",
            "ftp": a.ftp,
            "max_hr": a.max_hr,
            "weight_kg": a.weight_kg,
            "coaching_mode": getattr(a, "coaching_mode", "autonomous") or "autonomous",
            "primary_goal": a.primary_goal,
            "has_gemini_key": bool(a.gemini_api_key),
            "gemini_model": a.gemini_model or "gemini-3.6-flash",
            "is_mywhoosh_connected": bool(a.mywhoosh_token or (a.mywhoosh_email and a.mywhoosh_password)),
            "is_zwift_connected": bool(a.zwift_token or (a.zwift_username and a.zwift_password)),
            "last_mywhoosh_sync": a.last_mywhoosh_sync,
            "last_zwift_sync": a.last_zwift_sync
        }
        for a in athletes
    ]

@app.post("/api/athletes")
def create_athlete(athlete_in: AthleteCreate, db: Session = Depends(get_db)):
    """Create a new athlete profile."""
    # Inherit shared API key from Profile 1 if available
    p1 = db.query(AthleteProfile).filter(AthleteProfile.id == 1).first()
    shared_key = (p1.gemini_api_key if p1 else "") or os.environ.get("GEMINI_API_KEY", "")

    new_a = AthleteProfile(
        name=athlete_in.name,
        platform=athlete_in.platform or "mywhoosh",
        ftp=athlete_in.ftp or 200,
        max_hr=athlete_in.max_hr or 180,
        weight_kg=athlete_in.weight_kg or 70.0,
        coaching_mode=athlete_in.coaching_mode or "autonomous",
        primary_goal=athlete_in.primary_goal or "Fitness",
        available_days=athlete_in.available_days or "Monday,Wednesday,Friday,Saturday",
        gemini_api_key=shared_key
    )
    db.add(new_a)
    db.commit()
    db.refresh(new_a)
    return {"success": True, "athlete": {"id": new_a.id, "name": new_a.name, "platform": new_a.platform}}

@app.get("/api/athletes/{athlete_id}")
def get_athlete(athlete_id: int, db: Session = Depends(get_db)):
    """Get full details of a specific athlete."""
    a = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Athlete not found")
    return {
        "id": a.id,
        "name": a.name,
        "platform": getattr(a, "platform", "mywhoosh") or "mywhoosh",
        "ftp": a.ftp,
        "max_hr": a.max_hr,
        "weight_kg": a.weight_kg,
        "coaching_mode": getattr(a, "coaching_mode", "autonomous") or "autonomous",
        "target_weekly_hours": a.target_weekly_hours,
        "target_weekly_tss": a.target_weekly_tss,
        "primary_goal": a.primary_goal,
        "available_days": a.available_days,
        "has_gemini_key": bool(a.gemini_api_key),
        "gemini_model": a.gemini_model or "gemini-3.6-flash",
        "mywhoosh_email": a.mywhoosh_email or "",
        "has_mywhoosh_password": bool(a.mywhoosh_password),
        "is_mywhoosh_connected": bool(a.mywhoosh_token or (a.mywhoosh_email and a.mywhoosh_password)),
        "last_mywhoosh_sync": a.last_mywhoosh_sync,
        "zwift_username": a.zwift_username or "",
        "has_zwift_password": bool(a.zwift_password),
        "is_zwift_connected": bool(a.zwift_token or (a.zwift_username and a.zwift_password)),
        "last_zwift_sync": a.last_zwift_sync
    }

# ----------------- CALENDAR API -----------------

@app.get("/api/calendar")
def get_calendar_events(
    athlete_id: int = Query(1),
    start: Optional[str] = None,
    end: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Returns calendar events formatted for FullCalendar scoped to athlete_id."""
    query = db.query(CalendarEvent).filter(CalendarEvent.athlete_id == athlete_id)
    if start:
        query = query.filter(CalendarEvent.date >= start[:10])
    if end:
        query = query.filter(CalendarEvent.date <= end[:10])
    
    events = query.all()
    formatted = []
    
    zone_colors = {
        "Z1 - Recovery": "#10b981",    # Emerald
        "Z2 - Endurance": "#3b82f6",   # Blue
        "Z3 - Tempo": "#eab308",       # Yellow
        "Z4 - Threshold": "#f97316",   # Orange
        "Z5 - VO2Max": "#ef4444",      # Red
        "Z6 - Anaerobic": "#a855f7",   # Purple
        "Free Ride": "#06b6d4",        # Cyan
        "Rest Day": "#6b7280",         # Gray
        "Unplanned Ride": "#ec4899",   # Pink
        "Outdoor Ride": "#fc4c02"      # Strava Orange
    }

    for ev in events:
        color = zone_colors.get(ev.primary_zone, "#3b82f6")
        border_color = color
        
        if ev.event_type == "race":
            prio = (ev.race_priority or "A").upper()
            if prio == "A":
                title_prefix = "🏆 [A-Race] "
                color = "#d97706"
                border_color = "#f59e0b"
            elif prio == "B":
                title_prefix = "🏁 [B-Race] "
                color = "#7c3aed"
                border_color = "#a78bfa"
            else:
                title_prefix = "🎖️ [C-Race] "
                color = "#0284c7"
                border_color = "#38bdf8"
        elif ev.event_type == "outdoor_ride" or ev.primary_zone == "Outdoor Ride":
            title_prefix = "🚴‍♂️ [Outdoor] "
            color = "#fc4c02" # Strava orange
            border_color = "#e34000"
        elif ev.status == "completed":
            title_prefix = "✓ "
        elif ev.status == "skipped":
            title_prefix = "✕ "
            color = "#4b5563"
            border_color = "#4b5563"
        elif ev.status == "rest" or ev.event_type == "rest":
            title_prefix = "☕ "
            color = "#374151"
            border_color = "#374151"
        else:
            title_prefix = ""

        formatted.append({
            "id": ev.id,
            "title": f"{title_prefix}{ev.title}" + (f" ({ev.planned_tss or ev.actual_tss or 0} TSS)" if (ev.planned_tss or ev.actual_tss) else ""),
            "start": ev.date,
            "backgroundColor": color,
            "borderColor": border_color,
            "textColor": "#ffffff",
            "extendedProps": {
                "db_id": ev.id,
                "athlete_id": ev.athlete_id,
                "event_type": ev.event_type or "workout",
                "is_manual": bool(ev.is_manual),
                "is_outdoor": ev.event_type == "outdoor_ride" or ev.primary_zone == "Outdoor Ride",
                "ride_source": "strava" if (ev.activity_id and "strava" in ev.activity_id) else ("zwift" if (ev.activity_id and "zwift" in ev.activity_id) else "mywhoosh"),
                "race_priority": ev.race_priority,
                "race_type": ev.race_type,
                "target_distance_km": ev.target_distance_km,
                "workout_id": ev.workout_id,
                "workout_title": ev.title,
                "primary_zone": ev.primary_zone,
                "status": ev.status,
                "planned_duration_minutes": ev.planned_duration_minutes,
                "planned_tss": ev.planned_tss,
                "actual_duration_minutes": ev.actual_duration_minutes,
                "actual_tss": ev.actual_tss,
                "actual_avg_watts": ev.actual_avg_watts,
                "actual_avg_hr": ev.actual_avg_hr,
                "coach_notes": ev.coach_notes,
                "athlete_notes": ev.athlete_notes,
                "activity_id": ev.activity_id
            }
        })
    return formatted

@app.get("/api/races")
def get_target_races(athlete_id: int = Query(1), db: Session = Depends(get_db)):
    """List all scheduled target races ordered chronologically for athlete."""
    races = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == athlete_id,
        CalendarEvent.event_type == "race"
    ).order_by(CalendarEvent.date.asc()).all()
    return races

@app.post("/api/calendar")
def create_calendar_event(event: EventCreate, db: Session = Depends(get_db)):
    aid = event.athlete_id or 1
    ev_type = event.event_type or ("race" if event.race_priority else ("rest" if "rest" in event.title.lower() else "workout"))
    is_rest = ev_type == "rest" or "rest" in event.title.lower()
    ev = CalendarEvent(
        athlete_id=aid,
        date=event.date,
        title=event.title,
        workout_id=event.workout_id,
        event_type=ev_type,
        is_manual=True,
        race_priority=event.race_priority,
        race_type=event.race_type,
        target_distance_km=event.target_distance_km,
        primary_zone=event.primary_zone or ("Rest Day" if is_rest else ("Race" if ev_type == "race" else "Z2 - Endurance")),
        planned_duration_minutes=0.0 if is_rest else event.planned_duration_minutes,
        planned_tss=0 if is_rest else event.planned_tss,
        status="rest" if is_rest else "scheduled",
        coach_notes=event.coach_notes,
        athlete_notes=event.athlete_notes
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev

@app.put("/api/calendar/{event_id}")
def update_calendar_event(event_id: int, update: EventUpdate, db: Session = Depends(get_db)):
    ev = db.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")
    
    for k, v in update.dict(exclude_unset=True).items():
        setattr(ev, k, v)
    
    ev.is_manual = True
    db.commit()
    db.refresh(ev)
    return ev

@app.delete("/api/calendar/{event_id}")
def delete_calendar_event(event_id: int, db: Session = Depends(get_db)):
    ev = db.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")
    db.delete(ev)
    db.commit()
    return {"success": True}

# ----------------- WORKOUT CATALOG API -----------------

@app.get("/api/workouts")
def search_workouts(
    zone: Optional[str] = None,
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    min_tss: Optional[int] = None,
    max_tss: Optional[int] = None,
    min_dur: Optional[int] = None,
    max_dur: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    query = db.query(Workout)
    if zone:
        query = query.filter(Workout.primary_zone == zone)
    if category:
        query = query.filter(or_(Workout.category == category, Workout.all_categories.contains(category)))
    if keyword:
        query = query.filter(or_(
            Workout.title.ilike(f"%{keyword}%"),
            Workout.description.ilike(f"%{keyword}%")
        ))
    if min_tss is not None:
        query = query.filter(Workout.tss >= min_tss)
    if max_tss is not None:
        query = query.filter(Workout.tss <= max_tss)
    if min_dur is not None:
        query = query.filter(Workout.duration_minutes >= min_dur)
    if max_dur is not None:
        query = query.filter(Workout.duration_minutes <= max_dur)

    total = query.count()
    workouts = query.order_by(Workout.title.asc()).offset(offset).limit(limit).all()
    return {"total": total, "workouts": workouts}

@app.get("/api/workouts/{workout_id}")
def get_workout(workout_id: int, db: Session = Depends(get_db)):
    w = db.query(Workout).filter(Workout.id == workout_id).first()
    if not w:
        raise HTTPException(status_code=404, detail="Workout not found")
    return w

@app.get("/api/workouts/{event_id}/export-zwo")
def export_workout_zwo(event_id: int, db: Session = Depends(get_db)):
    """Export calendar event as Zwift XML workout file (.zwo)."""
    ev = db.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Calendar event not found")
    
    duration = ev.actual_duration_minutes or ev.planned_duration_minutes or 60.0
    zone = ev.primary_zone or "Z2 - Endurance"
    zwo_xml = zwift_service.generate_zwo(
        title=ev.title,
        description=ev.coach_notes or ev.athlete_notes or f"{zone} workout periodized by WhooshCoach AI",
        duration_minutes=duration,
        zone=zone
    )
    safe_filename = "".join(c for c in ev.title if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
    filename = f"{safe_filename or 'workout'}.zwo"
    return Response(
        content=zwo_xml,
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

# ----------------- AI COACH API -----------------

@app.post("/api/coach/generate-plan")
async def api_generate_plan(plan_req: PlanRequest, db: Session = Depends(get_db)):
    aid = plan_req.athlete_id or 1
    print(f"[API] Generating plan for athlete {aid} starting {plan_req.start_date} ({plan_req.days} days, overwrite={plan_req.overwrite_existing})", flush=True)
    res = await generate_training_plan(
        db=db,
        start_date=plan_req.start_date,
        days=plan_req.days,
        user_instructions=plan_req.user_instructions or "",
        overwrite_existing=plan_req.overwrite_existing,
        athlete_id=aid
    )
    if "error" in res:
        print(f"[API ERROR] Plan generation failed: {res['error']}", flush=True)
        raise HTTPException(status_code=400, detail=res["error"])
    return res

@app.post("/api/coach/test")
def api_test_gemini(req: Optional[GeminiTestRequest] = None, athlete_id: int = Query(1), db: Session = Depends(get_db)):
    """Tests Gemini API key and model connectivity with latency measurement."""
    aid = (req.athlete_id if req and req.athlete_id else None) or athlete_id
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == aid).first()
    api_key = (req.gemini_api_key if req and req.gemini_api_key else None) or (profile.gemini_api_key if profile else None)
    model = (req.gemini_model if req and req.gemini_model else None) or (profile.gemini_model if profile and profile.gemini_model else None)
    
    res = test_gemini_connection(api_key=api_key, model=model)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message", "Gemini test failed"))
    return res

@app.post("/api/coach/chat")
async def api_coach_chat(chat_req: ChatRequest, db: Session = Depends(get_db)):
    aid = chat_req.athlete_id or 1
    reply = await chat_with_coach(db, chat_req.message, athlete_id=aid)
    return {"reply": reply}

@app.get("/api/coach/logs")
def get_coach_logs(athlete_id: int = Query(1), limit: int = 20, db: Session = Depends(get_db)):
    logs = db.query(CoachLog).filter(
        CoachLog.athlete_id == athlete_id
    ).order_by(CoachLog.timestamp.desc()).limit(limit).all()
    return logs

# ----------------- UNIFIED & PLATFORM SYNC API -----------------

@app.post("/api/sync")
async def sync_athlete_activities(athlete_id: int = Query(1), db: Session = Depends(get_db)):
    """Dynamically syncs activities for athlete based on configured platform (mywhoosh or zwift)."""
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Athlete not found")

    platform = getattr(profile, "platform", "mywhoosh") or "mywhoosh"
    logger.info(f"Triggering sync for '{profile.name}' (ID: {profile.id}, Platform: {platform})...")

    results = {}
    if platform in ["mywhoosh", "both"]:
        if not profile.mywhoosh_email or not profile.mywhoosh_password:
            results["mywhoosh"] = {"status": "skipped", "message": "MyWhoosh email/password not configured."}
        else:
            res_mw = await mywhoosh_service.sync_activities(db, profile)
            results["mywhoosh"] = res_mw

    if platform in ["zwift", "both"]:
        if not profile.zwift_username or not profile.zwift_password:
            results["zwift"] = {"status": "skipped", "message": "Zwift username/password not configured."}
        else:
            res_zw = await zwift_service.sync_activities(db, profile)
            results["zwift"] = res_zw

    primary_res = results.get(platform) or results.get("mywhoosh") or results.get("zwift") or {"status": "success", "new_activities": 0}
    if primary_res.get("status") == "error":
        raise HTTPException(status_code=400, detail=primary_res.get("message", "Platform sync failed"))

    return {
        "status": "success",
        "platform": platform,
        "athlete_name": profile.name,
        "results": results,
        "new_activities": sum(r.get("new_activities", 0) for r in results.values() if isinstance(r, dict)),
        "cleaned_past_workouts": sum(r.get("cleaned_past_workouts", 0) for r in results.values() if isinstance(r, dict)),
        "last_sync": profile.last_zwift_sync if platform == "zwift" else profile.last_mywhoosh_sync,
        "adaptation": primary_res.get("adaptation")
    }

# Specific MyWhoosh endpoints
@app.post("/api/mywhoosh/sync")
async def manual_mywhoosh_sync(athlete_id: int = Query(1), db: Session = Depends(get_db)):
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile or not profile.mywhoosh_email or not profile.mywhoosh_password:
        raise HTTPException(status_code=400, detail="MyWhoosh email and password are not configured in Settings.")
    logger.info(f"Triggering manual MyWhoosh sync for '{profile.mywhoosh_email}'...")
    res = await mywhoosh_service.sync_activities(db, profile)
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message", "MyWhoosh sync failed"))
    return res

@app.post("/api/mywhoosh/test")
async def test_mywhoosh_connection(athlete_id: int = Query(1), db: Session = Depends(get_db)):
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile or not profile.mywhoosh_email or not profile.mywhoosh_password:
        raise HTTPException(status_code=400, detail="MyWhoosh email and password are not configured.")
    logger.info(f"Testing MyWhoosh authentication for '{profile.mywhoosh_email}'...")
    success, token, whoosh_id, err = await mywhoosh_service.login(profile.mywhoosh_email, profile.mywhoosh_password)
    if not success:
        raise HTTPException(status_code=400, detail=f"MyWhoosh authentication failed: {err}")
    profile.mywhoosh_token = token
    profile.mywhoosh_id = whoosh_id
    db.commit()
    return {"success": True, "whoosh_id": whoosh_id, "message": "Successfully authenticated with MyWhoosh!"}

# Specific Zwift endpoints
@app.post("/api/zwift/sync")
async def manual_zwift_sync(athlete_id: int = Query(1), db: Session = Depends(get_db)):
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile or not profile.zwift_username or not profile.zwift_password:
        raise HTTPException(status_code=400, detail="Zwift username and password are not configured in Settings.")
    logger.info(f"Triggering manual Zwift sync for '{profile.zwift_username}'...")
    res = await zwift_service.sync_activities(db, profile)
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message", "Zwift sync failed"))
    return res

@app.post("/api/zwift/test")
async def test_zwift_connection(req: Optional[ZwiftTestRequest] = None, athlete_id: int = Query(1), db: Session = Depends(get_db)):
    aid = (req.athlete_id if req and req.athlete_id else None) or athlete_id
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == aid).first()
    username = (req.username if req and req.username else None) or (profile.zwift_username if profile else None)
    password = (req.password if req and req.password else None) or (profile.zwift_password if profile else None)
    if not username or not password:
        raise HTTPException(status_code=400, detail="Zwift username and password are required.")
    
    success, token, zwift_id, err = await zwift_service.login(username, password)
    if not success:
        raise HTTPException(status_code=400, detail=f"Zwift authentication failed: {err}")
    
    if profile:
        profile.zwift_username = username
        profile.zwift_password = password
        profile.zwift_token = token
        profile.zwift_id = zwift_id
        db.commit()
    return {"success": True, "zwift_id": zwift_id, "message": f"Successfully authenticated with Zwift! (Profile ID: {zwift_id})"}

# ----------------- SYSTEM LOGS API -----------------

@app.get("/api/system/logs")
def get_system_logs(limit: int = 100):
    """Returns recent application logs for debugging."""
    logs = list(recent_logs)
    return logs[-limit:]

# ----------------- PROFILE & SETTINGS -----------------

@app.get("/api/settings")
def get_settings(athlete_id: int = Query(1), db: Session = Depends(get_db)):
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile:
        profile = AthleteProfile(id=athlete_id, name=f"Athlete {athlete_id}")
        db.add(profile)
        db.commit()
    
    return {
        "id": profile.id,
        "name": profile.name,
        "platform": getattr(profile, "platform", "mywhoosh") or "mywhoosh",
        "ftp": profile.ftp,
        "max_hr": profile.max_hr,
        "weight_kg": profile.weight_kg,
        "coaching_mode": getattr(profile, "coaching_mode", "autonomous") or "autonomous",
        "target_weekly_hours": profile.target_weekly_hours,
        "target_weekly_tss": profile.target_weekly_tss,
        "primary_goal": profile.primary_goal,
        "available_days": profile.available_days,
        "has_gemini_key": bool(profile.gemini_api_key),
        "gemini_model": profile.gemini_model or "gemini-3.6-flash",
        # MyWhoosh
        "mywhoosh_email": profile.mywhoosh_email or "",
        "has_mywhoosh_password": bool(profile.mywhoosh_password),
        "is_mywhoosh_connected": bool(profile.mywhoosh_token or (profile.mywhoosh_email and profile.mywhoosh_password)),
        "last_mywhoosh_sync": profile.last_mywhoosh_sync,
        # Zwift
        "zwift_username": profile.zwift_username or "",
        "has_zwift_password": bool(profile.zwift_password),
        "is_zwift_connected": bool(profile.zwift_token or (profile.zwift_username and profile.zwift_password)),
        "last_zwift_sync": profile.last_zwift_sync
    }

@app.post("/api/settings")
def update_settings(update: ProfileUpdate, athlete_id: int = Query(1), db: Session = Depends(get_db)):
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile:
        profile = AthleteProfile(id=athlete_id, name=f"Athlete {athlete_id}")
        db.add(profile)
    
    update_data = update.dict(exclude_unset=True)
    
    # Reset tokens if passwords changed
    if "mywhoosh_password" in update_data and update_data["mywhoosh_password"]:
        profile.mywhoosh_token = None
    if "zwift_password" in update_data and update_data["zwift_password"]:
        profile.zwift_token = None

    for k, v in update_data.items():
        if k in ["target_weekly_hours", "target_weekly_tss"]:
            setattr(profile, k, v)
        elif v is not None and v != "":
            setattr(profile, k, v)
    
    db.commit()
    return {"success": True}

# ----------------- METRICS & STATS -----------------

@app.get("/api/stats")
def get_dashboard_stats(athlete_id: int = Query(1), db: Session = Depends(get_db)):
    today = datetime.date.today()
    start_of_week = today - datetime.timedelta(days=today.weekday())
    end_of_week = start_of_week + datetime.timedelta(days=6)
    
    week_events = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == athlete_id,
        CalendarEvent.date >= start_of_week.strftime("%Y-%m-%d"),
        CalendarEvent.date <= end_of_week.strftime("%Y-%m-%d")
    ).all()

    planned_tss = sum(e.planned_tss or 0 for e in week_events if e.status != "skipped")
    completed_tss = sum(e.actual_tss or e.planned_tss or 0 for e in week_events if e.status == "completed")
    completed_hours = sum((e.actual_duration_minutes or e.planned_duration_minutes or 0) / 60.0 for e in week_events if e.status == "completed")
    completed_rides = sum(1 for e in week_events if e.status == "completed")
    completed_indoor_rides = sum(1 for e in week_events if e.status == "completed" and e.event_type != "outdoor_ride" and e.primary_zone != "Outdoor Ride")
    completed_outdoor_rides = sum(1 for e in week_events if e.status == "completed" and (e.event_type == "outdoor_ride" or e.primary_zone == "Outdoor Ride"))

    # Today's session for this athlete
    today_str = today.strftime("%Y-%m-%d")
    today_event = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == athlete_id,
        CalendarEvent.date == today_str
    ).first()
    today_data = None
    if today_event:
        w_obj = None
        if today_event.workout_id:
            w_obj = db.query(Workout).filter(Workout.id == today_event.workout_id).first()
        today_data = {
            "id": today_event.id,
            "title": today_event.title,
            "primary_zone": today_event.primary_zone,
            "planned_duration_minutes": today_event.planned_duration_minutes,
            "planned_tss": today_event.planned_tss,
            "status": today_event.status,
            "coach_notes": today_event.coach_notes,
            "workout": {
                "id": w_obj.id,
                "description": w_obj.description,
                "workout_steps": w_obj.workout_steps,
                "duration": w_obj.duration,
                "url": w_obj.url
            } if w_obj else None
        }

    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()

    return {
        "start_of_week": start_of_week.strftime("%Y-%m-%d"),
        "end_of_week": end_of_week.strftime("%Y-%m-%d"),
        "planned_tss": planned_tss,
        "completed_tss": completed_tss,
        "completed_hours": round(completed_hours, 1),
        "completed_rides": completed_rides,
        "completed_indoor_rides": completed_indoor_rides,
        "completed_outdoor_rides": completed_outdoor_rides,
        "today_workout": today_data,
        "athlete_name": profile.name if profile else f"Athlete {athlete_id}",
        "platform": getattr(profile, "platform", "mywhoosh") if profile else "mywhoosh",
        "coaching_mode": getattr(profile, "coaching_mode", "autonomous") if profile else "autonomous",
        "target_weekly_tss": profile.target_weekly_tss if profile else None,
        "target_weekly_hours": profile.target_weekly_hours if profile else None
    }
