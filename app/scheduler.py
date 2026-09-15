import datetime
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.database import SessionLocal, AthleteProfile

logger = logging.getLogger("whooshcoach.scheduler")

scheduler = AsyncIOScheduler()

async def sync_mywhoosh_and_adapt():
    """Background task to fetch MyWhoosh cloud activities, match workouts, and trigger AI adaptation."""
    from app.mywhoosh_service import mywhoosh_service
    db = SessionLocal()
    try:
        profile = db.query(AthleteProfile).filter(AthleteProfile.id == 1).first()
        if not profile or not profile.mywhoosh_email or not profile.mywhoosh_password:
            return {"status": "skipped", "message": "MyWhoosh credentials not configured"}

        logger.info("Starting background periodic MyWhoosh sync...")
        res = await mywhoosh_service.sync_activities(db, profile)
        logger.info(f"Background MyWhoosh sync result: {res}")
        return res
    except Exception as e:
        logger.error(f"Error during MyWhoosh background sync: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        db.close()

def start_scheduler():
    """Starts the periodic background scheduler."""
    if not scheduler.running:
        # Schedule MyWhoosh sync every 30 minutes
        scheduler.add_job(
            sync_mywhoosh_and_adapt,
            "interval",
            minutes=30,
            id="mywhoosh_sync_job",
            replace_existing=True,
            next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=10)
        )
        scheduler.start()
        logger.info("WhooshCoach background scheduler started (MyWhoosh sync interval: 30 minutes).")

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("WhooshCoach background scheduler stopped.")
