import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from flask import current_app

_MAX_WORKERS = int(os.environ.get("BACKGROUND_WORKERS", "4"))
_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, _MAX_WORKERS))
_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue_job(
    fn: Callable[[], Any],
    *,
    job_type: str,
    meta: Optional[Dict[str, Any]] = None,
    app=None,
) -> str:
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "job_type": job_type,
        "status": "queued",
        "created_at": _now_iso(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "meta": meta or {},
    }

    with _LOCK:
        _JOBS[job_id] = job

    app_obj = app or current_app._get_current_object()

    def _run():
        with app_obj.app_context():
            with _LOCK:
                job["status"] = "running"
                job["started_at"] = _now_iso()
            try:
                result = fn()
                with _LOCK:
                    job["status"] = "success"
                    job["result"] = result
            except Exception as exc:
                with _LOCK:
                    job["status"] = "error"
                    job["error"] = str(exc)
            finally:
                with _LOCK:
                    job["finished_at"] = _now_iso()

    _EXECUTOR.submit(_run)
    return job_id


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        return _JOBS.get(job_id)


def serialize_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": job.get("id"),
        "job_type": job.get("job_type"),
        "status": job.get("status"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "result": job.get("result"),
        "error": job.get("error"),
        "meta": job.get("meta") or {},
    }
