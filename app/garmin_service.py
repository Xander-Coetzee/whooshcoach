import os
import json
import logging
import datetime
from typing import Dict, Any, Tuple, Optional, List
from sqlalchemy.orm import Session
from app.database import AthleteProfile, CalendarEvent, RideActivity, DailyWellness
from app.physiology import calculate_session_tss

logger = logging.getLogger("whooshcoach.garmin")

class GarminService:
    def __init__(self):
        pass

    def _get_client(self, email: str, password: Optional[str] = None, tokens_json: Optional[str] = None):
        """
        Creates and authenticates a Garmin client.
        Uses cached tokens JSON if available, falling back to credentials.
        """
        from garminconnect import (
            Garmin,
            GarminConnectAuthenticationError,
            GarminConnectConnectionError,
            GarminConnectTooManyRequestsError,
        )

        client = Garmin(email, password)
        
        # 1. Try restoring from tokenstore (inline json)
        if tokens_json:
            try:
                logger.info(f"[Garmin] Attempting session restoration with cached tokens for '{email}'...")
                client.login(tokenstore=tokens_json)
                return client, None, None
            except Exception as e:
                logger.warning(f"[Garmin] Token restoration failed for '{email}' ({e}), falling back to credentials...")

        # 2. Fresh login with username and password
        if not email or not password:
            return None, None, "Missing Garmin email or password."

        try:
            logger.info(f"[Garmin] Performing fresh credential login for '{email}'...")
            client.login()
            new_tokens = client.client.dumps()
            display_name = client.get_full_name() or client.username
            return client, new_tokens, None
        except GarminConnectAuthenticationError as e:
            logger.error(f"[Garmin] Authentication failed for '{email}': {e}")
            return None, None, f"Invalid Garmin email or password: {e}"
        except GarminConnectTooManyRequestsError as e:
            logger.error(f"[Garmin] Rate limited by Garmin SSO for '{email}': {e}")
            return None, None, "Garmin SSO rate limit reached. Please wait a few minutes before retrying."
        except GarminConnectConnectionError as e:
            logger.error(f"[Garmin] Connection error for '{email}': {e}")
            return None, None, f"Connection to Garmin Connect failed: {e}"
        except Exception as e:
            logger.error(f"[Garmin] Unexpected login error for '{email}': {e}")
            return None, None, f"Garmin login error: {e}"

    def _format_weather(self, weather_data: Optional[Dict[str, Any]], ambient_temp_c: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Extracts and normalizes weather parameters (temperature, station, conditions, humidity, wind)."""
        if not weather_data or not isinstance(weather_data, dict):
            if ambient_temp_c is not None:
                return {"temp_c": ambient_temp_c, "condition": None, "station": None}
            return None
        station = (weather_data.get("weatherStationDTO") or {}).get("name")
        temp = weather_data.get("temp")
        temp_c = None
        if temp is not None:
            # Garmin weather temperature is in Fahrenheit if > 40 in normal daytime range
            temp_c = round((temp - 32) * 5 / 9, 1) if temp > 40 else round(temp, 1)
        elif ambient_temp_c is not None:
            temp_c = ambient_temp_c
        desc = (weather_data.get("weatherTypeDTO") or {}).get("desc")
        humidity = weather_data.get("relativeHumidity")
        wind_mph = weather_data.get("windSpeed")
        wind_kmh = round(wind_mph * 1.60934, 1) if wind_mph is not None else None
        return {
            "temp_c": temp_c,
            "condition": desc,
            "station": station,
            "humidity": humidity,
            "wind_kmh": wind_kmh
        }

    def _build_notes(self, act: Dict[str, Any], weather_info: Optional[Dict[str, Any]] = None, device_name: Optional[str] = None) -> str:
        """Constructs a high-signal, information-dense note string for coaching context."""
        dist_km = (act.get("distance") or 0.0) / 1000.0
        elev_m = act.get("elevationGain") or 0.0
        avg_w = act.get("averageWatts")
        norm_w = act.get("normPower") or act.get("weightedAveragePower")
        avg_hr = act.get("averageHR")
        max_hr = act.get("maxHR")
        calories = act.get("calories")

        parts = [f"Outdoor ride from Garmin: {dist_km:.1f} km, {elev_m:.0f}m elev"]
        if avg_w:
            w_str = f"{avg_w:.0f}W avg"
            if norm_w: w_str += f" (NP: {norm_w:.0f}W)"
            parts.append(w_str)
        if avg_hr:
            hr_str = f"{avg_hr:.0f} bpm avg"
            if max_hr: hr_str += f" (max {max_hr:.0f} bpm)"
            parts.append(hr_str)
        if weather_info and weather_info.get("temp_c") is not None:
            w_str = f"{weather_info['temp_c']}°C"
            if weather_info.get("condition"): w_str += f" {weather_info['condition']}"
            if weather_info.get("station"): w_str += f" @ {weather_info['station']}"
            parts.append(w_str)
        if calories:
            parts.append(f"{calories:.0f} kcal")
        if device_name:
            parts.append(f"Recorded on {device_name}")

        return ", ".join(parts) + "."

    def test_connection(self, email: str, password: str) -> Dict[str, Any]:
        """Validates Garmin Connect login and returns account display name."""
        try:
            client, new_tokens, err = self._get_client(email, password)
            if err:
                return {"status": "error", "message": err}
            
            try:
                display_name = client.get_full_name() or getattr(client, "username", None) or email.split("@")[0]
            except Exception:
                display_name = getattr(client, "username", None) or email.split("@")[0]
            return {
                "status": "success",
                "message": f"Successfully connected to Garmin Connect as '{display_name}'!",
                "display_name": display_name,
                "tokens_json": new_tokens
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def sync_activities_and_wellness(self, db: Session, profile: AthleteProfile, days_back: int = 30) -> Dict[str, Any]:
        """
        Synchronizes recent outdoor rides and daily health metrics (HRV, Sleep, Resting HR)
        from Garmin Connect directly into WhooshCoach.
        """
        if not profile.garmin_email:
            return {"status": "error", "message": "Garmin email is not configured."}

        client, new_tokens, err = self._get_client(
            email=profile.garmin_email,
            password=profile.garmin_password,
            tokens_json=profile.garmin_tokens
        )
        if err:
            return {"status": "error", "message": err}

        if new_tokens:
            profile.garmin_tokens = new_tokens
            db.commit()

        # Build device ID map (e.g. Edge 820, Edge 840 Solar)
        device_map = {}
        try:
            devices = client.get_devices() or []
            for d in devices:
                dev_id = d.get("deviceId")
                name = d.get("productDisplayName") or d.get("deviceTypeName")
                if dev_id and name:
                    device_map[dev_id] = name
        except Exception as e:
            logger.debug(f"[Garmin] Device fetch skipped: {e}")

        # Check for auto-detected Cycling FTP on Garmin device
        try:
            ftp_res = client.get_cycling_ftp()
            if ftp_res and isinstance(ftp_res, dict):
                detected_ftp = ftp_res.get("functionalThresholdPower")
                cal_date = ftp_res.get("calendarDate")
                if detected_ftp:
                    profile.garmin_ftp = int(detected_ftp)
                    profile.garmin_ftp_date = str(cal_date)[:10] if cal_date else None
                    db.commit()
                    logger.info(f"[Garmin] Detected Cycling FTP: {detected_ftp} W ({profile.garmin_ftp_date})")
        except Exception as e:
            logger.debug(f"[Garmin] Cycling FTP fetch skipped: {e}")

        # -------------------------------------------------------------
        # 1. SYNC ACTIVITIES (CYCLING RIDES)
        # -------------------------------------------------------------
        new_activities_count = 0
        latest_act = None

        try:
            logger.info(f"[Garmin] Fetching recent activities for '{profile.name}'...")
            raw_activities = client.get_activities(0, limit=40) or []
        except Exception as e:
            logger.error(f"[Garmin] Failed to fetch activities: {e}")
            return {"status": "error", "message": f"Failed to fetch activities from Garmin: {e}"}

        # Pre-load existing activities to perform deduplication & backfill
        existing_rides = db.query(RideActivity).filter(RideActivity.athlete_id == profile.id).all()
        existing_ids = {r.id for r in existing_rides}
        existing_start_epochs = []
        for r in existing_rides:
            if r.start_date_local:
                try:
                    clean_iso = r.start_date_local.replace("Z", "+00:00")
                    dt = datetime.datetime.fromisoformat(clean_iso)
                    existing_start_epochs.append(int(dt.timestamp()))
                except Exception:
                    pass

        cycling_types = {
            "cycling", "road_biking", "gravel_cycling", "mountain_biking",
            "virtual_ride", "indoor_cycling", "bmx", "track_cycling",
            "cyclocross", "e_bike_fitness", "e_bike_mountain"
        }

        for act in raw_activities:
            act_id = str(act.get("activityId") or "")
            if not act_id:
                continue

            act_db_id = f"garmin_{act_id}"
            if act_db_id in existing_ids:
                # Backfill weather, device name, and rich notes on existing records if missing
                existing_act = db.query(RideActivity).filter(RideActivity.id == act_db_id).first()
                if existing_act and existing_act.raw_json:
                    try:
                        act_data = json.loads(existing_act.raw_json)
                        if "weather_info" not in act_data:
                            weather_data = None
                            try:
                                weather_data = client.get_activity_weather(act_id)
                            except Exception:
                                pass
                            ambient_t = act_data.get("minTemperature") or act_data.get("maxTemperature")
                            w_info = self._format_weather(weather_data, ambient_t)
                            act_data["weather_info"] = w_info
                            dev_name = device_map.get(act_data.get("deviceId"))
                            if dev_name:
                                act_data["device_name"] = dev_name
                            existing_act.raw_json = json.dumps(act_data)
                            cal_ev = db.query(CalendarEvent).filter(CalendarEvent.activity_id == act_db_id).first()
                            if cal_ev and (not cal_ev.athlete_notes or "Outdoor ride from Garmin" in cal_ev.athlete_notes):
                                cal_ev.athlete_notes = self._build_notes(act_data, w_info, dev_name)
                            db.commit()
                    except Exception as e:
                        logger.debug(f"[Garmin] Backfill error for {act_db_id}: {e}")
                continue

            # Filter for cycling / bike activities
            type_obj = act.get("activityType") or {}
            type_key = str(type_obj.get("typeKey", "")).lower()
            if not any(k in type_key for k in ["cycl", "bike", "biking"]) and type_key not in cycling_types:
                continue

            title = act.get("activityName") or "Outdoor Ride"
            duration_sec = int(act.get("movingDuration") or act.get("duration") or 0)
            if duration_sec <= 60:
                continue

            distance_m = float(act.get("distance") or 0.0)
            elevation_m = float(act.get("elevationGain") or 0.0)
            avg_watts = float(act.get("averageWatts")) if act.get("averageWatts") is not None else None
            norm_power = float(act.get("normPower") or act.get("weightedAveragePower")) if (act.get("normPower") or act.get("weightedAveragePower")) else None
            max_watts = float(act.get("maxWatts")) if act.get("maxWatts") is not None else None
            avg_hr = float(act.get("averageHR")) if act.get("averageHR") is not None else None
            max_hr = float(act.get("maxHR")) if act.get("maxHR") is not None else None
            avg_cadence = float(act.get("averageRunningCadenceInStepsPerMinute") or act.get("avgCadence")) if (act.get("averageRunningCadenceInStepsPerMinute") or act.get("avgCadence")) else None

            start_datetime = act.get("startTimeLocal") or act.get("startTimeGMT")
            start_epoch = 0
            date_str = datetime.date.today().isoformat()
            if start_datetime:
                try:
                    # Garmin format: "2026-10-10 08:30:15"
                    dt = datetime.datetime.fromisoformat(start_datetime)
                    date_str = dt.strftime("%Y-%m-%d")
                    start_epoch = int(dt.timestamp())
                except Exception:
                    date_str = start_datetime[:10]

            # Cross-platform deduplication: Check +/- 15 min window against existing rides
            if start_epoch > 0:
                duplicate_found = False
                for ex_epoch in existing_start_epochs:
                    if abs(ex_epoch - start_epoch) < 900: # 15 minutes
                        duplicate_found = True
                        break
                if duplicate_found:
                    logger.info(f"[Garmin] Skipping activity '{title}' ({date_str}) — matches existing workout time window.")
                    continue

            # Deterministic TSS Calculation
            if norm_power or avg_watts:
                calc_tss = calculate_session_tss(
                    duration_seconds=duration_sec,
                    weighted_watts=norm_power,
                    avg_watts=avg_watts,
                    ftp=profile.ftp or 220
                )
            elif avg_hr and profile.max_hr:
                hr_ratio = avg_hr / max(100, profile.max_hr)
                calc_tss = int(round((duration_sec / 3600.0) * (hr_ratio ** 2) * 100.0 * 0.9))
            else:
                # Moderate endurance estimation (~55 TSS per hour)
                calc_tss = int(round((duration_sec / 3600.0) * 55.0))

            calc_tss = max(5, calc_tss)

            # Fetch weather and device context
            weather_data = None
            try:
                weather_data = client.get_activity_weather(act_id)
            except Exception:
                pass
            ambient_t = act.get("minTemperature") or act.get("maxTemperature")
            weather_info = self._format_weather(weather_data, ambient_t)
            act["weather_info"] = weather_info
            dev_name = device_map.get(act.get("deviceId"))
            if dev_name:
                act["device_name"] = dev_name

            athlete_notes_text = self._build_notes(act, weather_info, dev_name)

            # Match or create calendar event for this athlete
            matched_event = db.query(CalendarEvent).filter(
                CalendarEvent.athlete_id == profile.id,
                CalendarEvent.date == date_str,
                CalendarEvent.status.in_(["planned", "scheduled"])
            ).first()

            matched_id = None
            if matched_event:
                matched_event.status = "completed"
                matched_event.actual_tss = calc_tss
                matched_event.actual_duration_minutes = round(duration_sec / 60.0, 1)
                matched_event.actual_avg_watts = avg_watts
                matched_event.actual_avg_hr = avg_hr
                matched_event.activity_id = act_db_id
                matched_event.athlete_notes = athlete_notes_text
                matched_id = matched_event.id
                logger.info(f"[Garmin] Matched outdoor ride '{title}' to planned event on {date_str} (TSS: {calc_tss})")
            else:
                new_cal_event = CalendarEvent(
                    athlete_id=profile.id,
                    title=f"{title}",
                    date=date_str,
                    event_type="workout",
                    primary_zone="Outdoor Ride",
                    actual_tss=calc_tss,
                    actual_duration_minutes=round(duration_sec / 60.0, 1),
                    actual_avg_watts=avg_watts,
                    actual_avg_hr=avg_hr,
                    status="completed",
                    activity_id=act_db_id,
                    athlete_notes=athlete_notes_text
                )
                db.add(new_cal_event)
                db.flush()
                matched_id = new_cal_event.id
                logger.info(f"[Garmin] Created completed outdoor event '{title}' on {date_str} (TSS: {calc_tss})")

            db_act = RideActivity(
                id=act_db_id,
                athlete_id=profile.id,
                name=title,
                type="Ride",
                start_date=start_datetime or date_str,
                start_date_local=start_datetime or date_str,
                elapsed_time=duration_sec,
                moving_time=duration_sec,
                distance=distance_m,
                total_elevation_gain=elevation_m,
                average_watts=avg_watts,
                weighted_average_watts=norm_power,
                max_watts=max_watts,
                average_heartrate=avg_hr,
                max_heartrate=max_hr,
                average_cadence=avg_cadence,
                calculated_tss=calc_tss,
                matched_event_id=matched_id,
                raw_json=json.dumps(act),
                synced_at=datetime.datetime.utcnow()
            )
            db.add(db_act)
            existing_ids.add(act_db_id)
            if start_epoch > 0:
                existing_start_epochs.append(start_epoch)
            new_activities_count += 1
            if not latest_act:
                latest_act = db_act

        # -------------------------------------------------------------
        # 2. SYNC WELLNESS & RECOVERY METRICS (PAST 5 DAYS)
        # -------------------------------------------------------------
        wellness_count = 0
        today = datetime.date.today()
        for i in range(5):
            day_str = (today - datetime.timedelta(days=i)).isoformat()
            try:
                # 1. Daily User Stats (Resting Heart Rate, Body Battery, Stress)
                stats = client.get_stats(day_str) or {}
                resting_hr = stats.get("restingHeartRate") or stats.get("minHeartRate")
                stress_lvl = stats.get("averageStressLevel") or stats.get("maxStressLevel")
                body_batt = stats.get("bodyBatteryMostRecentValue")

                # 2. Sleep Data
                sleep_score = None
                sleep_sec = None
                try:
                    sleep = client.get_sleep_data(day_str) or {}
                    daily_sleep_dto = sleep.get("dailySleepDTO") or {}
                    sleep_sec = daily_sleep_dto.get("sleepTimeSeconds")
                    sleep_score = daily_sleep_dto.get("sleepScores", {}).get("overall", {}).get("value")
                except Exception:
                    pass

                # 3. Heart Rate Variability (HRV)
                hrv_status = None
                hrv_weekly_avg = None
                hrv_last_night_avg = None
                try:
                    hrv = client.get_hrv_data(day_str) or {}
                    hrv_summary = hrv.get("hrvSummary") or {}
                    hrv_status = hrv_summary.get("status") # BALANCED, UNBALANCED, LOW, POOR
                    hrv_weekly_avg = hrv_summary.get("weeklyAvg")
                    hrv_last_night_avg = hrv_summary.get("lastNightAvg")
                except Exception:
                    pass

                # Only persist if we obtained meaningful physiological markers
                if any([resting_hr, hrv_status, sleep_score, sleep_sec]):
                    existing_w = db.query(DailyWellness).filter(
                        DailyWellness.athlete_id == profile.id,
                        DailyWellness.date == day_str
                    ).first()

                    if existing_w:
                        if resting_hr: existing_w.resting_hr = resting_hr
                        if hrv_status: existing_w.hrv_status = hrv_status
                        if hrv_weekly_avg: existing_w.hrv_weekly_avg = hrv_weekly_avg
                        if hrv_last_night_avg: existing_w.hrv_last_night_avg = hrv_last_night_avg
                        if sleep_score: existing_w.sleep_score = sleep_score
                        if sleep_sec: existing_w.sleep_seconds = sleep_sec
                        if body_batt: existing_w.body_battery = body_batt
                        if stress_lvl: existing_w.stress_level = stress_lvl
                        existing_w.updated_at = datetime.datetime.utcnow()
                    else:
                        new_w = DailyWellness(
                            athlete_id=profile.id,
                            date=day_str,
                            resting_hr=resting_hr,
                            hrv_status=hrv_status,
                            hrv_weekly_avg=hrv_weekly_avg,
                            hrv_last_night_avg=hrv_last_night_avg,
                            sleep_score=sleep_score,
                            sleep_seconds=sleep_sec,
                            body_battery=body_batt,
                            stress_level=stress_lvl,
                            source="garmin"
                        )
                        db.add(new_w)
                    wellness_count += 1
            except Exception as e:
                logger.debug(f"[Garmin] Wellness fetch skipped for {day_str}: {e}")

        # Update last sync timestamp
        now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        profile.last_garmin_sync = now_str
        db.commit()

        # Trigger AI Coach Adaptation if new outdoor activities were ingested
        adaptation_message = None
        if new_activities_count > 0 and latest_act:
            try:
                import asyncio
                from app.gemini_coach import adapt_calendar_after_activity
                matched_ev = db.query(CalendarEvent).filter(CalendarEvent.activity_id == latest_act.id).first()
                if matched_ev:
                    res = asyncio.run(adapt_calendar_after_activity(db, matched_ev, latest_act, athlete_id=profile.id))
                    adaptation_message = res.get("debrief")
            except Exception as e:
                logger.error(f"[Garmin] Adaptation error: {e}")

        msg = f"Synced {new_activities_count} outdoor ride(s) and {wellness_count} recovery day(s) from Garmin Connect."
        logger.info(f"[Garmin] {msg}")

        return {
            "status": "success",
            "message": msg,
            "new_activities": new_activities_count,
            "wellness_days": wellness_count,
            "last_sync": now_str,
            "adaptation": adaptation_message
        }

garmin_service = GarminService()
