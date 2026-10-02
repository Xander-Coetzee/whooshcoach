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

    async def fetch_calendar_tasks(self, token: str, days_past: int = 90, days_future: int = 30) -> Tuple[List[Dict], Optional[str]]:
        """
        Fetches calendar tasks from MyWhoosh Unified Calendar endpoint.
        This includes inside workouts and outdoor rides synced from Strava.
        Returns (taskList, error_message).
        """
        now = int(datetime.datetime.utcnow().timestamp())
        start_epoch = now - (days_past * 86400)
        end_epoch = now + (days_future * 86400)
        url = f"https://service14.mywhoosh.com/v1/task/date-range-task-list?startDate={start_epoch}&endDate={end_epoch}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
        logger.info(f"[MyWhoosh] Fetching unified calendar tasks ({days_past}d past to {days_future}d future)...")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 401:
                    return [], "token_expired"
                if resp.status_code != 200:
                    logger.error(f"[MyWhoosh] Task endpoint returned HTTP {resp.status_code}: {resp.text[:200]}")
                    return [], f"Task API returned HTTP {resp.status_code}"
                data = resp.json()
                task_list = data.get("data", {}).get("taskList", [])
                logger.info(f"[MyWhoosh] Unified calendar returned {len(task_list)} task(s).")
                return task_list, None
        except Exception as e:
            logger.error(f"[MyWhoosh] Error fetching calendar tasks: {e}", exc_info=True)
            return [], str(e)

    def is_inside_ride(self, task_or_act: Dict) -> bool:
        """
        Determines whether a task or activity is an inside virtual MyWhoosh ride
        or an outside ride synced from Strava.
        """
        # 1. MapId check: In MyWhoosh, MapId > 0 indicates an in-game virtual island/world
        map_id = task_or_act.get("MapId")
        try:
            if map_id is not None and int(map_id) > 0:
                return True
        except (ValueError, TypeError):
            pass

        # 2. Check title / TaskName
        name = (task_or_act.get("TaskName") or task_or_act.get("title") or task_or_act.get("name") or "").strip().lower()
        if "mywhoosh" in name:
            return True

        # 3. Check route name
        route_name = (task_or_act.get("routeName") or "").strip().lower()
        if route_name:
            return True

        # 4. Known MyWhoosh virtual worlds / routes
        known_virtual_worlds = [
            "amazonia", "the muur", "muur", "oudenaarde", "paris classic", 
            "mompox", "alula", "hudayriyat", "zurich", "hautacam", 
            "jabel hafeet", "san francisco", "mount grizzly", "ardennes", 
            "al qudra", "watopia", "makuri", "innsbruck", "london", "richmond", "yorkshire"
        ]
        if any(w in name for w in known_virtual_worlds):
            return True

        # 5. Check TaskType
        task_type = task_or_act.get("TaskType") or ""
        if task_type in ["E_Simple_Workout", "E_GroupWorkout"]:
            return True

        # Otherwise, this is an outdoor ride from Strava
        return False


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
        2. Pulls inside activities from rider profile.
        3. Pulls unified calendar tasks (including outside Strava rides).
        4. Classifies each ride cleanly:
           - Inside MyWhoosh: type='VirtualRide', event_type='workout'
           - Outside Strava: type='Ride', event_type='outdoor_ride', primary_zone='Outdoor Ride'
        5. Performs strict deduplication to ensure indoor rides pushed to Strava are NOT double-counted.
        6. Removes past uncompleted training workouts (preserving races, custom, rest, and outdoor rides).
        7. Triggers Gemini AI coach adaptation with clear indoor vs outdoor fatigue context.
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

            # 1. Fetch inside rider activities
            activities, err = await self.fetch_activities(token, max_pages=3)
            if err == "token_expired":
                logger.info("[MyWhoosh] Token expired, attempting re-authentication...")
                success, new_token, whoosh_id, err = await self.login(profile.mywhoosh_email, profile.mywhoosh_password)
                if not success:
                    return {"status": "error", "message": f"MyWhoosh re-authentication failed: {err}"}
                token = new_token
                profile.mywhoosh_token = new_token
                profile.mywhoosh_id = whoosh_id
                db.commit()
                activities, err = await self.fetch_activities(token, max_pages=3)

            # 2. Fetch unified calendar tasks (including outside rides from Strava)
            tasks, task_err = await self.fetch_calendar_tasks(token, days_past=90, days_future=30)
            if task_err == "token_expired":
                success, new_token, whoosh_id, err = await self.login(profile.mywhoosh_email, profile.mywhoosh_password)
                if success:
                    token = new_token
                    profile.mywhoosh_token = new_token
                    db.commit()
                    tasks, task_err = await self.fetch_calendar_tasks(token, days_past=90, days_future=30)

            new_indoor_count = 0
            new_outdoor_count = 0
            latest_act = None

            # Pre-load existing activities for this athlete to perform fast deduplication
            existing_rides = db.query(RideActivity).filter(RideActivity.athlete_id == profile.id).all()
            existing_ids = {r.id for r in existing_rides}
            # Track start epochs of existing rides (+/- 10 min window to catch mirror uploads)
            existing_start_epochs = []
            for r in existing_rides:
                if r.start_date_local:
                    try:
                        clean_iso = r.start_date_local.replace("Z", "+00:00")
                        dt = datetime.datetime.fromisoformat(clean_iso)
                        existing_start_epochs.append(int(dt.timestamp()))
                    except Exception:
                        pass

            # --- PROCESS INSIDE RIDER ACTIVITIES (MyWhoosh) ---
            for act in activities:
                act_id = str(act.get("id") or act.get("activityFileId"))
                if not act_id:
                    continue

                act_db_id = f"mw_{act_id}"
                if act_db_id in existing_ids:
                    continue

                title = act.get("title") or act.get("routeName") or "MyWhoosh Ride"
                duration_str = act.get("rideDuration")
                duration_sec = self.parse_duration_seconds(duration_str)
                distance_km = float(act.get("distance") or 0.0)
                avg_watts = float(act.get("watt") or 0.0) if act.get("watt") else None
                avg_hr = float(act.get("heartrate") or 0.0) if act.get("heartrate") else None
                elevation = float(act.get("elevation") or 0.0)

                start_datetime = act.get("startDatetime")
                start_epoch = 0
                if start_datetime:
                    date_str = start_datetime[:10]
                    try:
                        clean_iso = start_datetime.replace("Z", "+00:00")
                        dt = datetime.datetime.fromisoformat(clean_iso)
                        start_epoch = int(dt.timestamp())
                    except Exception:
                        pass
                else:
                    timestamp = act.get("date")
                    if timestamp:
                        dt = datetime.datetime.fromtimestamp(int(timestamp), datetime.UTC)
                        date_str = dt.strftime("%Y-%m-%d")
                        start_datetime = dt.isoformat()
                        start_epoch = int(timestamp)
                    else:
                        date_str = datetime.date.today().isoformat()
                        start_datetime = datetime.datetime.utcnow().isoformat()

                calculated_tss = self.calculate_tss(duration_sec, avg_watts, avg_hr, profile)

                # Match or create calendar event for this athlete
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
                    matched_event.activity_id = act_db_id
                    matched_id = matched_event.id
                    logger.info(f"[MyWhoosh] Matched inside ride '{title}' to planned event on {date_str} (TSS: {calculated_tss})")
                else:
                    new_cal_event = CalendarEvent(
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
                        activity_id=act_db_id,
                        athlete_notes=f"Indoor virtual ride completed in MyWhoosh: {distance_km:.1f} km, {avg_watts or 0:.0f}W avg."
                    )
                    db.add(new_cal_event)
                    db.flush()
                    matched_id = new_cal_event.id
                    logger.info(f"[MyWhoosh] Created completed inside event '{title}' on {date_str} (TSS: {calculated_tss})")

                db_act = RideActivity(
                    id=act_db_id,
                    athlete_id=profile.id,
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
                existing_ids.add(act_db_id)
                if start_epoch > 0:
                    existing_start_epochs.append(start_epoch)
                new_indoor_count += 1
                if not latest_act:
                    latest_act = db_act

            # --- PROCESS UNIFIED CALENDAR TASKS (OUTDOOR STRAVA & EXTRA TASKS) ---
            for t in tasks:
                if t.get("TaskState") != "E_Completed":
                    continue

                task_id = str(t.get("TaskId") or "")
                if not task_id:
                    continue

                start_epoch = int(t.get("TaskStartedTimeEpoc") or 0)
                end_epoch = int(t.get("TaskEndEpochTime") or 0)
                task_name = (t.get("TaskName") or "Cycling Activity").strip()
                distance_km = float(t.get("TotalKilometers") or 0.0)
                elevation = float(t.get("TotalElevation") or 0.0)
                task_tss = int(t.get("TSS") or 0)

                # Duration calculation
                if end_epoch > start_epoch > 0:
                    duration_sec = end_epoch - start_epoch
                elif distance_km > 0:
                    duration_sec = int((distance_km / 25.0) * 3600)
                else:
                    duration_sec = 0

                # Strict inside vs outside classification
                inside = self.is_inside_ride(t)

                # DEDUPLICATION CHECK:
                # 1. Direct ID match
                if f"mw_{task_id}" in existing_ids or f"strava_mw_{task_id}" in existing_ids or f"mw_task_{task_id}" in existing_ids:
                    continue

                # 2. Timestamp cross-check: if a ride already exists within +/- 10 mins (600s),
                # this is the SAME session (e.g. MyWhoosh uploaded it to Strava, and MyWhoosh unified calendar pulled it back).
                # NEVER duplicate or confuse it!
                if start_epoch > 0:
                    is_duplicate_session = any(abs(start_epoch - ex_epoch) < 600 for ex_epoch in existing_start_epochs)
                    if is_duplicate_session:
                        continue

                # Date string
                if start_epoch > 0:
                    dt = datetime.datetime.fromtimestamp(start_epoch, datetime.UTC)
                    date_str = dt.strftime("%Y-%m-%d")
                    start_datetime = dt.isoformat()
                else:
                    date_str = datetime.date.today().isoformat()
                    start_datetime = datetime.datetime.utcnow().isoformat()

                if inside:
                    # Inside virtual ride (not already captured by activities list)
                    target_id = f"mw_task_{task_id}"
                    ride_type = "VirtualRide"
                    event_type = "workout"
                    primary_zone = "Free Ride"
                    notes = f"Completed in MyWhoosh: {distance_km:.1f} km, {elevation:.0f}m elev."
                    cal_title = task_name
                else:
                    # TRUE OUTSIDE RIDE FROM STRAVA!
                    target_id = f"strava_mw_{task_id}"
                    ride_type = "Ride"
                    event_type = "outdoor_ride"
                    primary_zone = "Outdoor Ride"
                    notes = f"Outside ride synced from Strava via MyWhoosh: {distance_km:.1f} km, {elevation:.0f}m elevation."
                    cal_title = f"🚴‍♂️ {task_name}"

                calculated_tss = task_tss if task_tss > 0 else self.calculate_tss(duration_sec, None, None, profile)

                # Match or create calendar event
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
                    matched_event.activity_id = target_id
                    matched_event.athlete_notes = notes
                    matched_id = matched_event.id
                    logger.info(f"[MyWhoosh] Matched task '{task_name}' ({ride_type}) to planned event on {date_str} (TSS: {calculated_tss})")
                else:
                    new_cal_event = CalendarEvent(
                        athlete_id=profile.id,
                        title=cal_title,
                        date=date_str,
                        event_type=event_type,
                        primary_zone=primary_zone,
                        actual_tss=calculated_tss,
                        actual_duration_minutes=round(duration_sec / 60.0, 1),
                        status="completed",
                        activity_id=target_id,
                        athlete_notes=notes
                    )
                    db.add(new_cal_event)
                    db.flush()
                    matched_id = new_cal_event.id
                    logger.info(f"[MyWhoosh] Ingested completed {ride_type} '{cal_title}' on {date_str} (TSS: {calculated_tss})")

                db_act = RideActivity(
                    id=target_id,
                    athlete_id=profile.id,
                    name=task_name,
                    type=ride_type,
                    start_date=start_datetime,
                    start_date_local=start_datetime,
                    elapsed_time=duration_sec,
                    moving_time=duration_sec,
                    distance=distance_km * 1000.0,
                    total_elevation_gain=elevation,
                    calculated_tss=calculated_tss,
                    matched_event_id=matched_id,
                    raw_json=json.dumps(t),
                    synced_at=datetime.datetime.utcnow()
                )
                db.add(db_act)
                existing_ids.add(target_id)
                if start_epoch > 0:
                    existing_start_epochs.append(start_epoch)

                if inside:
                    new_indoor_count += 1
                else:
                    new_outdoor_count += 1
                    logger.info(f"[MyWhoosh] CONFIRMED: Ingested OUTSIDE ride '{task_name}' from Strava on {date_str} ({distance_km:.1f} km, {elevation:.0f}m elevation, TSS: {calculated_tss}).")

                if not latest_act:
                    latest_act = db_act

            # Remove past uncompleted training workouts for this athlete
            # (Strictly cleans past dates where status != 'completed', while preserving target races, custom rides, rest days, and outdoor rides)
            today_str = datetime.date.today().strftime("%Y-%m-%d")
            cleaned_past = db.query(CalendarEvent).filter(
                CalendarEvent.athlete_id == profile.id,
                CalendarEvent.date < today_str,
                CalendarEvent.status != "completed",
                CalendarEvent.event_type.notin_(["race", "custom", "rest", "outdoor_ride"])
            ).delete(synchronize_session=False)

            if cleaned_past > 0:
                logger.info(f"[MyWhoosh] Cleaned up {cleaned_past} expired past uncompleted workout(s) before {today_str} for athlete {profile.name}.")

            profile.last_mywhoosh_sync = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            db.commit()

            total_new = new_indoor_count + new_outdoor_count

            # Trigger Gemini adaptation if new ride was logged
            adaptation_notes = None
            if total_new > 0 and latest_act and profile.gemini_api_key:
                try:
                    matched_cal_event = db.query(CalendarEvent).filter(CalendarEvent.id == latest_act.matched_event_id).first()
                    if matched_cal_event:
                        adapt_res = await adapt_calendar_after_activity(db, matched_cal_event, latest_act)
                        adaptation_notes = adapt_res.get("debrief")
                except Exception as e:
                    logger.warning(f"AI Coach adaptation error: {e}")

            logger.info(f"[MyWhoosh] Sync completed successfully: {new_indoor_count} indoor ride(s), {new_outdoor_count} outside Strava ride(s), {cleaned_past} expired past workout(s) removed.")
            return {
                "status": "success",
                "new_activities": total_new,
                "new_indoor_rides": new_indoor_count,
                "new_outdoor_rides": new_outdoor_count,
                "cleaned_past_workouts": cleaned_past,
                "last_sync": profile.last_mywhoosh_sync,
                "adaptation": adaptation_notes
            }
        except Exception as e:
            logger.error(f"[MyWhoosh] Sync exception: {e}", exc_info=True)
            return {"status": "error", "message": f"MyWhoosh sync failed: {str(e)}"}

mywhoosh_service = MyWhooshService()
