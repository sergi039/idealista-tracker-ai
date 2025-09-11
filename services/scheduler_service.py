import os
import logging
import fcntl
import tempfile
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit

logger = logging.getLogger(__name__)

scheduler = None
scheduler_lock_file = None

def init_scheduler(app):
    """Initialize the background scheduler with protection against duplicate instances"""
    global scheduler, scheduler_lock_file
    
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
        
        # Schedule ingestion for 7:00 AM and 7:00 PM CET
        scheduler.add_job(
            func=run_scheduled_ingestion,
            trigger=CronTrigger(hour=7, minute=0, timezone='Europe/Madrid'),
            id='morning_ingestion',
            name='Morning IMAP Ingestion',
            replace_existing=True
        )
        
        scheduler.add_job(
            func=run_scheduled_ingestion,
            trigger=CronTrigger(hour=19, minute=0, timezone='Europe/Madrid'),
            id='evening_ingestion',
            name='Evening IMAP Ingestion',
            replace_existing=True
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
                except:
                    pass
        
        atexit.register(cleanup)
        
        logger.info("Scheduler initialized with ingestion jobs at 07:00 and 19:00 CET")
        return scheduler
        
    except Exception as e:
        logger.error(f"Failed to initialize scheduler: {str(e)}")
        return None

def run_scheduled_ingestion():
    """Run the scheduled ingestion job"""
    try:
        from config import Config
        
        if Config.EMAIL_BACKEND == "imap":
            logger.info("Starting scheduled IMAP ingestion")
            from services.imap_service import IMAPService
            service = IMAPService()
            processed_count = service.run_ingestion()
        else:
            logger.info("Starting scheduled Gmail API ingestion")
            from services.gmail_service import GmailService
            service = GmailService()
            # Gmail service doesn't have run_full_sync, use run_ingestion
            processed_count = service.run_ingestion()
        
        logger.info(f"Scheduled ingestion completed. Processed {processed_count} properties")
        
    except Exception as e:
        logger.error(f"Scheduled ingestion failed: {str(e)}")

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
