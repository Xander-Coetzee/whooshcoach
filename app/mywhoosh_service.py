import datetime
import json
import logging
import uuid
from typing import Dict, List, Optional, Tuple
import httpx
from sqlalchemy.orm import Session

from app.database import AthleteProfile, CalendarEvent, RideActivity
from app.gemini_coach import adapt_calendar_after_activity

logger = logging.getLogger("whooshcoach.mywhoosh")

LOGIN_URL = "https://services.mywhoosh.com/http-service/api/login"
ACTIVITIES_BASE = "https://service14.mywhoosh.com/v2/"

class MyWhooshService:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    async def login(self, username: str, password: str) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """
        Authenticates against MyWhoosh API.
        Returns (success, access_token, whoosh_id, error_message).
        """
        logger.info(f"[MyWhoosh] Logging in user '{username}'...")
        payload = {
            "Username": username.strip(),
            "Password": password.strip(),
            "Platform": "Android",
            "Action": 1001,
            "CorrelationId": str(uuid.uuid4()),
            "DeviceId": str(uuid.uuid4()),
            "Authorization": ""
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(LOGIN_URL, json=payload, headers={"Content-Type": "application/json"})
                logger.info(f"[MyWhoosh] Login response: HTTP {resp.status_code}")
                if resp.status_code != 200:
                    logger.error(f"[MyWhoosh] Login failed with HTTP {resp.status_code}: {resp.text[:200]}")
                    return False, None, None, f"MyWhoosh server returned HTTP {resp.status_code}"
                
                data = resp.json()
                if not data.get("Success"):
                    msg = data.get("Message", "Authentication failed. Check email and password.")
                    logger.warning(f"[MyWhoosh] Login rejected: {msg}")
                    return False, None, None, msg

                token = data.get("AccessToken")
                whoosh_id = data.get("WhooshId")
                if not token:
                    logger.error("[MyWhoosh] Login response missing AccessToken")
                    return False, None, None, "No access token in MyWhoosh response."

                logger.info(f"[MyWhoosh] Login successful! WhooshId: {whoosh_id}")
                return True, token, whoosh_id, None
        except Exception as e:
            logger.error(f"[MyWhoosh] Login exception: {e}", exc_info=True)
            return False, None, None, str(e)

    async def fetch_activities(self, token: str, max_pages: int = 3) -> Tuple[List[Dict], Optional[str]]:
        """
        Fetches activities for the authenticated user from MyWhoosh cloud.
        Returns (activities_list, error_message).
        """
        activities_url = ACTIVITIES_BASE + "rider/profile/activities"
        all_activities = []

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }

        logger.info(f"[MyWhoosh] Fetching activities from cloud (max_pages={max_pages})...")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for page in range(1, max_pages + 1):
                    payload = {"sortDate": "DESC", "page": page}
                    resp = await client.post(activities_url, json=payload, headers=headers)
                    logger.info(f"[MyWhoosh] Activities page {page}: HTTP {resp.status_code}")
                    if resp.status_code == 401:
                        logger.warning("[MyWhoosh] Token expired (HTTP 401)")
                        return [], "token_expired"
                    if resp.status_code != 200:
                        logger.error(f"[MyWhoosh] Page {page} returned HTTP {resp.status_code}: {resp.text[:200]}")
                        return all_activities, f"MyWhoosh API returned HTTP {resp.status_code}"

                    body = resp.json()
                    data = body.get("data", {})
                    results = data.get("results", [])
                    total_pages = data.get("totalPages", 1)

                    logger.info(f"[MyWhoosh] Page {page}/{total_pages}: received {len(results)} activities")
                    all_activities.extend(results)

                    if page >= total_pages:
                        break

            logger.info(f"[MyWhoosh] Fetched total of {len(all_activities)} activities.")
            return all_activities, None
        except Exception as e:
            logger.error(f"[MyWhoosh] Error fetching MyWhoosh activities: {e}", exc_info=True)
            return all_activities, str(e)

    def parse_duration_seconds(self, duration_str: Optional[str]) -> int:
        """Parses rideDuration string like '01:15:24.000' or '45:10' into seconds."""
        if not duration_str:
            return 0
        try:
            clean = duration_str.split(".")[0].strip()
            parts = [int(p) for p in clean.split(":") if p.isdigit()]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2:
                return parts[0] * 60 + parts[1]
            elif len(parts) == 1:
                return parts[0]
        except Exception:
            pass
        return 0

    def calculate_tss(self, duration_sec: int, avg_watts: Optional[float], avg_hr: Optional[float], profile: AthleteProfile) -> int:
        """Calculates Training Stress Score using power or heart rate."""
        if duration_sec <= 0:
            return 0

        # Method 1: Coggan Power TSS
        if avg_watts and avg_watts > 0 and profile.ftp and profile.ftp > 0:
            intensity_factor = avg_watts / profile.ftp
            tss = (duration_sec * avg_watts * intensity_factor) / (profile.ftp * 3600) * 100
            return int(round(tss))

        # Method 2: Heart Rate TSS estimate
        if avg_hr and avg_hr > 0 and profile.max_hr and profile.max_hr > 0:
            rest_hr = 50.0
            hr_reserve_ratio = max(0.1, (avg_hr - rest_hr) / (profile.max_hr - rest_hr))
            intensity_factor = hr_reserve_ratio * 0.95
            tss = (duration_sec / 3600.0) * (intensity_factor ** 2) * 100
            return int(round(tss))

        # Fallback: Moderate aerobic effort (~50 TSS per hour)
        return int(round((duration_sec / 3600.0) * 50))

    async def sync_activities(self, db: Session, profile: AthleteProfile) -> Dict:
        """
        Full synchronization routine:
        1. Ensures valid token (logs in if needed).
        2. Pulls recent activities.
        3. Saves to database, calculates TSS, matches calendar events.
        4. Triggers Gemini AI coach adaptation.
        """
        if not profile.mywhoosh_email or not profile.mywhoosh_password:
            return {"status": "error", "message": "MyWhoosh email and password are not configured in Settings."}

        logger.info(f"[MyWhoosh] Starting sync_activities for user '{profile.mywhoosh_email}'...")
        try:
            token = profile.mywhoosh_token

            # If no token, log in
            if not token:
                success, new_token, whoosh_id, err = await self.login(profile.mywhoosh_email, profile.mywhoosh_password)
                if not success:
                    return {"status": "error", "message": f"MyWhoosh login failed: {err}"}
                token = new_token
                profile.mywhoosh_token = new_token
                profile.mywhoosh_id = whoosh_id
                db.commit()

            # Fetch activities
            activities, err = await self.fetch_activities(token, max_pages=2)
            if err == "token_expired":
                logger.info("[MyWhoosh] Token expired, attempting re-authentication...")
                success, new_token, whoosh_id, err = await self.login(profile.mywhoosh_email, profile.mywhoosh_password)
                if not success:
                    return {"status": "error", "message": f"MyWhoosh re-authentication failed: {err}"}
                token = new_token
                profile.mywhoosh_token = new_token
                profile.mywhoosh_id = whoosh_id
                db.commit()
                activities, err = await self.fetch_activities(token, max_pages=2)

            if err and not activities:
                return {"status": "error", "message": f"Failed to fetch activities: {err}"}

            new_count = 0
            latest_act = None

            for act in activities:
                act_id = str(act.get("id") or act.get("activityFileId"))
                if not act_id:
                    continue

                # Check if activity already recorded
                existing = db.query(RideActivity).filter(RideActivity.id == f"mw_{act_id}").first()
                if existing:
                    continue

                # Extract metrics
                title = act.get("title") or act.get("routeName") or "MyWhoosh Ride"
                duration_str = act.get("rideDuration")
                duration_sec = self.parse_duration_seconds(duration_str)
                distance_km = float(act.get("distance") or 0.0)
                avg_watts = float(act.get("watt") or 0.0) if act.get("watt") else None
                avg_hr = float(act.get("heartrate") or 0.0) if act.get("heartrate") else None
                elevation = float(act.get("elevation") or 0.0)

                # Date handling
                start_datetime = act.get("startDatetime")
                if start_datetime:
                    date_str = start_datetime[:10]
                else:
                    timestamp = act.get("date")
                    if timestamp:
                        dt = datetime.datetime.utcfromtimestamp(int(timestamp))
                        date_str = dt.strftime("%Y-%m-%d")
                        start_datetime = dt.isoformat()
                    else:
                        date_str = datetime.date.today().isoformat()
                        start_datetime = datetime.datetime.utcnow().isoformat()

                calculated_tss = self.calculate_tss(duration_sec, avg_watts, avg_hr, profile)

                # Match or create calendar event
                matched_event = db.query(CalendarEvent).filter(
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
                    matched_event.activity_id = f"mw_{act_id}"
                    matched_id = matched_event.id
                    logger.info(f"[MyWhoosh] Matched activity '{title}' to planned calendar event on {date_str} (TSS: {calculated_tss})")
                else:
                    # Unplanned ride on calendar
                    new_cal_event = CalendarEvent(
                        title=f"{title}",
                        date=date_str,
                        event_type="workout",
                        primary_zone="Free Ride",
                        actual_tss=calculated_tss,
                        actual_duration_minutes=round(duration_sec / 60.0, 1),
                        actual_avg_watts=avg_watts,
                        actual_avg_hr=avg_hr,
                        status="completed",
                        activity_id=f"mw_{act_id}",
                        athlete_notes=f"Completed in MyWhoosh: {distance_km:.1f} km, {avg_watts or 0:.0f}W avg."
                    )
                    db.add(new_cal_event)
                    db.flush()
                    matched_id = new_cal_event.id
                    logger.info(f"[MyWhoosh] Created new completed calendar event '{title}' on {date_str} (TSS: {calculated_tss})")

                db_act = RideActivity(
                    id=f"mw_{act_id}",
                    name=title,
                    type="VirtualRide",
                    start_date=start_datetime,
                    start_date_local=start_datetime,
                    elapsed_time=duration_sec,
                    moving_time=duration_sec,
                    distance=distance_km * 1000.0,
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
            # Remove past uncompleted training workouts
            # (Strictly cleans past dates where status != 'completed', while preserving target races, custom rides, and rest days)
            today_str = datetime.date.today().strftime("%Y-%m-%d")
            cleaned_past = db.query(CalendarEvent).filter(
                CalendarEvent.date < today_str,
                CalendarEvent.status != "completed",
                CalendarEvent.event_type.notin_(["race", "custom", "rest"])
            ).delete(synchronize_session=False)

            if cleaned_past > 0:
                logger.info(f"[MyWhoosh] Cleaned up {cleaned_past} expired past uncompleted workout(s) before {today_str}.")

            profile.last_mywhoosh_sync = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            db.commit()

            # Trigger Gemini adaptation if new ride was logged
            adaptation_notes = None
            if new_count > 0 and latest_act and profile.gemini_api_key:
                try:
                    matched_cal_event = db.query(CalendarEvent).filter(CalendarEvent.id == latest_act.matched_event_id).first()
                    if matched_cal_event:
                        adapt_res = await adapt_calendar_after_activity(db, matched_cal_event, latest_act)
                        adaptation_notes = adapt_res.get("debrief")
                except Exception as e:
                    logger.warning(f"AI Coach adaptation error: {e}")

            logger.info(f"[MyWhoosh] Sync completed successfully: {new_count} new ride(s) logged, {cleaned_past} expired past workout(s) removed.")
            return {
                "status": "success",
                "new_activities": new_count,
                "cleaned_past_workouts": cleaned_past,
                "last_sync": profile.last_mywhoosh_sync,
                "adaptation": adaptation_notes
            }
        except Exception as e:
            logger.error(f"[MyWhoosh] Sync exception: {e}", exc_info=True)
            return {"status": "error", "message": f"MyWhoosh sync failed: {str(e)}"}

mywhoosh_service = MyWhooshService()
