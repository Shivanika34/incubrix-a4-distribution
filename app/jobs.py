"""Job creation, idempotency, lookup, and the small echo worker."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import logging
import threading
import time
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.callbacks import CallbackAdapter
from app.database import IdempotencyKey, Job
from app.errors import AppError
from app.logging_config import job_id_var

logger = logging.getLogger(__name__)


class JobService:
    def __init__(self, session_factory, callback: CallbackAdapter, echo_sleep_seconds: float):
        self.session_factory = session_factory
        self.callback = callback
        self.echo_sleep_seconds = echo_sleep_seconds
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="incubrix-job")
        self._count_lock = threading.Lock()
        self.work_count = 0

    @staticmethod
    def body_hash(body: dict) -> str:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def create_or_get(self, key: str, body: dict, callback_url: str | None):
        digest = self.body_hash(body)
        job_id = str(uuid.uuid4())
        try:
            with self.session_factory.begin() as session:
                session.add(Job(id=job_id, job_type="echo", status="queued", progress=0, result_links=[]))
                session.add(IdempotencyKey(key=key, request_hash=digest, job_id=job_id))
                session.flush()
        except IntegrityError:
            with self.session_factory() as session:
                record = session.scalar(select(IdempotencyKey).where(IdempotencyKey.key == key))
                if record is None:
                    raise
                if record.request_hash != digest:
                    raise AppError("IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used with a different request body.", False)
                return session.get(Job, record.job_id), False
        job = self.get(job_id)
        self.executor.submit(self._run_echo, job_id, body, callback_url)
        return job, True

    def get(self, job_id: str):
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise AppError("JOB_NOT_FOUND", "Job was not found.", False)
            session.expunge(job)
            return job

    def _run_echo(self, job_id: str, body: dict, callback_url: str | None) -> None:
        token = job_id_var.set(job_id)
        try:
            with self._count_lock:
                self.work_count += 1
            self._update(job_id, status="running", progress=10)
            time.sleep(self.echo_sleep_seconds)
            result_links = [{"rel": "result", "href": f"urn:incubrix:echo:{job_id}"}]
            self._update(job_id, status="succeeded", progress=100, result_links=result_links)
            logger.info("job completed")
            if callback_url:
                payload = self._result(self.get(job_id))
                try:
                    self.callback.post(callback_url, payload)
                except Exception:
                    logger.error("job callback delivery failed")
        except Exception:
            logger.exception("job execution failed")
            self._update(job_id, status="failed", progress=100)
        finally:
            job_id_var.reset(token)

    def _update(self, job_id: str, **values) -> None:
        with self.session_factory.begin() as session:
            job = session.get(Job, job_id)
            for name, value in values.items():
                setattr(job, name, value)
            job.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _result(job: Job) -> dict:
        return {
            "job_id": job.id,
            "status": job.status,
            "progress": job.progress,
            "result_links": job.result_links,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)
