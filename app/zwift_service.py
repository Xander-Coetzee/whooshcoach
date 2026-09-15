import datetime
import json
import logging
import re
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET
from xml.dom import minidom
import httpx
from sqlalchemy.orm import Session

from app.database import AthleteProfile, CalendarEvent, RideActivity

logger = logging.getLogger("whooshcoach.zwift")

ZWIFT_AUTH_URL = "https://secure.zwift.com/auth/realms/zwift/protocol/openid-connect/token"
ZWIFT_API_BASE = "https://us-or-rly101.zwift.com/api"

class ZwiftService:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    async def login(self, username: str, password: str) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """
        Authenticates against Zwift OpenID Connect service.
        Returns (success, access_token, profile_id, error_message).
        """
        logger.info(f"[Zwift] Attempting login for '{username}'...")
        payload = {
            "client_id": "Zwift_Mobile_App",
            "grant_type": "password",
            "username": username.strip(),
            "password": password.strip()
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Zwift/1.0 (Android)"
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(ZWIFT_AUTH_URL, data=payload, headers=headers)
                logger.info(f"[Zwift] Auth response: HTTP {resp.status_code}")
                if resp.status_code != 200:
                    try:
                        err_json = resp.json()
                        err_desc = err_json.get("error_description") or err_json.get("error") or resp.text
                    except Exception:
                        err_desc = resp.text[:200]
                    return False, None, None, f"Zwift login failed (HTTP {resp.status_code}): {err_desc}"

                data = resp.json()
                token = data.get("access_token")
                if not token:
                    return False, None, None, "No access token in Zwift response."

                # Query profile ID
                prof_resp = await client.get(
                    f"{ZWIFT_API_BASE}/profiles/me",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
                )
                zwift_id = None
                if prof_resp.status_code == 200:
                    pdata = prof_resp.json()
                    zwift_id = str(pdata.get("id") or "")
                    logger.info(f"[Zwift] Profile fetched! ID: {zwift_id}, Name: {pdata.get('firstName')} {pdata.get('lastName')}")

                return True, token, zwift_id, None
        except Exception as e:
            logger.error(f"[Zwift] Login exception: {e}", exc_info=True)
            return False, None, None, str(e)

    async def fetch_activities(self, token: str, profile_id: str, limit: int = 15) -> Tuple[List[Dict], Optional[str]]:
        """
        Fetches completed activities feed from Zwift API.
        Returns (activities_list, error_message).
        """
        if not profile_id:
            return [], "Zwift Profile ID is required to fetch activities."

        url = f"{ZWIFT_API_BASE}/profiles/{profile_id}/activities"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        params = {"start": 0, "limit": limit}

        logger.info(f"[Zwift] Fetching activities for profile '{profile_id}' (limit={limit})...")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code == 401:
                    return [], "token_expired"
                if resp.status_code != 200:
                    return [], f"Zwift API returned HTTP {resp.status_code}: {resp.text[:200]}"

                results = resp.json()
                if isinstance(results, dict) and "activities" in results:
                    results = results["activities"]
                if not isinstance(results, list):
                    results = []

                logger.info(f"[Zwift] Fetched {len(results)} activities from cloud.")
                return results, None
        except Exception as e:
            logger.error(f"[Zwift] Error fetching activities: {e}", exc_info=True)
            return [], str(e)

    def calculate_tss(self, duration_sec: int, avg_watts: Optional[float], avg_hr: Optional[float], profile: AthleteProfile) -> int:
        """Calculates Training Stress Score using power or heart rate."""
        if duration_sec <= 0:
            return 0

        ftp = profile.ftp or 200
        if avg_watts and avg_watts > 0 and ftp > 0:
            intensity_factor = avg_watts / ftp
            tss = (duration_sec * avg_watts * intensity_factor) / (ftp * 3600) * 100
            return int(round(tss))

        max_hr = profile.max_hr or 185
        if avg_hr and avg_hr > 0 and max_hr > 0:
            rest_hr = 50.0
            hr_reserve_ratio = max(0.1, (avg_hr - rest_hr) / (max_hr - rest_hr))
            intensity_factor = hr_reserve_ratio * 0.95
            tss = (duration_sec / 3600.0) * (intensity_factor ** 2) * 100
            return int(round(tss))

        return int(round((duration_sec / 3600.0) * 50))

    async def sync_activities(self, db: Session, profile: AthleteProfile) -> Dict:
        """
        Orchestrates Zwift activity sync for a specific athlete:
        1. Authenticates or refreshes token.
        2. Pulls recent activities.
        3. Saves to database, matches calendar events.
        4. Cleans past uncompleted workouts.
        5. Triggers Gemini AI coach adaptation.
        """
        from app.gemini_coach import adapt_calendar_after_activity

        if not profile.zwift_username or not profile.zwift_password:
            return {"status": "error", "message": "Zwift username and password are not configured."}

        logger.info(f"[Zwift] Starting sync_activities for athlete '{profile.name}' (ID {profile.id})...")
        try:
            token = profile.zwift_token
            zwift_id = profile.zwift_id

            # Login if no token or id
            if not token or not zwift_id:
                success, new_token, new_id, err = await self.login(profile.zwift_username, profile.zwift_password)
                if not success:
                    return {"status": "error", "message": f"Zwift login failed: {err}"}
                token = new_token
                zwift_id = new_id
                profile.zwift_token = new_token
                profile.zwift_id = new_id
                db.commit()

            # Fetch activities
            activities, err = await self.fetch_activities(token, zwift_id, limit=20)
            if err == "token_expired":
                logger.info("[Zwift] Token expired, re-authenticating...")
                success, new_token, new_id, err = await self.login(profile.zwift_username, profile.zwift_password)
                if not success:
                    return {"status": "error", "message": f"Zwift re-authentication failed: {err}"}
                token = new_token
                zwift_id = new_id
                profile.zwift_token = new_token
                profile.zwift_id = new_id
                db.commit()
                activities, err = await self.fetch_activities(token, zwift_id, limit=20)

            if err and not activities:
                return {"status": "error", "message": f"Failed to fetch Zwift activities: {err}"}

            new_count = 0
            latest_act = None

            for act in activities:
                act_id = str(act.get("id") or act.get("id_str") or "")
                if not act_id:
                    continue

                db_act_id = f"zw_{act_id}"
                existing = db.query(RideActivity).filter(RideActivity.id == db_act_id).first()
                if existing:
                    continue

                title = act.get("name") or "Zwift Ride"
                
                # Duration handling (milliseconds to seconds)
                duration_ms = act.get("durationInMilliseconds") or act.get("movingTimeInMs") or 0
                duration_sec = int(round(duration_ms / 1000.0)) if duration_ms > 0 else int(act.get("movingTime") or 0)

                distance_m = float(act.get("distanceInMeters") or 0.0)
                distance_km = distance_m / 1000.0

                avg_watts = float(act.get("avgWatts") or act.get("avgPower") or 0.0) or None
                avg_hr = float(act.get("avgHeartRate") or act.get("avgHr") or 0.0) or None
                elevation = float(act.get("totalElevation") or act.get("elevationGainInMeters") or 0.0)

                # Date parsing
                start_date_raw = act.get("startDate") or act.get("start_date") or ""
                if start_date_raw:
                    date_str = start_date_raw[:10]
                    start_datetime = start_date_raw
                else:
                    date_str = datetime.date.today().isoformat()
                    start_datetime = datetime.datetime.utcnow().isoformat()

                calculated_tss = self.calculate_tss(duration_sec, avg_watts, avg_hr, profile)

                # Match scheduled calendar event for this athlete
                matched_event = db.query(CalendarEvent).filter(
                    CalendarEvent.athlete_id == profile.id,
                    CalendarEvent.date == date_str,
                    CalendarEvent.status.in_(["planned", "scheduled"])
                ).first()

                matched_id = None
                if matched_event:
                    matched_event.status = "completed"
                    matched_event.actual_tss = calculated_tss
                    matched_event.actual_duration_minutes = round(duration_sec / 60.0, 1)
                    matched_event.actual_avg_watts = avg_watts
                    matched_event.actual_avg_hr = avg_hr
                    matched_event.activity_id = db_act_id
                    matched_id = matched_event.id
                    logger.info(f"[Zwift] Matched activity '{title}' to planned event on {date_str} for athlete {profile.name}")
                else:
                    # Create completed unplanned ride
                    new_event = CalendarEvent(
                        athlete_id=profile.id,
                        title=f"{title}",
                        date=date_str,
                        event_type="workout",
                        primary_zone="Free Ride",
                        actual_tss=calculated_tss,
                        actual_duration_minutes=round(duration_sec / 60.0, 1),
                        actual_avg_watts=avg_watts,
                        actual_avg_hr=avg_hr,
                        status="completed",
                        activity_id=db_act_id,
                        athlete_notes=f"Completed on Zwift: {distance_km:.1f} km, {avg_watts or 0:.0f}W avg."
                    )
                    db.add(new_event)
                    db.flush()
                    matched_id = new_event.id
                    logger.info(f"[Zwift] Logged completed ride '{title}' on {date_str} for athlete {profile.name}")

                db_act = RideActivity(
                    id=db_act_id,
                    athlete_id=profile.id,
                    name=title,
                    type="VirtualRide",
                    start_date=start_datetime,
                    start_date_local=start_datetime,
                    elapsed_time=duration_sec,
                    moving_time=duration_sec,
                    distance=distance_m,
                    total_elevation_gain=elevation,
                    average_watts=avg_watts,
                    average_heartrate=avg_hr,
                    calculated_tss=calculated_tss,
                    matched_event_id=matched_id,
                    raw_json=json.dumps(act),
                    synced_at=datetime.datetime.utcnow()
                )
                db.add(db_act)
                new_count += 1
                if not latest_act:
                    latest_act = db_act

            # Remove past uncompleted training workouts for this athlete
            today_str = datetime.date.today().strftime("%Y-%m-%d")
            cleaned_past = db.query(CalendarEvent).filter(
                CalendarEvent.athlete_id == profile.id,
                CalendarEvent.date < today_str,
                CalendarEvent.status != "completed",
                CalendarEvent.event_type.notin_(["race", "custom", "rest"])
            ).delete(synchronize_session=False)

            if cleaned_past > 0:
                logger.info(f"[Zwift] Cleaned up {cleaned_past} past uncompleted workout(s) for athlete {profile.name}.")

            profile.last_zwift_sync = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            db.commit()

            # Dynamic AI Coach adaptation
            adaptation_notes = None
            if new_count > 0 and latest_act and profile.gemini_api_key:
                try:
                    matched_cal_event = db.query(CalendarEvent).filter(CalendarEvent.id == latest_act.matched_event_id).first()
                    if matched_cal_event:
                        adapt_res = await adapt_calendar_after_activity(db, matched_cal_event, latest_act)
                        adaptation_notes = adapt_res.get("debrief")
                except Exception as e:
                    logger.warning(f"AI Coach adaptation error: {e}")

            logger.info(f"[Zwift] Sync completed for {profile.name}: {new_count} new ride(s), {cleaned_past} expired workout(s) purged.")
            return {
                "status": "success",
                "new_activities": new_count,
                "cleaned_past_workouts": cleaned_past,
                "last_sync": profile.last_zwift_sync,
                "adaptation": adaptation_notes
            }
        except Exception as e:
            logger.error(f"[Zwift] Sync exception: {e}", exc_info=True)
            return {"status": "error", "message": f"Zwift sync failed: {str(e)}"}

    def generate_zwo(self, title: str, description: str, duration_minutes: float, zone: str = "Z2 - Endurance", steps_raw: str = "") -> str:
        """
        Generates a standard Zwift Workout XML (.zwo) document.
        """
        root = ET.Element("workout_file")
        ET.SubElement(root, "author").text = "WhooshCoach AI"
        ET.SubElement(root, "name").text = title
        ET.SubElement(root, "description").text = description or "Personalized AI Periodized Workout"
        ET.SubElement(root, "sportType").text = "bike"
        ET.SubElement(root, "tags")

        workout_elem = ET.SubElement(root, "workout")

        # Map zones to typical % FTP targets
        zone_power_map = {
            "Z1 - Recovery": 0.55,
            "Z2 - Endurance": 0.68,
            "Z3 - Tempo": 0.82,
            "Z4 - Threshold": 0.95,
            "Z5 - VO2Max": 1.10,
            "Z6 - Anaerobic": 1.25,
            "Race": 0.90
        }
        main_power = zone_power_map.get(zone, 0.70)
        total_seconds = int(max(15, duration_minutes) * 60)

        # Standard structured workout layout:
        # 1. Warmup: 10 mins ramping 50% to 75%
        # 2. Main interval / steady state
        # 3. Cooldown: 5 mins ramping 70% down to 50%
        warmup_sec = min(600, int(total_seconds * 0.15))
        cooldown_sec = min(300, int(total_seconds * 0.10))
        main_sec = max(60, total_seconds - warmup_sec - cooldown_sec)

        # Warmup
        ET.SubElement(workout_elem, "Warmup", {
            "Duration": str(warmup_sec),
            "PowerLow": "0.50",
            "PowerHigh": "0.75"
        })

        # Steady State / Work block
        ET.SubElement(workout_elem, "SteadyState", {
            "Duration": str(main_sec),
            "Power": f"{main_power:.2f}"
        })

        # Cooldown
        ET.SubElement(workout_elem, "Cooldown", {
            "Duration": str(cooldown_sec),
            "PowerLow": "0.70",
            "PowerHigh": "0.50"
        })

        # Pretty-print XML
        xml_str = ET.tostring(root, encoding="utf-8")
        parsed = minidom.parseString(xml_str)
        return parsed.toprettyxml(indent="  ")

zwift_service = ZwiftService()
