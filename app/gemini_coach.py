import datetime
import json
import logging
import os
import re
import time
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.database import AthleteProfile, Workout, CalendarEvent, RideActivity, CoachLog

logger = logging.getLogger("whooshcoach.coach")

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"]

def get_gemini_client(api_key: Optional[str] = None):
    """Initializes the google-genai client."""
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=key)
    except Exception as e:
        logger.error(f"Failed to initialize google-genai client: {e}")
        return None

def generate_content_with_fallback(
    client,
    preferred_model: str,
    contents: Any,
    config: Optional[Dict[str, Any]] = None
) -> Tuple[Any, str, bool]:
    """
    Calls client.models.generate_content using preferred_model.
    If 429 (Resource Exhausted/Quota), 404 (Model Not Found), or 503 (Unavailable) occurs,
    automatically falls back to alternative models in FALLBACK_MODELS.
    Returns: (response, model_used, fallback_occurred)
    """
    # Map any legacy or deprecated model names to active official models
    model_alias_map = {
        "gemini-3.6-flash": "gemini-3.5-flash",
        "gemini-2.5-flash": "gemini-3.5-flash",
        "gemini-2.0-flash": "gemini-3.5-flash",
        "gemini-1.5-flash": "gemini-3.5-flash",
        "gemini-2.5-flash-lite": "gemini-3.5-flash-lite",
    }
    normalized_preferred = model_alias_map.get(preferred_model, preferred_model)

    candidates = [normalized_preferred]
    for m in FALLBACK_MODELS:
        if m not in candidates:
            candidates.append(m)

    last_error = None
    for idx, model_name in enumerate(candidates):
        for attempt in range(2):
            try:
                print(f"[Gemini] Querying model '{model_name}' (attempt {attempt+1})...", flush=True)
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config
                )
                fallback_occurred = (model_name != preferred_model)
                if fallback_occurred:
                    print(f"[Gemini] Fallback active: Successfully used '{model_name}' (preferred was '{preferred_model}').", flush=True)
                return response, model_name, fallback_occurred
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                is_recoverable = any(code in err_str for code in [
                    "429", "resource_exhausted", "quota", "rate_limit", "rate limit",
                    "404", "not_found", "no longer available", "503", "unavailable", "overloaded"
                ])
                print(f"[Gemini] Model '{model_name}' error (attempt {attempt+1}): {e}", flush=True)
                if "503" in err_str and attempt == 0:
                    time.sleep(1.5)
                    continue
                break

        if idx < len(candidates) - 1:
            next_model = candidates[idx + 1]
            print(f"[Gemini] Falling back from '{model_name}' to '{next_model}'...", flush=True)
            continue
        else:
            print(f"[Gemini API ERROR] All candidate models exhausted. Last error: {last_error}", flush=True)
            raise last_error

    raise last_error

def test_gemini_connection(api_key: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
    """Tests Gemini API key credentials and measures latency against the requested model."""
    client = get_gemini_client(api_key)
    if not client:
        return {
            "success": False,
            "message": "Gemini API key is not configured or could not initialize client."
        }

    model_to_test = model or DEFAULT_MODEL
    t0 = time.time()
    try:
        response, used_model, fallback = generate_content_with_fallback(
            client=client,
            preferred_model=model_to_test,
            contents="Hello! Confirm connection by replying with exactly: OK - Coach Online",
            config={"max_output_tokens": 50}
        )
        latency_ms = int((time.time() - t0) * 1000)
        fb_text = f" (fallback from {model_to_test})" if fallback else ""
        return {
            "success": True,
            "model": used_model,
            "latency_ms": latency_ms,
            "fallback_used": fallback,
            "reply": response.text.strip() if response and response.text else "OK",
            "message": f"Successfully connected to {used_model}{fb_text} in {latency_ms}ms!"
        }
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        return {
            "success": False,
            "model": model_to_test,
            "latency_ms": latency_ms,
            "message": f"Connection failed: {str(e)}"
        }

def get_athlete_context(db: Session, athlete_id: int = 1) -> str:
    """Builds a comprehensive context string of athlete profile, fitness metrics, and recent ride history."""
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile:
        profile = db.query(AthleteProfile).filter(AthleteProfile.id == 1).first()
    if not profile:
        return "Athlete profile: Standard cyclist, FTP 220W, Max HR 185."

    # Fetch last 10 completed activities from RideActivity for this athlete
    recent_rides = db.query(RideActivity).filter(
        RideActivity.athlete_id == profile.id
    ).order_by(RideActivity.start_date_local.desc()).limit(10).all()
    recent_events = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == profile.id,
        CalendarEvent.status == "completed"
    ).order_by(CalendarEvent.date.desc()).limit(10).all()

    ride_log_lines = []
    total_recent_minutes = 0.0
    total_recent_tss = 0
    indoor_count = 0
    outdoor_count = 0

    if recent_rides:
        for r in recent_rides:
            dur_min = round(r.moving_time / 60) if r.moving_time else 0
            total_recent_minutes += dur_min
            if r.calculated_tss:
                total_recent_tss += r.calculated_tss
            power_str = f", Avg Power: {r.average_watts:.0f}W" if r.average_watts else ""
            hr_str = f", Avg HR: {r.average_heartrate:.0f}bpm" if r.average_heartrate else ""
            dist_str = f", Dist: {r.distance/1000.0:.1f}km" if (r.distance and r.distance > 100) else (f", Dist: {r.distance:.1f}km" if r.distance else "")
            date_only = r.start_date_local[:10] if r.start_date_local else (r.start_date[:10] if r.start_date else "Recent")
            
            is_outdoor = (r.type == "Ride" or (r.id and "strava" in r.id))
            if is_outdoor:
                outdoor_count += 1
                type_tag = "[Outdoor Strava]"
            else:
                indoor_count += 1
                type_tag = "[Indoor MyWhoosh]" if (r.id and "mw" in r.id) else "[Indoor Virtual]"

            ride_log_lines.append(f"- {date_only} {type_tag}: \"{r.name}\" ({dur_min} min{dist_str}, TSS: {r.calculated_tss or 'N/A'}{power_str}{hr_str})")
    elif recent_events:
        for e in recent_events:
            dur_min = e.actual_duration_minutes or 0.0
            total_recent_minutes += dur_min
            if e.actual_tss:
                total_recent_tss += e.actual_tss
            is_outdoor = (e.event_type == "outdoor_ride" or e.primary_zone == "Outdoor Ride")
            type_tag = "[Outdoor Strava]" if is_outdoor else "[Indoor Workout]"
            if is_outdoor:
                outdoor_count += 1
            else:
                indoor_count += 1
            ride_log_lines.append(f"- {e.date} {type_tag}: \"{e.title}\" ({dur_min:.0f} min, TSS: {e.actual_tss or 0})")

    recent_history_block = "\n".join(ride_log_lines) if ride_log_lines else "No recent completed rides logged yet."
    baseline_stats_line = (
        f"- Recent 14-Day Baseline Load: {len(recent_rides or recent_events)} completed rides "
        f"({indoor_count} indoor virtual, {outdoor_count} outdoor Strava, {round(total_recent_minutes/60, 1)} hrs, ~{total_recent_tss} TSS combined).\n"
        f"- Environment Context: Outdoor rides carry real road resistance, wind, and heat fatigue. "
        f"Never confuse indoor trainer workouts with outdoor rides. Respect recovery needs following outdoor volume."
    )

    coaching_mode = getattr(profile, "coaching_mode", "autonomous") or "autonomous"
    if coaching_mode == "autonomous" or not profile.target_weekly_tss:
        coaching_directive = (
            "- Coaching Mode: AUTONOMOUS AI (Context-Driven)\n"
            "  * The athlete does NOT set fixed weekly hours or TSS.\n"
            "  * You (the AI coach) have full discretion to determine workout durations, session TSS, and overall weekly volume.\n"
            "  * Base your decisions on: baseline load from recent completed rides, proximity/priority of upcoming races, and recovery needs."
        )
    else:
        coaching_directive = (
            f"- Coaching Mode: TARGET DRIVEN (Athlete specified weekly targets: {profile.target_weekly_hours or 'N/A'} hrs / {profile.target_weekly_tss or 'N/A'} TSS)"
        )

    context = f"""
ATHLETE PROFILE & METRICS:
- Name: {profile.name}
- Current FTP: {profile.ftp} W
- Max Heart Rate: {profile.max_hr} bpm
- Body Weight: {profile.weight_kg} kg
{coaching_directive}
- Primary Training Goal: {profile.primary_goal}
- Available Training Days: {profile.available_days}
{baseline_stats_line}

RECENT COMPLETED RIDE HISTORY (PAST ACTIVITIES):
{recent_history_block}
"""
    return context.strip()

def get_workout_catalog_sample(db: Session, max_per_zone: int = 20) -> str:
    """Generates a diverse catalog of available MyWhoosh workouts grouped by zone."""
    zones = ["Z1 - Recovery", "Z2 - Endurance", "Z3 - Tempo", "Z4 - Threshold", "Z5 - VO2Max", "Z6 - Anaerobic"]
    catalog_lines = []
    
    for z in zones:
        workouts = db.query(Workout).filter(Workout.primary_zone == z).order_by(Workout.duration_minutes.asc()).limit(max_per_zone).all()
        catalog_lines.append(f"### ZONE: {z}")
        for w in workouts:
            catalog_lines.append(
                f"- ID:{w.id} | \"{w.title}\" | {w.duration} ({w.duration_minutes:.0f}m) | {w.tss} TSS | IF:{w.intensity_factor} | Cat:{w.category}"
            )
        catalog_lines.append("")

    return "\n".join(catalog_lines)

async def generate_training_plan(
    db: Session,
    start_date: str,
    days: int = 7,
    user_instructions: str = "",
    overwrite_existing: bool = True,
    athlete_id: int = 1
) -> Dict[str, Any]:
    """Uses Gemini to generate a structured workout calendar based on workouts, periodized for upcoming races."""
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile:
        profile = db.query(AthleteProfile).filter(AthleteProfile.id == 1).first()
    client = get_gemini_client(profile.gemini_api_key if profile else None)
    if not client:
        return {"error": "Gemini API key is not configured. Please set it in Settings."}

    preferred_model = (profile.gemini_model if profile and profile.gemini_model else DEFAULT_MODEL)
    athlete_ctx = get_athlete_context(db, athlete_id=profile.id if profile else 1)
    catalog = get_workout_catalog_sample(db, max_per_zone=20)

    # Compute end date of the planning window
    start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = start_dt + datetime.timedelta(days=days)
    end_date_str = end_dt.strftime("%Y-%m-%d")

    # If overwrite requested, clean uncompleted AI-generated events in this date window for this athlete BEFORE building context!
    # STRICT PRESERVATION: Completed rides, target races, custom rides, and athlete manual rest days are NEVER deleted!
    if overwrite_existing:
        db.query(CalendarEvent).filter(
            CalendarEvent.athlete_id == profile.id,
            CalendarEvent.date >= start_date,
            CalendarEvent.date < end_date_str,
            CalendarEvent.status != "completed",
            or_(CalendarEvent.is_manual == False, CalendarEvent.is_manual.is_(None)),
            CalendarEvent.event_type.notin_(["race", "custom"])
        ).delete(synchronize_session=False)
        db.commit()

    # Fetch upcoming target races (in and beyond this block for forward-looking periodization)
    upcoming_races = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == profile.id,
        CalendarEvent.event_type == "race",
        CalendarEvent.date >= start_date
    ).order_by(CalendarEvent.date.asc()).all()

    race_descriptions = []
    if upcoming_races:
        for r in upcoming_races:
            prio = f"Priority {r.race_priority}-Race" if r.race_priority else "Target Race"
            rtype = f"Type: {r.race_type}" if r.race_type else "Cycling Race"
            dist = f"Distance: {r.target_distance_km:.0f}km" if r.target_distance_km else ""
            notes = f"Strategy/Goal: {r.athlete_notes}" if r.athlete_notes else ""
            details = ", ".join([x for x in [prio, rtype, dist, notes] if x])
            race_descriptions.append(f"- {r.date}: \"{r.title}\" ({details})")
    races_block = "\n".join(race_descriptions) if race_descriptions else "No target races currently scheduled on the calendar."

    # Fetch athlete's manually scheduled events / rest days in this planning window
    manual_events = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == profile.id,
        CalendarEvent.date >= start_date,
        CalendarEvent.date < end_date_str,
        CalendarEvent.is_manual == True
    ).order_by(CalendarEvent.date.asc()).all()

    manual_rest_dates = [
        ev.date for ev in manual_events 
        if ev.event_type == "rest" or ev.status == "rest" or "rest" in (ev.title or "").lower()
    ]
    if manual_rest_dates:
        manual_rest_block = "\n".join([f"- {d}: ATHLETE-COMMITTED REST DAY (Strictly non-negotiable recovery day. Do NOT schedule any workout on this date.)" for d in manual_rest_dates])
    else:
        manual_rest_block = "None specified by athlete in this date window."

    # Fetch existing scheduled rides or rest days in this planning window (committed / completed / manual)
    existing_events = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == profile.id,
        CalendarEvent.date >= start_date,
        CalendarEvent.date < end_date_str
    ).order_by(CalendarEvent.date.asc()).all()

    existing_event_lines = []
    for ev in existing_events:
        if ev.event_type == "race":
            continue  # Covered in races block
        if ev.date in manual_rest_dates:
            continue  # Covered in manual rest days block
        ev_type_str = f"[{ev.event_type.upper()}]" if ev.event_type else "[WORKOUT]"
        manual_tag = " (Athlete Manual)" if ev.is_manual else ""
        existing_event_lines.append(f"- {ev.date}: {ev_type_str}{manual_tag} \"{ev.title}\" ({ev.planned_tss or 0} TSS, Status: {ev.status})")
    existing_cal_block = "\n".join(existing_event_lines) if existing_event_lines else "No other pre-existing rides or commitments in this date range."

    # Parse available training days from athlete profile
    raw_available = (profile.available_days or "") if profile else ""
    available_days_list = [d.strip() for d in raw_available.split(",") if d.strip()]
    available_days_normalized = set(d.lower() for d in available_days_list)
    available_days_display = ", ".join(available_days_list) if available_days_list else "Every day (all days available)"

    # Build explicit day-by-day availability calendar schedule for prompt
    calendar_schedule_lines = []
    for i in range(days):
        curr_dt = start_dt + datetime.timedelta(days=i)
        curr_date_str = curr_dt.strftime("%Y-%m-%d")
        weekday_name = curr_dt.strftime("%A")

        if curr_date_str in manual_rest_dates:
            calendar_schedule_lines.append(
                f"- {curr_date_str} ({weekday_name}): ATHLETE MANUAL REST DAY -> MANDATORY REST DAY (0 TSS, workout_id=null, status='rest')"
            )
        elif any(r.date == curr_date_str for r in upcoming_races):
            matching_race = next(r for r in upcoming_races if r.date == curr_date_str)
            prio = f"Priority {matching_race.race_priority} " if matching_race.race_priority else ""
            calendar_schedule_lines.append(
                f"- {curr_date_str} ({weekday_name}): TARGET RACE DAY -> {prio}\"{matching_race.title}\" (Do NOT schedule a conflicting workout)"
            )
        elif available_days_normalized and weekday_name.lower() not in available_days_normalized:
            calendar_schedule_lines.append(
                f"- {curr_date_str} ({weekday_name}): NON-AVAILABLE TRAINING DAY -> MANDATORY REST DAY (0 TSS, workout_id=null, status='rest')"
            )
        else:
            calendar_schedule_lines.append(
                f"- {curr_date_str} ({weekday_name}): AVAILABLE TRAINING DAY -> Assign workout or recovery session"
            )
    availability_calendar_block = "\n".join(calendar_schedule_lines)

    prompt = f"""
You are an elite endurance cycling coach creating a personalized, periodized training calendar.
Given the athlete's profile, recent completed ride history, existing calendar events, and upcoming target races, plan a {days}-day training schedule starting on {start_date}.

{athlete_ctx}

TARGET RACES & GOAL EVENTS ON CALENDAR:
{races_block}

ATHLETE COMMITTED REST DAYS (MANDATORY RECOVERY - NEVER OVERRIDE):
{manual_rest_block}

OTHER EXISTING COMMITTED EVENTS IN THIS DATE RANGE:
{existing_cal_block}

DAY-BY-DAY CALENDAR & ATHLETE AVAILABILITY:
Athlete's Configured Available Training Days: {available_days_display}
{availability_calendar_block}

USER SPECIAL INSTRUCTIONS / FOCUS:
{user_instructions or 'None provided. Focus on balanced progressive training tailored to their goals and upcoming races.'}

AVAILABLE MYWHOOSH WORKOUT CATALOG:
{catalog}

RULES FOR THE PLAN & PERIODIZATION:
1. STRICT DAY AVAILABILITY (CRITICAL RULE):
   - You may ONLY schedule cycling workouts on dates marked "AVAILABLE TRAINING DAY" in the DAY-BY-DAY CALENDAR above.
   - On every date marked "NON-AVAILABLE TRAINING DAY" or "ATHLETE MANUAL REST DAY", you MUST prescribe a Rest Day (title: "Rest Day", planned_duration_minutes: 0, planned_tss: 0, workout_id: null, status: "rest"). NEVER assign a workout on non-available or manual rest days.
   - Even if you want to prescribe extra training volume, you MUST fit all training sessions ONLY onto the available training days.
2. Every scheduled cycling workout on an AVAILABLE day MUST select a real workout from the catalog above with its exact ID, Title, and Zone.
3. AUTONOMOUS DURATION & TSS PRESCRIPTION:
   - You (the AI Coach) have full discretion and responsibility to decide the exact duration (minutes) and TSS for each session, and the resulting total weekly hours/TSS.
   - Do NOT try to force an arbitrary number. Determine the optimal physiological dose based on:
     * Baseline work capacity from recent completed rides.
     * Proximity, priority, and demands of upcoming races.
     * Fatigue management: ensure high-intensity sessions are paired with adequate rest or active recovery.
   - In "coach_overview", explicitly state your physiological rationale for the prescribed session durations, weekly volume, and TSS breakdown.
4. RACE PERIODIZATION RULES:
   - If an A-Race is scheduled within this block (or within 10 days after):
     * 5 to 7 days before an A-Race: Taper training volume by ~40-50% (short 30-45 min sessions) while preserving neuromuscular sharpness with short, crisp Z4/Z5 intervals.
     * Day before the race: Schedule a short 20-35 min pre-race activation/opener (Z1/Z2 with 2-3 micro-efforts) or rest.
     * Race Day: Acknowledge the race. Do not assign an exhausting conflicting workout.
     * Day after race: Prescribe complete rest or 30-min Z1 Active Recovery.
   - If a B-Race is scheduled:
     * Plan a 1-2 day mini-taper beforehand and 1 day of recovery after.
   - If a C-Race is scheduled:
     * Treat it as a hard training session without taper.
   - If no race is scheduled:
     * Progressively build base, tempo, sweetspot, or threshold according to the athlete's primary goal, keeping weekly ramp rates safe (~5-8% increase).
5. Provide a clear coaching rationale in coach_notes for each session.
6. The schedule array MUST include an entry for every single date from {start_date} to {(start_dt + datetime.timedelta(days=days-1)).strftime('%Y-%m-%d')} ({days} days in total).

OUTPUT FORMAT:
Respond with ONLY valid JSON adhering strictly to this schema:
{{
  "weekly_focus": "string summary of the training objective for this period (mentioning race preparation if applicable)",
  "total_planned_tss": integer,
  "total_planned_hours": float,
  "coach_overview": "2-3 sentences explaining the strategy and race periodization to the athlete",
  "schedule": [
    {{
      "date": "YYYY-MM-DD",
      "workout_id": integer or null,
      "title": "string title",
      "primary_zone": "string zone",
      "planned_duration_minutes": float,
      "planned_tss": integer,
      "status": "scheduled" or "rest",
      "coach_notes": "brief instruction for this specific session"
    }}
  ]
}}
"""

    try:
        response, model_used, fallback_used = generate_content_with_fallback(
            client=client,
            preferred_model=preferred_model,
            contents=prompt,
            config={
                "response_mime_type": "application/json"
            }
        )
        
        plan_data = json.loads(response.text)
        
        # Save generated events to database with strict guardrails
        raw_schedule = plan_data.get("schedule", [])
        schedule_by_date = {item.get("date"): item for item in raw_schedule if item.get("date")}
        saved_events = []
        total_calculated_tss = 0
        total_calculated_minutes = 0.0

        for i in range(days):
            curr_dt = start_dt + datetime.timedelta(days=i)
            event_date = curr_dt.strftime("%Y-%m-%d")
            ev_weekday = curr_dt.strftime("%A")

            # PRESERVE RACES, CUSTOM COMMITTED EVENTS, ATHLETE MANUAL REST DAYS, & COMPLETED RIDES
            existing_protected = db.query(CalendarEvent).filter(
                CalendarEvent.athlete_id == profile.id,
                CalendarEvent.date == event_date,
                or_(
                    CalendarEvent.event_type.in_(["race", "custom"]),
                    CalendarEvent.is_manual == True,
                    CalendarEvent.status == "completed"
                )
            ).first()
            if existing_protected:
                total_calculated_tss += (existing_protected.planned_tss or 0)
                total_calculated_minutes += (existing_protected.planned_duration_minutes or 0.0)
                saved_events.append(existing_protected)
                continue

            # If not already wiped by batch overwrite, clean non-completed AI placeholder on this specific day
            if not overwrite_existing:
                db.query(CalendarEvent).filter(
                    CalendarEvent.athlete_id == profile.id,
                    CalendarEvent.date == event_date,
                    CalendarEvent.status != "completed",
                    or_(CalendarEvent.is_manual == False, CalendarEvent.is_manual.is_(None)),
                    CalendarEvent.event_type.notin_(["race", "custom"])
                ).delete(synchronize_session=False)

            item = schedule_by_date.get(event_date)
            is_non_available = bool(available_days_normalized and ev_weekday.lower() not in available_days_normalized)
            is_manual_rest = event_date in manual_rest_dates

            if is_non_available or is_manual_rest:
                # Python Guardrail: Force to Rest Day
                event_title = "Rest Day"
                workout_id = None
                event_type = "rest"
                primary_zone = "Z1 - Recovery"
                planned_duration = 0.0
                planned_tss = 0
                event_status = "rest"
                if is_non_available:
                    coach_notes = (item.get("coach_notes") if item else "") or f"Scheduled rest: {ev_weekday} is not an available training day."
                else:
                    coach_notes = (item.get("coach_notes") if item else "") or "Scheduled rest: Athlete manual rest day."
            elif not item:
                # Not returned by Gemini -> Default to Rest Day
                event_title = "Rest Day"
                workout_id = None
                event_type = "rest"
                primary_zone = "Z1 - Recovery"
                planned_duration = 0.0
                planned_tss = 0
                event_status = "rest"
                coach_notes = "Rest day."
            else:
                raw_title = item.get("title", "Workout")
                raw_status = item.get("status", "scheduled")
                raw_wid = item.get("workout_id")
                is_rest_item = (raw_status == "rest" or "rest" in raw_title.lower() or raw_wid is None)

                if is_rest_item:
                    event_title = raw_title if "rest" in raw_title.lower() else "Rest Day"
                    workout_id = None
                    event_type = "rest"
                    primary_zone = "Z1 - Recovery"
                    planned_duration = 0.0
                    planned_tss = 0
                    event_status = "rest"
                    coach_notes = item.get("coach_notes", "Recovery day.")
                else:
                    event_title = raw_title
                    workout_id = raw_wid
                    event_type = "workout"
                    primary_zone = item.get("primary_zone", "Z2 - Endurance")
                    planned_duration = float(item.get("planned_duration_minutes", 60.0))
                    planned_tss = int(item.get("planned_tss", 50))
                    event_status = "scheduled"
                    coach_notes = item.get("coach_notes", "")

            event = CalendarEvent(
                athlete_id=profile.id,
                date=event_date,
                title=event_title,
                workout_id=workout_id,
                event_type=event_type,
                is_manual=False,
                primary_zone=primary_zone,
                planned_duration_minutes=planned_duration,
                planned_tss=planned_tss,
                status=event_status,
                coach_notes=coach_notes
            )
            db.add(event)
            saved_events.append(event)
            total_calculated_tss += planned_tss
            total_calculated_minutes += planned_duration

        total_calculated_hours = round(total_calculated_minutes / 60.0, 1)
        reported_tss = total_calculated_tss
        reported_hours = total_calculated_hours

        # Log coach plan generation
        fb_msg = f" (fallback from {preferred_model})" if fallback_used else ""
        log_entry = CoachLog(
            athlete_id=profile.id,
            role="assistant",
            message=f"Generated {days}-day plan via {model_used}{fb_msg}: {plan_data.get('weekly_focus')}. Overview: {plan_data.get('coach_overview')}",
            context_type="plan_generation"
        )
        db.add(log_entry)
        db.commit()

        print(f"[Coach] Generated {days}-day plan via {model_used}{fb_msg}: {len(saved_events)} events, {reported_tss} TSS ({reported_hours}h)", flush=True)

        return {
            "success": True,
            "start_date": start_date,
            "days": days,
            "model_used": model_used,
            "fallback_used": fallback_used,
            "weekly_focus": plan_data.get("weekly_focus"),
            "total_planned_tss": reported_tss,
            "total_planned_hours": reported_hours,
            "coach_overview": plan_data.get("coach_overview"),
            "scheduled_count": len(saved_events),
            "events": [
                {
                    "date": ev.date,
                    "title": ev.title,
                    "primary_zone": ev.primary_zone or "Z2 - Endurance",
                    "planned_duration_minutes": ev.planned_duration_minutes or 0,
                    "planned_tss": ev.planned_tss or 0,
                    "status": ev.status or "scheduled",
                    "event_type": ev.event_type or "workout",
                    "coach_notes": getattr(ev, "coach_notes", "") or ""
                }
                for ev in saved_events
            ]
        }

    except Exception as e:
        db.rollback()
        print(f"[Coach ERROR] generate_training_plan failed: {e}", flush=True)
        logger.error(f"Error generating training plan with Gemini: {e}")
        return {"error": f"Failed to generate training plan: {str(e)}"}

async def adapt_calendar_after_activity(
    db: Session,
    completed_event: CalendarEvent,
    activity: RideActivity,
    athlete_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Evaluates completed ride against plan and adapts upcoming calendar workouts if needed.
    """
    aid = athlete_id or completed_event.athlete_id or 1
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == aid).first()
    if not profile:
        profile = db.query(AthleteProfile).filter(AthleteProfile.id == 1).first()
    client = get_gemini_client(profile.gemini_api_key if profile else None)
    if not client:
        return {"adapted": False, "message": "Gemini API key not configured."}

    # Fetch upcoming scheduled events for the next 7 days for this athlete
    today_str = completed_event.date
    upcoming_events = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == aid,
        CalendarEvent.date > today_str,
        CalendarEvent.status == "scheduled"
    ).order_by(CalendarEvent.date.asc()).limit(7).all()

    if not upcoming_events:
        return {"adapted": False, "message": "No upcoming scheduled events to adapt."}

    upcoming_summary = [
        {
            "id": e.id,
            "date": e.date,
            "title": e.title,
            "type": e.event_type,
            "zone": e.primary_zone,
            "tss": e.planned_tss,
            "duration": e.planned_duration_minutes
        }
        for e in upcoming_events
    ]

    planned_tss = completed_event.planned_tss or 0
    actual_tss = completed_event.actual_tss or 0
    tss_diff = actual_tss - planned_tss

    is_outdoor_act = (activity.type == "Ride" or (activity.id and "strava" in activity.id) or completed_event.event_type == "outdoor_ride")
    ride_env_desc = "Outdoor Road/Gravel Ride (via Strava)" if is_outdoor_act else "Indoor Virtual Trainer Ride (via MyWhoosh)"

    prompt = f"""
You are an AI cycling coach monitoring an athlete's live training calendar.
An activity was just completed. Determine if upcoming training days need dynamic adjustments.

COMPLETED ACTIVITY DETAILS:
- Date: {completed_event.date}
- Workout Planned: "{completed_event.title}" ({completed_event.primary_zone}, Planned TSS: {planned_tss})
- Actual Completed Ride: "{activity.name}"
- Ride Environment: {ride_env_desc}
- Actual Duration: {round(activity.moving_time / 60, 1)} min
- Actual TSS: {actual_tss} TSS (Difference: {tss_diff:+d} TSS)
- Avg Power: {activity.average_watts or 'N/A'} W, Normalized: {activity.weighted_average_watts or 'N/A'} W
- Avg HR: {activity.average_heartrate or 'N/A'} bpm, Max HR: {activity.max_heartrate or 'N/A'} bpm
- Athlete FTP: {profile.ftp if profile else 220} W

UPCOMING SCHEDULED SESSIONS (NEXT 7 DAYS):
{json.dumps(upcoming_summary, indent=2)}

ADAPTATION GUIDELINES:
1. If actual TSS was significantly higher than planned (+30 TSS or hard group ride/race):
   - The immediate next day should be downgraded to an active recovery ride (Z1/Z2 low TSS) or complete rest day.
2. OUTDOOR RIDE FATIGUE: If the completed session was an outdoor ride, factor in muscular fatigue from road vibration, wind, and climbing. Avoid scheduling hard indoor threshold/VO2Max workouts the immediate next day.
3. If actual TSS was lower or workout was skipped:
   - Avoid cramming high intensity into consecutive days. Rebalance appropriately.
3. If actual workout matched plan reasonably (within +/- 15 TSS):
   - Keep schedule as is ("requires_adjustment": false).
4. IMMUTABLE SESSIONS: Races, custom events, and athlete-scheduled manual rest days (type 'race', 'custom', or is_manual=true) MUST NEVER be modified or converted to workouts.

OUTPUT FORMAT:
Respond with ONLY valid JSON adhering strictly to:
{{
  "requires_adjustment": boolean,
  "coach_debrief": "2-3 sentences providing feedback on the ride and explaining schedule adjustments",
  "adjustments": [
    {{
      "event_id": integer,
      "date": "YYYY-MM-DD",
      "new_title": "string title",
      "new_zone": "string zone",
      "new_tss": integer,
      "new_duration_minutes": float,
      "status": "modified" or "rest",
      "coach_reason": "why this session was modified"
    }}
  ]
}}
"""

    preferred_model = (profile.gemini_model if profile and profile.gemini_model else DEFAULT_MODEL)
    try:
        response, model_used, fallback_used = generate_content_with_fallback(
            client=client,
            preferred_model=preferred_model,
            contents=prompt,
            config={
                "response_mime_type": "application/json"
            }
        )
        
        result = json.loads(response.text)
        debrief = result.get("coach_debrief", "Great job getting the ride in!")
        completed_event.coach_notes = debrief
        
        if result.get("requires_adjustment") and result.get("adjustments"):
            for adj in result["adjustments"]:
                ev = db.query(CalendarEvent).filter(
                    CalendarEvent.athlete_id == aid,
                    CalendarEvent.id == adj["event_id"]
                ).first()
                if ev and ev.status == "scheduled" and not ev.is_manual and ev.event_type not in ["race", "custom", "rest"]:
                    ev.title = adj.get("new_title", ev.title)
                    ev.primary_zone = adj.get("new_zone", ev.primary_zone)
                    ev.planned_tss = adj.get("new_tss", ev.planned_tss)
                    ev.planned_duration_minutes = adj.get("new_duration_minutes", ev.planned_duration_minutes)
                    ev.status = adj.get("status", "modified")
                    ev.coach_notes = adj.get("coach_reason", "")
        
        # Log to coach logs
        fb_msg = f" via {model_used}" + (f" (fallback from {preferred_model})" if fallback_used else "")
        db.add(CoachLog(
            athlete_id=aid,
            role="assistant",
            message=f"Activity Adaptation for {completed_event.date}{fb_msg}: {debrief}",
            context_type="adaptation"
        ))
        db.commit()

        return {
            "adapted": result.get("requires_adjustment", False),
            "model_used": model_used,
            "fallback_used": fallback_used,
            "debrief": debrief,
            "adjustments": result.get("adjustments", [])
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Error in Gemini adaptation: {e}")
        return {"adapted": False, "error": str(e)}

async def chat_with_coach(db: Session, user_message: str, athlete_id: int = 1) -> str:
    """Conversational assistant for athlete training advice and questions."""
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile:
        profile = db.query(AthleteProfile).filter(AthleteProfile.id == 1).first()
    client = get_gemini_client(profile.gemini_api_key if profile else None)
    if not client:
        return "Please configure your Gemini API Key in Settings to chat with your AI Coach."

    athlete_ctx = get_athlete_context(db, athlete_id=profile.id if profile else 1)
    
    # Recent history
    recent_logs = db.query(CoachLog).filter(
        CoachLog.athlete_id == profile.id
    ).order_by(CoachLog.timestamp.desc()).limit(6).all()
    recent_logs.reverse()
    
    history_text = "\n".join([f"{l.role.upper()}: {l.message}" for l in recent_logs])

    # Upcoming workouts
    today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    upcoming = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == profile.id,
        CalendarEvent.date >= today_str
    ).order_by(CalendarEvent.date.asc()).limit(5).all()
    upcoming_text = "\n".join([f"- {u.date}: {u.title} ({u.primary_zone}, {u.planned_tss} TSS, {u.status})" for u in upcoming])

    prompt = f"""
You are the athlete's dedicated AI cycling coach (WhooshCoach).
You are motivating, scientifically grounded, and concise.

{athlete_ctx}

UPCOMING WORKOUTS:
{upcoming_text or 'No scheduled workouts.'}

RECENT CONVERSATION:
{history_text}

ATHLETE MESSAGE:
{user_message}

Provide a helpful, encouraging, and concise response (1-3 short paragraphs). If they ask for pacing, zones, nutrition, or workout swaps, give specific actionable advice.
"""

    preferred_model = (profile.gemini_model if profile and profile.gemini_model else DEFAULT_MODEL)
    try:
        response, model_used, fallback_used = generate_content_with_fallback(
            client=client,
            preferred_model=preferred_model,
            contents=prompt
        )
        reply = response.text.strip()
        
        # Save to logs
        db.add(CoachLog(athlete_id=profile.id, role="user", message=user_message, context_type="chat"))
        db.add(CoachLog(athlete_id=profile.id, role="assistant", message=reply, context_type="chat"))
        db.commit()
        return reply

    except Exception as e:
        logger.error(f"Error in chat_with_coach: {e}")
        return f"Coach is temporarily unavailable: {str(e)}"
