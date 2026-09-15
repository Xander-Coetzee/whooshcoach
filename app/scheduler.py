import datetime
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.database import SessionLocal, AthleteProfile

logger = logging.getLogger("whooshcoach.scheduler")

scheduler = AsyncIOScheduler()

async def sync_all_athletes():
    """Background task to fetch cloud activities for all athletes (MyWhoosh and/or Zwift), match workouts, and adapt."""
    from app.mywhoosh_service import mywhoosh_service
    from app.zwift_service import zwift_service

    db = SessionLocal()
    try:
        athletes = db.query(AthleteProfile).all()
        logger.info(f"Starting background sync for {len(athletes)} athlete(s)...")

        for profile in athletes:
            platform = getattr(profile, "platform", "mywhoosh") or "mywhoosh"
            
            # Sync MyWhoosh if applicable
            if platform in ["mywhoosh", "both"] and profile.mywhoosh_email and profile.mywhoosh_password:
                try:
                    logger.info(f"[Background] Syncing MyWhoosh for athlete '{profile.name}' (ID: {profile.id})...")
                    res = await mywhoosh_service.sync_activities(db, profile)
                    logger.info(f"[Background] Athlete '{profile.name}' MyWhoosh sync result: {res.get('status')}")
                except Exception as e:
                    logger.error(f"[Background] MyWhoosh sync error for '{profile.name}': {e}", exc_info=True)

            # Sync Zwift if applicable
            if platform in ["zwift", "both"] and profile.zwift_username and profile.zwift_password:
                try:
                    logger.info(f"[Background] Syncing Zwift for athlete '{profile.name}' (ID: {profile.id})...")
                    res = await zwift_service.sync_activities(db, profile)
                    logger.info(f"[Background] Athlete '{profile.name}' Zwift sync result: {res.get('status')}")
                except Exception as e:
                    logger.error(f"[Background] Zwift sync error for '{profile.name}': {e}", exc_info=True)

    except Exception as e:
        logger.error(f"Error during background multi-athlete sync: {e}", exc_info=True)
    finally:
        db.close()

# For backwards compatibility with existing references
sync_mywhoosh_and_adapt = sync_all_athletes

def start_scheduler():
    """Starts the periodic background scheduler."""
    if not scheduler.running:
        scheduler.add_job(
            sync_all_athletes,
            "interval",
            minutes=30,
            id="multi_athlete_sync_job",
            replace_existing=True,
            next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=10)
        )
        scheduler.start()
        logger.info("WhooshCoach background scheduler started (Multi-athlete sync interval: 30 minutes).")

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("WhooshCoach background scheduler stopped.")
