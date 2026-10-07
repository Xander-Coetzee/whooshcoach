"""
Tiered Context Engine for WhooshCoach.
Implements Section 11 Context Hierarchy (CrankAddict/section-11) and Coach Watts Data Model.

Context Tiers:
- Tier 1: dossier.md / AthleteDossier (Stable Context): Private, slow-moving athlete truths,
  long-term goals, strict schedule constraints (e.g. no weekend rides), injury notes.
- Tier 2: latest.json / Operational State: Deterministic physiological metrics (CTL, ATL, TSB,
  start-of-day ACWR, Readiness decision Go/Modify/Skip), last 14 days of activities,
  upcoming 7 days of scheduled calendar events, and target races.
- Tier 3: history.json / Long-Term Memory: 90/180-day volume trends, weekly TSS progression,
  power curves, loaded on demand via tool calling.
- Input Trust Boundary: Sanitizes and encapsulates free-text athlete descriptions into
  <untrusted_athlete_data> tags to prevent prompt injection or accidental coaching derailment.
"""

import datetime
import json
import re
from typing import Dict, List, Optional, Any
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import AthleteProfile, AthleteDossier, CalendarEvent, RideActivity, Workout
from app.physiology import (
    calculate_power_zones,
    calculate_hr_zones,
    calculate_training_load,
    calculate_acwr,
    compute_readiness_decision,
    score_workout_execution
)


# ---------------------------------------------------------------------------
# INPUT TRUST BOUNDARY (Section 11 standard)
# ---------------------------------------------------------------------------

def wrap_untrusted_data(raw_text: Optional[str]) -> str:
    """
    Wraps free-form user notes or external Strava ride descriptions
    in an explicit untrusted data boundary.
    """
    if not raw_text or not raw_text.strip():
        return ""
    # Strip any potential tag injection attempts
    cleaned = re.sub(r'<\s*/?\s*untrusted_athlete_data\s*>', '', raw_text.strip(), flags=re.IGNORECASE)
    cleaned = cleaned.replace("```", "'''")
    return f"<untrusted_athlete_data>\n{cleaned}\n</untrusted_athlete_data>"


# ---------------------------------------------------------------------------
# TIER 1: ATHLETE DOSSIER (Stable Context)
# ---------------------------------------------------------------------------

def get_athlete_dossier(db: Session, athlete_id: int = 1) -> Dict[str, Any]:
    """Retrieves the stable athlete dossier."""
    dossier = db.query(AthleteDossier).filter(AthleteDossier.athlete_id == athlete_id).first()
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    
    if not dossier:
        # Fallback creation
        sched = "Strictly NO workouts on Saturday and Sunday. Available training days: Tuesday and Thursday."
        dossier = AthleteDossier(
            athlete_id=athlete_id,
            long_term_goals=profile.primary_goal if profile else "Aerobic base and FTP improvement.",
            schedule_constraints=sched,
            injury_and_health_notes="None currently active.",
            equipment_notes="Smart trainer (indoor) and road/gravel bike with power meter (outdoor).",
            training_preferences="Progressive overload with sweet spot and polarized distribution."
        )
        db.add(dossier)
        db.commit()
        db.refresh(dossier)
        
    return {
        "athlete_id": dossier.athlete_id,
        "athlete_name": profile.name if profile else "Athlete",
        "long_term_goals": dossier.long_term_goals or "",
        "schedule_constraints": dossier.schedule_constraints or "",
        "injury_and_health_notes": dossier.injury_and_health_notes or "None currently active.",
        "equipment_notes": dossier.equipment_notes or "",
        "training_preferences": dossier.training_preferences or "",
        "dossier_markdown": dossier.dossier_markdown or "",
        "updated_at": dossier.updated_at.isoformat() if dossier.updated_at else None
    }


def format_dossier_for_prompt(dossier: Dict[str, Any]) -> str:
    """Formats the stable dossier into high-signal markdown for the system prompt."""
    return f"""
[TIER 1: ATHLETE DOSSIER - STABLE CONTEXT & MANDATORY RULES]
- Athlete: {dossier.get('athlete_name')}
- Long-Term Goals: {dossier.get('long_term_goals')}
- STRICT SCHEDULE CONSTRAINTS: {dossier.get('schedule_constraints')}
- Health & Injury Context: {dossier.get('injury_and_health_notes')}
- Equipment Setup: {dossier.get('equipment_notes')}
- Training Preferences: {dossier.get('training_preferences')}
* NOTE: You MUST strictly obey Schedule Constraints above (e.g. no workouts on forbidden days like Saturday/Sunday).
""".strip()


# ---------------------------------------------------------------------------
# TIER 2: OPERATIONAL STATE (latest.json)
# ---------------------------------------------------------------------------

def build_latest_state(db: Session, athlete_id: int = 1, as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Builds the complete operational state for the athlete:
    - Deterministic CTL, ATL, TSB, ACWR, Readiness decision (Go / Modify / Skip).
    - Last 14 days of completed activities with execution scoring and indoor/outdoor classification.
    - Next 7 days of scheduled calendar events.
    - Upcoming priority target races.
    """
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile:
        profile = db.query(AthleteProfile).filter(AthleteProfile.id == 1).first()
    ftp = profile.ftp if profile else 220
    max_hr = profile.max_hr if profile else 185
    
    today_str = as_of_date or datetime.date.today().isoformat()
    today_dt = datetime.date.fromisoformat(today_str)
    
    # 1. Fetch 90 days of completed activities to compute daily TSS map
    history_cutoff = (today_dt - datetime.timedelta(days=90)).isoformat()
    all_activities = db.query(RideActivity).filter(
        RideActivity.athlete_id == profile.id,
        RideActivity.start_date_local >= history_cutoff
    ).all()
    
    daily_tss_map: Dict[str, int] = {}
    for act in all_activities:
        act_date = act.start_date_local[:10] if act.start_date_local else (act.start_date[:10] if act.start_date else None)
        if act_date:
            daily_tss_map[act_date] = daily_tss_map.get(act_date, 0) + (act.calculated_tss or 0)
            
    # Also check completed calendar events to ensure nothing is missed
    comp_events = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == profile.id,
        CalendarEvent.status == "completed",
        CalendarEvent.date >= history_cutoff
    ).all()
    for ev in comp_events:
        if ev.date and ev.actual_tss:
            daily_tss_map[ev.date] = max(daily_tss_map.get(ev.date, 0), ev.actual_tss)

    # 2. Deterministic Training Load & ACWR
    load_metrics = calculate_training_load(daily_tss_map, as_of_date=today_str, history_days=90)
    ctl = load_metrics["ctl"]
    atl = load_metrics["atl"]
    tsb = load_metrics["tsb"]
    
    acwr, acute_avg, chronic_avg = calculate_acwr(daily_tss_map, as_of_date=today_str)
    
    # Check yesterday's outdoor status & consecutive hard days
    yesterday_str = (today_dt - datetime.timedelta(days=1)).isoformat()
    yesterday_acts = [a for a in all_activities if (a.start_date_local or a.start_date or "").startswith(yesterday_str)]
    yesterday_outdoor_overload = any(
        (a.type == "Ride" or (a.id and "strava" in a.id)) and (a.calculated_tss or 0) >= 70
        for a in yesterday_acts
    )
    
    consecutive_hard_days = 0
    for i in range(1, 4):
        d_str = (today_dt - datetime.timedelta(days=i)).isoformat()
        if daily_tss_map.get(d_str, 0) >= 60:
            consecutive_hard_days += 1
        else:
            break

    dossier = get_athlete_dossier(db, athlete_id=profile.id)
    has_active_injury = "active" in dossier.get("injury_and_health_notes", "").lower() and "none" not in dossier.get("injury_and_health_notes", "").lower()
    
    readiness = compute_readiness_decision(
        ctl=ctl,
        atl=atl,
        tsb=tsb,
        acwr=acwr,
        recent_consecutive_hard_days=consecutive_hard_days,
        has_active_injury_or_illness=has_active_injury,
        yesterday_outdoor_overload=yesterday_outdoor_overload
    )
    
    # 3. Last 14 Days Recent Activities (High-Signal Operational State)
    fourteen_days_ago = (today_dt - datetime.timedelta(days=14)).isoformat()
    recent_acts = db.query(RideActivity).filter(
        RideActivity.athlete_id == profile.id,
        RideActivity.start_date_local >= fourteen_days_ago
    ).order_by(RideActivity.start_date_local.desc()).limit(15).all()
    
    recent_activities_data = []
    indoor_count = 0
    outdoor_count = 0
    
    for a in recent_acts:
        dur_min = round(a.moving_time / 60, 1) if a.moving_time else 0.0
        act_date = a.start_date_local[:10] if a.start_date_local else (a.start_date[:10] if a.start_date else "Recent")
        is_outdoor = (a.type == "Ride" or (a.id and "strava" in a.id))
        
        if is_outdoor:
            outdoor_count += 1
            env_label = "Outdoor Road/Gravel [Strava]"
        else:
            indoor_count += 1
            env_label = "Indoor Virtual [MyWhoosh]" if (a.id and "mw" in a.id) else "Indoor Virtual [Trainer]"
            
        matched_ev = db.query(CalendarEvent).filter(CalendarEvent.activity_id == a.id).first() if a.id else None
        exec_score = None
        if matched_ev and matched_ev.planned_tss:
            exec_score = score_workout_execution(
                planned_tss=matched_ev.planned_tss,
                actual_tss=a.calculated_tss or 0,
                planned_duration_min=matched_ev.planned_duration_minutes or dur_min,
                actual_duration_min=dur_min,
                avg_watts=a.average_watts,
                target_ftp=ftp
            )
            
        recent_activities_data.append({
            "id": a.id,
            "date": act_date,
            "name": a.name,
            "environment": env_label,
            "is_outdoor": is_outdoor,
            "duration_minutes": dur_min,
            "distance_km": round((a.distance or 0) / 1000.0, 1) if (a.distance and a.distance > 100) else round(a.distance or 0, 1),
            "tss": a.calculated_tss or 0,
            "avg_watts": round(a.average_watts, 1) if a.average_watts else None,
            "weighted_watts": round(a.weighted_average_watts, 1) if a.weighted_average_watts else None,
            "avg_hr": round(a.average_heartrate, 1) if a.average_heartrate else None,
            "execution_score": exec_score.get("score") if exec_score else None,
            "execution_summary": exec_score.get("summary") if exec_score else None,
            "notes": wrap_untrusted_data(matched_ev.athlete_notes if matched_ev else None)
        })

    # 4. Upcoming 7 Days Scheduled Calendar Events
    next_7_days_cutoff = (today_dt + datetime.timedelta(days=7)).isoformat()
    upcoming_events = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == profile.id,
        CalendarEvent.date >= today_str,
        CalendarEvent.date <= next_7_days_cutoff
    ).order_by(CalendarEvent.date.asc()).all()
    
    upcoming_calendar_data = [
        {
            "id": e.id,
            "date": e.date,
            "title": e.title,
            "event_type": e.event_type or "workout",
            "primary_zone": e.primary_zone or "Z2 - Endurance",
            "planned_duration_minutes": e.planned_duration_minutes or 0,
            "planned_tss": e.planned_tss or 0,
            "status": e.status or "scheduled",
            "is_manual": bool(e.is_manual),
            "notes": wrap_untrusted_data(e.athlete_notes)
        }
        for e in upcoming_events
    ]
    
    # 5. Target Races / Priority Events
    target_races = db.query(CalendarEvent).filter(
        CalendarEvent.athlete_id == profile.id,
        CalendarEvent.event_type == "race",
        CalendarEvent.date >= today_str
    ).order_by(CalendarEvent.date.asc()).all()
    
    races_data = [
        {
            "id": r.id,
            "date": r.date,
            "title": r.title,
            "priority": r.race_priority or "A",
            "race_type": r.race_type or "Cycling Race",
            "distance_km": r.target_distance_km,
            "strategy_notes": wrap_untrusted_data(r.athlete_notes)
        }
        for r in target_races
    ]

    power_zones = calculate_power_zones(ftp)
    hr_zones = calculate_hr_zones(max_hr)

    return {
        "athlete_summary": {
            "id": profile.id,
            "name": profile.name,
            "platform": profile.platform,
            "ftp": ftp,
            "max_hr": max_hr,
            "weight_kg": profile.weight_kg,
            "coaching_mode": getattr(profile, "coaching_mode", "autonomous") or "autonomous",
            "power_zones": power_zones,
            "hr_zones": hr_zones
        },
        "readiness_decision": readiness,
        "training_load": {
            "fitness_ctl": ctl,
            "fatigue_atl": atl,
            "form_tsb": tsb,
            "acwr_start_of_day": acwr,
            "acute_load_7d": acute_avg,
            "chronic_load_28d": chronic_avg,
            "indoor_rides_14d": indoor_count,
            "outdoor_rides_14d": outdoor_count
        },
        "recent_activities": recent_activities_data,
        "upcoming_calendar": upcoming_calendar_data,
        "target_races": races_data
    }


def format_latest_state_for_prompt(state: Dict[str, Any]) -> str:
    """Formats Tier 2 latest.json operational state into high-signal markdown for Gemini."""
    ath = state["athlete_summary"]
    tl = state["training_load"]
    rd = state["readiness_decision"]
    
    # Readiness badge
    readiness_block = (
        f"READINESS GATE: [{rd['decision'].upper()}] (Score: {rd['score']}/100, Status: {rd['status_color'].upper()})\n"
        f"- Physiological Form: CTL {tl['fitness_ctl']} (Fitness) | ATL {tl['fatigue_atl']} (Fatigue) | TSB {tl['form_tsb']:+.1f} (Form)\n"
        f"- Workload Ratio: Start-of-day ACWR = {tl['acwr_start_of_day']:.2f} (7d: {tl['acute_load_7d']} TSS/d, 28d: {tl['chronic_load_28d']} TSS/d)\n"
        f"- Coach Diagnostic: {rd['reason']}\n"
    )
    if rd.get("flags"):
        readiness_block += "- Flags: " + "; ".join(rd["flags"]) + "\n"
    if rd.get("recommendations"):
        readiness_block += "- Prescribed Action: " + " ".join(rd["recommendations"]) + "\n"

    # Recent activities lines
    act_lines = []
    for a in state["recent_activities"][:8]:
        pwr_str = f", Avg: {a['avg_watts']}W" if a.get("avg_watts") else ""
        hr_str = f", HR: {a['avg_hr']}bpm" if a.get("avg_hr") else ""
        score_str = f", Score: {a['execution_score']}/10" if a.get("execution_score") else ""
        notes_str = f" {a['notes']}" if a.get("notes") else ""
        act_lines.append(
            f"- {a['date']} {a['environment']}: \"{a['name']}\" ({a['duration_minutes']} min, {a['tss']} TSS{pwr_str}{hr_str}{score_str}){notes_str}"
        )
    recent_acts_text = "\n".join(act_lines) if act_lines else "No recent completed rides logged."

    # Upcoming calendar lines
    cal_lines = []
    for c in state["upcoming_calendar"]:
        cal_lines.append(
            f"- {c['date']}: \"{c['title']}\" ({c['primary_zone']}, {c['planned_tss']} TSS, {c['planned_duration_minutes']}m, Status: {c['status']})"
        )
    cal_text = "\n".join(cal_lines) if cal_lines else "No upcoming workouts currently scheduled."

    # Target races
    race_lines = []
    for r in state["target_races"]:
        race_lines.append(
            f"- {r['date']}: \"{r['title']}\" (Priority {r['priority']}, Type: {r['race_type']}, Distance: {r.get('distance_km') or 'N/A'}km)"
        )
    races_text = "\n".join(race_lines) if race_lines else "No priority target races scheduled."

    return f"""
[TIER 2: OPERATIONAL STATE - DETERMINISTIC PHYSIOLOGY & LIVE CALENDAR]
ATHLETE METRICS:
- FTP: {ath['ftp']} W | Max HR: {ath['max_hr']} bpm | Weight: {ath['weight_kg']} kg | Platform: {ath['platform'].title()}
- Z2 Endurance: {ath['power_zones']['Z2 - Endurance']['min_watts']}-{ath['power_zones']['Z2 - Endurance']['max_watts']} W
- Z4 Threshold: {ath['power_zones']['Z4 - Threshold']['min_watts']}-{ath['power_zones']['Z4 - Threshold']['max_watts']} W
- Z5 VO2Max: {ath['power_zones']['Z5 - VO2Max']['min_watts']}-{ath['power_zones']['Z5 - VO2Max']['max_watts']} W

DETERMINISTIC READINESS ENGINE:
{readiness_block.strip()}

RECENT COMPLETED ACTIVITIES (PAST 14 DAYS - {tl['indoor_rides_14d']} Indoor, {tl['outdoor_rides_14d']} Outdoor):
{recent_acts_text}

UPCOMING SCHEDULED SESSIONS (NEXT 7 DAYS):
{cal_text}

UPCOMING TARGET RACES & EVENTS:
{races_text}
""".strip()


# ---------------------------------------------------------------------------
# TIER 3: LONG-TERM MEMORY (history.json)
# ---------------------------------------------------------------------------

def build_history_state(db: Session, athlete_id: int = 1, days: int = 90) -> Dict[str, Any]:
    """
    Builds the long-term historical trends (90 to 180 days).
    Summarizes weekly volume, peak power efforts, and threshold progression.
    Loaded on demand via tool calling.
    """
    profile = db.query(AthleteProfile).filter(AthleteProfile.id == athlete_id).first()
    if not profile:
        profile = db.query(AthleteProfile).filter(AthleteProfile.id == 1).first()
        
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    activities = db.query(RideActivity).filter(
        RideActivity.athlete_id == profile.id,
        RideActivity.start_date_local >= cutoff
    ).order_by(RideActivity.start_date_local.asc()).all()
    
    # Weekly aggregates
    weeks: Dict[str, Dict[str, Any]] = {}
    peak_watts = 0.0
    peak_weighted = 0.0
    
    for a in activities:
        d_str = a.start_date_local[:10] if a.start_date_local else (a.start_date[:10] if a.start_date else None)
        if not d_str:
            continue
        dt = datetime.date.fromisoformat(d_str)
        # Week beginning Monday
        week_start = (dt - datetime.timedelta(days=dt.weekday())).isoformat()
        if week_start not in weeks:
            weeks[week_start] = {
                "week_start": week_start,
                "total_tss": 0,
                "total_minutes": 0.0,
                "ride_count": 0,
                "indoor_count": 0,
                "outdoor_count": 0
            }
        weeks[week_start]["total_tss"] += (a.calculated_tss or 0)
        weeks[week_start]["total_minutes"] += (round(a.moving_time / 60, 1) if a.moving_time else 0.0)
        weeks[week_start]["ride_count"] += 1
        
        is_outdoor = (a.type == "Ride" or (a.id and "strava" in a.id))
        if is_outdoor:
            weeks[week_start]["outdoor_count"] += 1
        else:
            weeks[week_start]["indoor_count"] += 1
            
        if a.max_watts and a.max_watts > peak_watts:
            peak_watts = a.max_watts
        if a.weighted_average_watts and a.weighted_average_watts > peak_weighted:
            peak_weighted = a.weighted_average_watts

    weekly_summary = list(weeks.values())
    for w in weekly_summary:
        w["total_hours"] = round(w["total_minutes"] / 60.0, 1)

    return {
        "athlete_id": profile.id,
        "as_of_date": datetime.date.today().isoformat(),
        "history_window_days": days,
        "total_activities_count": len(activities),
        "peak_max_watts": peak_watts or None,
        "peak_normalized_watts": peak_weighted or None,
        "weekly_progression": weekly_summary
    }
