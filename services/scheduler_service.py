import os
import logging
import fcntl
import tempfile
from contextlib import contextmanager
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit
from config import Config

logger = logging.getLogger(__name__)

scheduler = None
scheduler_lock_file = None
# Flask app the jobs run against. APScheduler executes them on its own worker
# threads, which carry no app context of their own (#14).
flask_app = None


@contextmanager
def job_app_context():
    """Push the Flask app context a scheduled job needs to touch the database.

    Without it every `db.session` call raises "Working outside of application
    context", the job body's own `except Exception` swallows it, and APScheduler
    still reports "executed successfully" - which is how ingestion stayed dead
    and silent from February to August 2026 (#14).

    Missing app is a hard error, not a skip: a scheduler running jobs that can
    never reach the database is worse than one that refuses to start.
    """
    if flask_app is None:
        raise RuntimeError(
            "Scheduled job started without a Flask app. init_scheduler(app) must "
            "run before any job fires."
        )
    with flask_app.app_context():
        yield


def init_scheduler(app):
    """Initialize the background scheduler with protection against duplicate instances"""
    global scheduler, scheduler_lock_file, flask_app

    if app.config.get("TESTING"):
        logger.info("Scheduler disabled in TESTING")
        return None

    if not app.config.get("AUTO_START_SCHEDULER", False):
        logger.info("Scheduler disabled by config")
        return None

    if scheduler is not None:
        return scheduler

    # Try to acquire an exclusive lock to prevent duplicate schedulers
    lock_path = os.path.join(
        tempfile.gettempdir(), "idealista_universal_scheduler.lock"
    )
    try:
        scheduler_lock_file = open(lock_path, "w")
        fcntl.flock(scheduler_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        scheduler_lock_file.write(str(os.getpid()))
        scheduler_lock_file.flush()
        logger.info("Acquired scheduler lock (PID: %s)", os.getpid())
    except IOError:
        # Lock held by another instance — close file handle before returning
        if scheduler_lock_file:
            scheduler_lock_file.close()
            scheduler_lock_file = None
        logger.info(
            "Another scheduler instance is already running, skipping initialization"
        )
        return None
    except Exception:
        # Unexpected error during lock — close handle to prevent leak
        if scheduler_lock_file:
            scheduler_lock_file.close()
            scheduler_lock_file = None
        raise

    # Register cleanup IMMEDIATELY after lock acquisition so the handle is
    # always released on process exit, even if scheduler init below fails.
    def cleanup():
        global scheduler, scheduler_lock_file, flask_app
        flask_app = None
        if scheduler:
            try:
                scheduler.shutdown()
            except Exception:
                pass
            scheduler = None
        if scheduler_lock_file:
            try:
                fcntl.flock(scheduler_lock_file.fileno(), fcntl.LOCK_UN)
                scheduler_lock_file.close()
                os.remove(lock_path)
            except Exception:
                pass
            scheduler_lock_file = None

    atexit.register(cleanup)

    try:
        scheduler = BackgroundScheduler()

        # Bind before the first job can fire, so job_app_context() always has
        # an app to push.
        flask_app = app

        timezone = getattr(Config, "SCHEDULER_TIMEZONE", "Europe/Madrid")

        # Schedule ingestion jobs from config.
        ingestion_times = list(getattr(Config, "INGESTION_TIMES", ["07:00", "19:00"]))
        if len(ingestion_times) == 2:
            ingestion_job_ids = ["morning_ingestion", "evening_ingestion"]
        else:
            ingestion_job_ids = [f"ingestion_{i}" for i in range(len(ingestion_times))]

        for idx, time_str in enumerate(ingestion_times):
            try:
                hour_str, minute_str = time_str.split(":", 1)
                hour = int(hour_str)
                minute = int(minute_str)
            except Exception:
                logger.warning("Invalid ingestion time '%s', skipping", time_str)
                continue

            scheduler.add_job(
                func=run_scheduled_ingestion,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
                id=ingestion_job_ids[idx],
                name=f"IMAP Ingestion {time_str}",
                replace_existing=True,
            )

        # Schedule listing status check for favorites.
        listing_time = getattr(Config, "LISTING_STATUS_CHECK_TIME", "10:00")
        try:
            hour_str, minute_str = listing_time.split(":", 1)
            hour = int(hour_str)
            minute = int(minute_str)
        except Exception:
            hour = 10
            minute = 0

        scheduler.add_job(
            func=run_listing_status_check,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
            id="listing_status_check",
            name="Daily Listing Status Check",
            replace_existing=True,
        )

        scheduler.start()

        logger.info(
            "Scheduler initialized. Ingestion times=%s, listing_status_time=%s, timezone=%s",
            ingestion_times,
            listing_time,
            timezone,
        )
        return scheduler

    except Exception:
        logger.error("Failed to initialize scheduler", exc_info=True)
        # Release lock so other instances can try
        cleanup()
        raise


def run_scheduled_ingestion():
    """Run the scheduled ingestion job.

    The whole body runs inside the app context, not just the service call:
    constructing a service and reading its result can both reach the database.

    Failures are logged *and* re-raised. Swallowing them is what let #14 hide -
    APScheduler reports "executed successfully" for a job that returns normally,
    so a caught exception means a dead job that looks alive.
    """
    with job_app_context():
        try:
            from config import Config

            target = getattr(Config, "INGESTION_TARGET", "properties")

            logger.info("Starting scheduled IMAP ingestion (target=%s)", target)

            if target == "lands":
                from services.imap_service import IMAPService

                service = IMAPService()
            else:
                from services.property_imap_service import PropertyIMAPService

                service = PropertyIMAPService()

            processed_count = service.run_ingestion()

            logger.info(
                "Scheduled ingestion completed. Processed %s properties",
                processed_count,
            )

        except Exception:
            logger.error("Scheduled ingestion failed", exc_info=True)
            raise


def run_listing_status_check():
    """Run the scheduled listing status check job.

    Same contract as run_scheduled_ingestion: one context around the whole body
    (reading the result rows is database work too), and failures propagate so
    APScheduler cannot log a dead job as a successful one.
    """
    with job_app_context():
        try:
            from config import Config

            target = getattr(Config, "INGESTION_TARGET", "properties")
            if target != "lands":
                # Not "email-driven" -- nothing checks Property statuses on a
                # schedule at all. ListingStatusService.check_all_active_properties
                # exists and works, but idealista currently answers 403 + captcha
                # to the scraper, so an unattended sweep would only burn requests
                # against a blocked host. The per-listing button on
                # /properties/<id> is the way in until that changes (issue #136).
                logger.info(
                    "Skipping scheduled listing status check: target=%s has no "
                    "scheduled sweep, use the per-listing check on /properties/<id>",
                    target,
                )
                return

            logger.info("Starting scheduled listing status check (lands)")
            from services.listing_status_service import ListingStatusService

            service = ListingStatusService()

            # Check favorites first (they get priority)
            results = service.check_favorites_status(limit=30)

            logger.info(
                "Listing status check completed. Checked %s favorites: %s removed, %s sold",
                results["checked"],
                results["removed"],
                results["sold"],
            )

            # If any listings were removed, log details
            if results.get("details"):
                for detail in results["details"]:
                    logger.info(
                        "Status change: Land %s (%s) - %s -> %s",
                        detail["land_id"],
                        detail["title"],
                        detail["old_status"],
                        detail["new_status"],
                    )

        except Exception:
            logger.error("Listing status check failed", exc_info=True)
            raise


def get_scheduler_status():
    """Get current scheduler status"""
    global scheduler

    if scheduler is None:
        return {"status": "not_initialized"}

    jobs = []
    for job in scheduler.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat()
                if job.next_run_time
                else None,
                "trigger": str(job.trigger),
            }
        )

    return {"status": "running" if scheduler.running else "stopped", "jobs": jobs}
