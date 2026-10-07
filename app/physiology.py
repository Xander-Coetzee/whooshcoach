"""
Physiology & Deterministic Load Engine for WhooshCoach.
Implements Section 11 protocol standards (CrankAddict/section-11) and Coach Watts scoring.

Core principles:
- Deterministic code first, LLM second: All physiological metrics (CTL, ATL, TSB, ACWR, zones,
  and start-of-day readiness) are computed in pure Python with verified formulas.
- Never ask the LLM to calculate training load or floating-point readiness.
"""

import datetime
import math
from typing import Dict, List, Optional, Any, Tuple


# ---------------------------------------------------------------------------
# 1. POWER & HEART RATE ZONES (Coggan & Friel standards)
# ---------------------------------------------------------------------------

def calculate_power_zones(ftp: int) -> Dict[str, Dict[str, Any]]:
    """Calculates Coggan 7-zone power boundaries from FTP."""
    ftp_val = max(50, ftp)
    return {
        "Z1 - Active Recovery": {
            "min_watts": 0,
            "max_watts": round(0.55 * ftp_val),
            "pct_range": "< 55%",
            "description": "Very easy spinning, promotes blood flow without training fatigue."
        },
        "Z2 - Endurance": {
            "min_watts": round(0.56 * ftp_val),
            "max_watts": round(0.75 * ftp_val),
            "pct_range": "56% - 75%",
            "description": "Aerobic base building, mitochondrial density, fat oxidation."
        },
        "Z3 - Tempo": {
            "min_watts": round(0.76 * ftp_val),
            "max_watts": round(0.90 * ftp_val),
            "pct_range": "76% - 90%",
            "description": "Rhythmic aerobic pace, stamina builder, moderate glycogen cost."
        },
        "Z4 - Sweet Spot": {
            "min_watts": round(0.88 * ftp_val),
            "max_watts": round(0.94 * ftp_val),
            "pct_range": "88% - 94%",
            "description": "Maximum aerobic adaptation with manageable recovery cost."
        },
        "Z4 - Threshold": {
            "min_watts": round(0.91 * ftp_val),
            "max_watts": round(1.05 * ftp_val),
            "pct_range": "91% - 105%",
            "description": "Lactate threshold (LT2), sustainable 40-60 min maximum power."
        },
        "Z5 - VO2Max": {
            "min_watts": round(1.06 * ftp_val),
            "max_watts": round(1.20 * ftp_val),
            "pct_range": "106% - 120%",
            "description": "Maximal oxygen uptake, hard 3-5 min intervals, severe strain."
        },
        "Z6 - Anaerobic Capacity": {
            "min_watts": round(1.21 * ftp_val),
            "max_watts": round(1.50 * ftp_val),
            "pct_range": "121% - 150%",
            "description": "High glycolytic anaerobic work, 30s-2m efforts, rapid lactic build."
        },
        "Z7 - Neuromuscular": {
            "min_watts": round(1.51 * ftp_val),
            "max_watts": 2500,
            "pct_range": "> 150%",
            "description": "Maximal sprint power, all-out neuromuscular recruitment."
        }
    }


def calculate_hr_zones(max_hr: int) -> Dict[str, Dict[str, Any]]:
    """Calculates 5-zone HR boundaries from Max HR."""
    m_hr = max(100, max_hr)
    return {
        "Z1 - Active Recovery": {"min_hr": 0, "max_hr": round(0.68 * m_hr)},
        "Z2 - Aerobic Endurance": {"min_hr": round(0.69 * m_hr), "max_hr": round(0.83 * m_hr)},
        "Z3 - Tempo": {"min_hr": round(0.84 * m_hr), "max_hr": round(0.90 * m_hr)},
        "Z4 - Threshold": {"min_hr": round(0.91 * m_hr), "max_hr": round(0.95 * m_hr)},
        "Z5 - Anaerobic": {"min_hr": round(0.96 * m_hr), "max_hr": m_hr}
    }


# ---------------------------------------------------------------------------
# 2. TSS & INTENSITY FACTOR
# ---------------------------------------------------------------------------

def calculate_session_tss(
    duration_seconds: int,
    weighted_watts: Optional[float] = None,
    avg_watts: Optional[float] = None,
    ftp: int = 220
) -> int:
    """
    Computes Training Stress Score (TSS) deterministically.
    TSS = (sec * NP * IF) / (FTP * 3600) * 100
    where IF = NP / FTP.
    """
    if duration_seconds <= 0 or ftp <= 0:
        return 0
    
    np_power = weighted_watts or avg_watts
    if not np_power or np_power <= 0:
        # Fallback duration estimate: ~45 TSS per hour of moderate riding
        return int(round((duration_seconds / 3600.0) * 45))
    
    intensity_factor = np_power / float(ftp)
    tss = (duration_seconds * np_power * intensity_factor) / (ftp * 3600.0) * 100.0
    return max(0, int(round(tss)))


# ---------------------------------------------------------------------------
# 3. CTL, ATL, TSB (Fitness, Fatigue, Form) - Section 11 Standard
# ---------------------------------------------------------------------------

def calculate_training_load(
    daily_tss_map: Dict[str, int],
    as_of_date: Optional[str] = None,
    history_days: int = 90
) -> Dict[str, Any]:
    """
    Calculates rolling CTL (Fitness, 42d constant), ATL (Fatigue, 7d constant),
    and TSB (Form = CTL - ATL) up to as_of_date.
    
    Uses standard impulse-response formulas:
    CTL_today = CTL_yesterday + (TSS_today - CTL_yesterday) / 42
    ATL_today = ATL_yesterday + (TSS_today - ATL_yesterday) / 7
    TSB_today = CTL_today - ATL_today
    """
    today = datetime.date.fromisoformat(as_of_date) if as_of_date else datetime.date.today()
    start_date = today - datetime.timedelta(days=history_days)
    
    ctl = 0.0
    atl = 0.0
    
    timeline = []
    curr = start_date
    while curr <= today:
        date_str = curr.isoformat()
        day_tss = daily_tss_map.get(date_str, 0)
        
        # Exponential moving averages
        ctl = ctl + (day_tss - ctl) / 42.0
        atl = atl + (day_tss - atl) / 7.0
        tsb = ctl - atl
        
        timeline.append({
            "date": date_str,
            "tss": day_tss,
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(tsb, 1)
        })
        curr += datetime.timedelta(days=1)
        
    latest = timeline[-1] if timeline else {"ctl": 0.0, "atl": 0.0, "tsb": 0.0, "tss": 0}
    
    return {
        "as_of_date": today.isoformat(),
        "ctl": round(latest["ctl"], 1),
        "atl": round(latest["atl"], 1),
        "tsb": round(latest["tsb"], 1),
        "timeline_tail": timeline[-14:] # Last 14 days for quick charting
    }


# ---------------------------------------------------------------------------
# 4. START-OF-DAY ACWR (Acute:Chronic Workload Ratio)
# ---------------------------------------------------------------------------

def calculate_acwr(
    daily_tss_map: Dict[str, int],
    as_of_date: Optional[str] = None
) -> Tuple[float, float, float]:
    """
    Calculates Start-of-Day ACWR (excludes activities completed today).
    Acute load: 7-day average daily TSS (days -7 to -1).
    Chronic load: 28-day average daily TSS (days -28 to -1).
    Returns (acwr, acute_avg_tss, chronic_avg_tss).
    """
    today = datetime.date.fromisoformat(as_of_date) if as_of_date else datetime.date.today()
    
    # 7-day acute window: yesterday back 7 days
    acute_tss_sum = sum(
        daily_tss_map.get((today - datetime.timedelta(days=i)).isoformat(), 0)
        for i in range(1, 8)
    )
    acute_avg = acute_tss_sum / 7.0
    
    # 28-day chronic window: yesterday back 28 days
    chronic_tss_sum = sum(
        daily_tss_map.get((today - datetime.timedelta(days=i)).isoformat(), 0)
        for i in range(1, 29)
    )
    chronic_avg = chronic_tss_sum / 28.0
    
    if chronic_avg <= 0.0:
        acwr = 1.0 if acute_avg <= 0.0 else round(acute_avg / 10.0, 2)
    else:
        acwr = round(acute_avg / chronic_avg, 2)
        
    return acwr, round(acute_avg, 1), round(chronic_avg, 1)


# ---------------------------------------------------------------------------
# 5. START-OF-DAY READINESS DECISION (Section 11 Protocol Engine)
# ---------------------------------------------------------------------------

def compute_readiness_decision(
    ctl: float,
    atl: float,
    tsb: float,
    acwr: float,
    recent_consecutive_hard_days: int = 0,
    has_active_injury_or_illness: bool = False,
    yesterday_outdoor_overload: bool = False
) -> Dict[str, Any]:
    """
    Evaluates athlete readiness deterministically (Section 11 Protocol).
    Outputs:
    - decision: "Go" | "Modify" | "Skip"
    - score: 0 to 100
    - reason: Human-readable explanation
    - recommendations: Array of specific coaching action items
    """
    score = 100
    flags = []
    recommendations = []
    
    # Check 1: Active illness or acute injury marker
    if has_active_injury_or_illness:
        return {
            "decision": "Skip",
            "score": 25,
            "status_color": "red",
            "reason": "Active illness or injury logged. Full rest or medical recovery required.",
            "recommendations": [
                "Cancel high-intensity intervals and endurance rides.",
                "Prioritize hydration, sleep, and physical therapy recovery."
            ],
            "metrics": {"ctl": ctl, "atl": atl, "tsb": tsb, "acwr": acwr}
        }
        
    # Check 2: Acute fatigue & TSB balance
    if tsb < -30:
        score -= 40
        flags.append(f"Severe acute fatigue: TSB is {tsb:.1f} (< -30)")
    elif tsb < -20:
        score -= 25
        flags.append(f"Elevated fatigue: TSB is {tsb:.1f} (< -20)")
    elif tsb > 15:
        flags.append(f"High freshness: TSB is +{tsb:.1f} (Ideal for race/peak effort)")
        
    # Check 3: ACWR (Gabbett Acute:Chronic Workload Ratio)
    if acwr >= 1.5:
        score -= 35
        flags.append(f"Dangerous workload spike: ACWR is {acwr:.2f} (>= 1.5 danger threshold)")
    elif acwr >= 1.3:
        score -= 15
        flags.append(f"Workload accumulation warning: ACWR is {acwr:.2f} (1.3 - 1.5)")
    elif acwr < 0.8:
        flags.append(f"Deload or recovering: ACWR is {acwr:.2f} (< 0.8)")
        
    # Check 4: Consecutive hard days
    if recent_consecutive_hard_days >= 3:
        score -= 20
        flags.append(f"3+ consecutive hard training sessions completed without rest.")
        
    # Check 5: Yesterday outdoor overload
    if yesterday_outdoor_overload:
        score -= 15
        flags.append("Significant outdoor volume/climbing fatigue from yesterday.")

    # Determine final readiness gate
    score = max(10, min(100, score))
    
    if score < 50 or acwr >= 1.5 and tsb < -25:
        decision = "Skip"
        status_color = "red"
        reason = "Systemic fatigue or spike in workload necessitates rest or active recovery only."
        recommendations = [
            "Convert any high-intensity or threshold workout today into a complete Rest Day or 30-min Z1 Active Recovery spin.",
            "Allow parasympathetic restoration before resuming quality intervals."
        ]
    elif score < 75 or acwr >= 1.3 or tsb < -15 or recent_consecutive_hard_days >= 2:
        decision = "Modify"
        status_color = "amber"
        reason = "Accumulated fatigue warrants modified training intensity or reduced duration."
        recommendations = [
            "Downgrade high-intensity intervals (VO2Max/Anaerobic) to steady Z2 Aerobic Endurance.",
            "Cap session duration to 45-60 minutes.",
            "Avoid low-cadence grinding; spin smoothly above 85-90 RPM."
        ]
    else:
        decision = "Go"
        status_color = "green"
        reason = "Physiological markers indicate optimal training readiness. Green light for quality work."
        recommendations = [
            "Proceed with scheduled workout as planned.",
            "Target quality execution and hit power interval targets."
        ]
        
    return {
        "decision": decision,
        "score": score,
        "status_color": status_color,
        "reason": reason,
        "flags": flags,
        "recommendations": recommendations,
        "metrics": {
            "ctl": ctl,
            "atl": atl,
            "tsb": tsb,
            "acwr": acwr
        }
    }


# ---------------------------------------------------------------------------
# 6. WORKOUT EXECUTION SCORING (Coach Watts Standard 1-10)
# ---------------------------------------------------------------------------

def score_workout_execution(
    planned_tss: int,
    actual_tss: int,
    planned_duration_min: float,
    actual_duration_min: float,
    avg_watts: Optional[float] = None,
    target_ftp: int = 220
) -> Dict[str, Any]:
    """
    Scores completed workout compliance on a 1-10 scale (Coach Watts standard).
    Calculates duration accuracy and TSS execution fidelity.
    """
    if planned_duration_min <= 0 or planned_tss <= 0:
        return {
            "score": 8,
            "rating": "Free Ride",
            "compliance_pct": 100,
            "summary": "Completed unscheduled or free-ride volume."
        }
        
    dur_ratio = actual_duration_min / planned_duration_min
    tss_ratio = actual_tss / float(planned_tss)
    
    # Optimal execution is near 100% (within 90% - 110%)
    dur_deviation = abs(1.0 - dur_ratio)
    tss_deviation = abs(1.0 - tss_ratio)
    
    score = 10.0
    # Penalty for duration discrepancies
    if dur_deviation > 0.15:
        score -= min(3.0, (dur_deviation - 0.15) * 6.0)
    # Penalty for TSS discrepancies
    if tss_deviation > 0.20:
        score -= min(4.0, (tss_deviation - 0.20) * 5.0)
        
    final_score = max(1, min(10, int(round(score))))
    
    if final_score >= 9:
        rating = "Excellent Execution"
        summary = f"Nailed the planned targets! Duration was {dur_ratio*100:.0f}% and TSS was {tss_ratio*100:.0f}% of plan."
    elif final_score >= 7:
        rating = "Solid Session"
        summary = f"Good adherence to planned load ({actual_tss} vs {planned_tss} TSS planned, {actual_duration_min:.0f}m)."
    elif final_score >= 5:
        rating = "Moderate Deviation"
        summary = f"Session diverged from plan: {actual_tss} TSS vs {planned_tss} TSS planned."
    else:
        rating = "Significant Under/Over-load"
        summary = f"Substantial gap between plan and execution ({actual_tss} vs {planned_tss} TSS)."
        
    return {
        "score": final_score,
        "rating": rating,
        "duration_ratio_pct": round(dur_ratio * 100, 1),
        "tss_ratio_pct": round(tss_ratio * 100, 1),
        "summary": summary
    }
