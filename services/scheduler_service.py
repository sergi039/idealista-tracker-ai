import os
import logging
import fcntl
import tempfile
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit
from config import Config

logger = logging.getLogger(__name__)

scheduler = None
scheduler_lock_file = None

def init_scheduler(app):
    """Initialize the background scheduler with protection against duplicate instances"""
    global scheduler, scheduler_lock_file
    
    if app.config.get('TESTING'):
        logger.info("Scheduler disabled in TESTING")
        return None

    if not getattr(Config, 'AUTO_START_SCHEDULER', False):
        logger.info("Scheduler disabled by config")
        return None

    if scheduler is not None:
        return scheduler
    
    # Try to acquire an exclusive lock to prevent duplicate schedulers
    lock_path = os.path.join(tempfile.gettempdir(), 'idealista_scheduler.lock')
    try:
        scheduler_lock_file = open(lock_path, 'w')
        fcntl.flock(scheduler_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        scheduler_lock_file.write(str(os.getpid()))
        scheduler_lock_file.flush()
        logger.info(f"Acquired scheduler lock (PID: {os.getpid()})")
    except IOError:
        logger.info("Another scheduler instance is already running, skipping initialization")
        return None
    
    try:
        scheduler = BackgroundScheduler()

        timezone = getattr(Config, 'SCHEDULER_TIMEZONE', 'Europe/Madrid')

        # Schedule ingestion jobs from config.
        ingestion_times = list(getattr(Config, 'INGESTION_TIMES', ['07:00', '19:00']))
        if len(ingestion_times) == 2:
            ingestion_job_ids = ['morning_ingestion', 'evening_ingestion']
        else:
            ingestion_job_ids = [f'ingestion_{i}' for i in range(len(ingestion_times))]

        for idx, time_str in enumerate(ingestion_times):
            try:
                hour_str, minute_str = time_str.split(':', 1)
                hour = int(hour_str)
                minute = int(minute_str)
            except Exception:
                logger.warning(f"Invalid ingestion time '{time_str}', skipping")
                continue

            scheduler.add_job(
                func=run_scheduled_ingestion,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
                id=ingestion_job_ids[idx],
                name=f"IMAP Ingestion {time_str}",
                replace_existing=True,
            )

        # Schedule listing status check for favorites.
        listing_time = getattr(Config, 'LISTING_STATUS_CHECK_TIME', '10:00')
        try:
            hour_str, minute_str = listing_time.split(':', 1)
            hour = int(hour_str)
            minute = int(minute_str)
        except Exception:
            hour = 10
            minute = 0

        scheduler.add_job(
            func=run_listing_status_check,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
            id='listing_status_check',
            name='Daily Listing Status Check',
            replace_existing=True,
        )

        scheduler.start()
        
        # Shut down the scheduler and release lock when exiting the app
        def cleanup():
            global scheduler_lock_file
            if scheduler:
                scheduler.shutdown()
            if scheduler_lock_file:
                try:
                    fcntl.flock(scheduler_lock_file.fileno(), fcntl.LOCK_UN)
                    scheduler_lock_file.close()
                    os.remove(scheduler_lock_file.name)
                except Exception:
                    pass
        
        atexit.register(cleanup)

        logger.info(
            "Scheduler initialized. Ingestion times=%s, listing_status_time=%s, timezone=%s",
            ingestion_times,
            listing_time,
            timezone,
        )
        return scheduler
        
    except Exception as e:
        logger.error(f"Failed to initialize scheduler: {str(e)}")
        return None

def run_scheduled_ingestion():
    """Run the scheduled ingestion job"""
    try:
        from config import Config
        
        # Use IMAP service for email ingestion
        logger.info("Starting scheduled IMAP ingestion")
        from services.imap_service import IMAPService
        service = IMAPService()
        processed_count = service.run_ingestion()
        
        logger.info(f"Scheduled ingestion completed. Processed {processed_count} properties")
        
    except Exception as e:
        logger.error(f"Scheduled ingestion failed: {str(e)}")

def run_listing_status_check():
    """Run the scheduled listing status check job"""
    try:
        logger.info("Starting scheduled listing status check")
        from services.listing_status_service import ListingStatusService
        service = ListingStatusService()

        # Check favorites first (they get priority)
        results = service.check_favorites_status(limit=30)

        logger.info(f"Listing status check completed. Checked {results['checked']} favorites: "
                   f"{results['removed']} removed, {results['sold']} sold")

        # If any listings were removed, log details
        if results.get('details'):
            for detail in results['details']:
                logger.info(f"Status change: Land {detail['land_id']} ({detail['title']}) - "
                           f"{detail['old_status']} -> {detail['new_status']}")

    except Exception as e:
        logger.error(f"Listing status check failed: {str(e)}")


def get_scheduler_status():
    """Get current scheduler status"""
    global scheduler
    
    if scheduler is None:
        return {"status": "not_initialized"}
    
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "trigger": str(job.trigger)
        })
    
    return {
        "status": "running" if scheduler.running else "stopped",
        "jobs": jobs
    }
