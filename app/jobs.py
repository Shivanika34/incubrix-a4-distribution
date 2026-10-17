"""Job creation, idempotency, lookup, and the small echo worker."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.callbacks import CallbackAdapter
from app.database import CertificationRun, Destination, IdempotencyKey, Job, RuleDecision
from app.errors import AppError
from app.logging_config import job_id_var
from app.rules import check_rules

logger = logging.getLogger(__name__)


class JobService:
    def __init__(self, session_factory, callback: CallbackAdapter, echo_sleep_seconds: float, connector_registry=None, disabled_connectors=frozenset()):
        self.session_factory = session_factory
        self.callback = callback
        self.echo_sleep_seconds = echo_sleep_seconds
        self.connector_registry = connector_registry
        self.disabled_connectors = disabled_connectors
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
                session.add(Job(id=job_id, job_type="echo", status="queued", progress=0, result_links=[], result={}))
                session.flush()
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

    def create_publishing(self, key: str, body: dict):
        digest = self.body_hash(body)
        job_id = str(uuid.uuid4())
        try:
            with self.session_factory.begin() as session:
                session.add(Job(id=job_id, job_type="publishing", status="queued", progress=0, result_links=[], result={}))
                session.flush()
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
        self.executor.submit(self._run_publishing, job_id, body, key)
        return job, True

    def create_certification(self, key: str, body: dict):
        digest = self.body_hash(body)
        job_id = str(uuid.uuid4())
        try:
            with self.session_factory.begin() as session:
                session.add(Job(id=job_id, job_type="certification", status="queued", progress=0, result_links=[], result={}))
                session.flush()
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
        self.executor.submit(self._run_certification, job_id, body, key)
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

    def _run_publishing(self, job_id: str, body: dict, idempotency_key: str) -> None:
        token = job_id_var.set(job_id)
        try:
            self._update(job_id, status="running", progress=5)
            outcomes = []
            destination_ids = body["destination_ids"]
            for index, destination_id in enumerate(destination_ids, start=1):
                outcomes.append(self._publish_destination(body, destination_id, idempotency_key))
                self._update(job_id, progress=int(index * 100 / len(destination_ids)))

            successful = sum(item["status"] in {"published", "assisted"} for item in outcomes)
            failed = len(outcomes) - successful
            status = "succeeded" if failed == 0 else "partial" if successful else "failed"
            result = {"destinations": outcomes}
            result_links = [
                {"rel": "destination", "href": item["live_url"], "destination_id": item["destination_id"]}
                for item in outcomes if item.get("live_url")
            ]
            self._update(job_id, status=status, progress=100, result=result, result_links=result_links)
            logger.info("publishing job completed", extra={"destination_count": len(outcomes), "job_status": status})
        except Exception:
            logger.exception("publishing job execution failed")
            self._update(job_id, status="failed", progress=100, result={"destinations": []})
        finally:
            job_id_var.reset(token)

    def _run_certification(self, job_id: str, body: dict, idempotency_key: str) -> None:
        token = job_id_var.set(job_id)
        run_id = str(uuid.uuid4())
        destination_id = body["destination_id"]
        started_at = datetime.now(timezone.utc)
        try:
            self._update(job_id, status="running", progress=5)
            with self.session_factory.begin() as session:
                session.add(CertificationRun(id=run_id, destination_id=destination_id, status="running", started_at=started_at))

            outcome = self._certify_destination(body, destination_id, idempotency_key, run_id)
            success = outcome["status"] in {"certified", "assisted"}
            if success:
                with self.session_factory.begin() as session:
                    destination = session.get(Destination, destination_id)
                    if destination is not None:
                        destination.last_certified_at = datetime.now(timezone.utc)
                        if outcome["status"] == "certified" and destination.level in {"L0", "L2"}:
                            destination.level = "L3"
                        elif outcome["status"] == "assisted" and destination.level == "L0":
                            destination.level = "L1"
            self._update(
                job_id,
                status="succeeded" if success else "failed",
                progress=100,
                result={"certification": outcome},
                result_links=[{"rel": "live", "href": outcome["live_url"]}] if outcome.get("live_url") else [],
            )
        except Exception:
            logger.exception("certification job execution failed")
            self._update(
                job_id,
                status="failed",
                progress=100,
                result={"certification": {
                    "certification_run_id": run_id,
                    "destination_id": destination_id,
                    "status": "failed",
                    "error_code": "INTERNAL_ERROR",
                    "reason": "Certification failed.",
                }},
            )
        finally:
            job_id_var.reset(token)

    def _certify_destination(self, body: dict, destination_id: str, idempotency_key: str, run_id: str) -> dict:
        started_at = datetime.now(timezone.utc).isoformat()
        outcome = {
            "certification_run_id": run_id,
            "destination_id": destination_id,
            "started_at": started_at,
            "status": "failed",
            "connector": None,
            "platform_post_id": None,
            "live_url": None,
            "status_result": {},
            "delete_result": {},
            "error_code": None,
            "reason": None,
        }
        with self.session_factory() as session:
            destination = session.get(Destination, destination_id)
            if destination is None:
                return self._finish_certification(run_id, {**outcome, "error_code": "DESTINATION_NOT_FOUND", "reason": "Destination was not found."})
            destination_data = {column.name: getattr(destination, column.name) for column in Destination.__table__.columns}

        connector_key = destination_data["connector_key"]
        outcome["connector"] = connector_key
        if connector_key in self.disabled_connectors:
            return self._finish_certification(run_id, {**outcome, "error_code": "CONNECTOR_DISABLED", "reason": "Connector is disabled by configuration."})
        connector = self.connector_registry.get(connector_key) if self.connector_registry else None
        if connector is None:
            return self._finish_certification(run_id, {**outcome, "error_code": "CONNECTOR_NOT_FOUND", "reason": f"Connector '{connector_key}' is not registered."})

        rendition = body.get("content_item") or {
            "title": f"IncuBrix certification test {run_id[:8]}",
            "body": "Temporary certification post. This post is created and deleted by the local certification harness.",
        }
        connection = {
            **body.get("connection", {}),
            "base_url": body.get("connection", {}).get("base_url") or destination_data["url"],
        }
        decision = check_rules(rendition, destination_data, [])
        with self.session_factory.begin() as session:
            session.add(RuleDecision(destination_id=destination_id, outcome=decision.outcome, reason=decision.reason, error_code=decision.error_code))
        assisted_only = connector_key == "assisted" and decision.error_code == "DEST_SPEC_VIOLATION" and decision.reason == "This destination requires assisted posting."
        if decision.outcome != "allowed" and not assisted_only:
            return self._finish_certification(run_id, {**outcome, "error_code": decision.error_code, "reason": decision.reason})

        effective_rendition = decision.rendition or rendition
        try:
            validation = connector.validate(effective_rendition, destination_data, connection)
            if not validation["ok"]:
                return self._finish_certification(run_id, {**outcome, "error_code": "DEST_SPEC_VIOLATION", "reason": "The certification rendition violates the destination specification."})
            if connector_key == "assisted":
                pack = connector.build_assist_pack(effective_rendition, destination_data)
                return self._finish_certification(run_id, {**outcome, "status": "assisted", "reason": "Posting pack generated; manual publication is required.", "posting_pack": pack})

            published = connector.publish(effective_rendition, destination_data, connection, f"{idempotency_key}:{destination_id}")
            outcome.update(platform_post_id=published.get("platform_post_id"), live_url=published.get("live_url"))
            status_result = connector.get_status(outcome["platform_post_id"], connection)
            outcome["status_result"] = status_result
            if status_result.get("status") not in {"live", "published"}:
                return self._finish_certification(run_id, {**outcome, "error_code": "CERTIFICATION_FAILED", "reason": "The test post did not report a live status."})
            delete_result = connector.delete(outcome["platform_post_id"], connection)
            outcome["delete_result"] = delete_result
            if not delete_result.get("deleted"):
                return self._finish_certification(run_id, {**outcome, "error_code": "CERTIFICATION_FAILED", "reason": "The test post could not be deleted."})
            return self._finish_certification(run_id, {**outcome, "status": "certified", "reason": "Test post published, confirmed live, and deleted."})
        except AppError as error:
            return self._finish_certification(run_id, {**outcome, "error_code": error.code, "reason": error.message})
        except Exception:
            logger.exception("destination certification failed", extra={"destination_id": destination_id})
            return self._finish_certification(run_id, {**outcome, "error_code": "CERTIFICATION_FAILED", "reason": "The certification connector flow failed."})

    def _finish_certification(self, run_id: str, outcome: dict) -> dict:
        completed_at = datetime.now(timezone.utc)
        outcome = {**outcome, "completed_at": completed_at.isoformat()}
        with self.session_factory.begin() as session:
            record = session.get(CertificationRun, run_id)
            record.status = outcome["status"]
            record.platform_post_id = outcome.get("platform_post_id")
            record.live_url = outcome.get("live_url")
            record.status_result = outcome.get("status_result") or {}
            record.delete_result = outcome.get("delete_result") or {}
            record.error_code = outcome.get("error_code")
            record.reason = outcome.get("reason")
            record.completed_at = completed_at
        self._write_certification_artifact(outcome)
        return outcome

    @staticmethod
    def _write_certification_artifact(outcome: dict) -> None:
        """Write review evidence without copying connection credentials."""
        connector = outcome.get("connector") or "unknown"
        artifact = {
            key: outcome.get(key)
            for key in (
                "certification_run_id",
                "destination_id",
                "connector",
                "started_at",
                "completed_at",
                "status",
                "platform_post_id",
                "live_url",
                "status_result",
                "delete_result",
                "error_code",
                "reason",
                "posting_pack",
            )
            if key in outcome
        }
        directory = Path("artifacts") / "certification" / str(connector)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{outcome['certification_run_id']}.json"
        path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _publish_destination(self, body: dict, destination_id: str, idempotency_key: str) -> dict:
        base = {
            "destination_id": destination_id,
            "status": "failed",
            "error_code": None,
            "reason": None,
            "platform_post_id": None,
            "live_url": None,
        }
        with self.session_factory() as session:
            destination = session.get(Destination, destination_id)
            if destination is None:
                return {**base, "error_code": "DESTINATION_NOT_FOUND", "reason": "Destination was not found."}
            destination_data = {column.name: getattr(destination, column.name) for column in Destination.__table__.columns}

        connector_key = destination_data["connector_key"]
        connector = self.connector_registry.get(connector_key) if self.connector_registry else None
        if connector is None:
            return {**base, "error_code": "CONNECTOR_NOT_FOUND", "reason": f"Connector '{connector_key}' is not registered."}

        rendition = body["content_item"]
        connection = {
            **body.get("connections", {}).get(destination_id, {}),
            "base_url": body.get("connections", {}).get(destination_id, {}).get("base_url") or destination_data["url"],
        }
        decision = check_rules(rendition, destination_data, [])
        with self.session_factory.begin() as session:
            session.add(RuleDecision(
                destination_id=destination_id,
                outcome=decision.outcome,
                reason=decision.reason,
                error_code=decision.error_code,
            ))
        assisted_only = (
            connector_key == "assisted"
            and decision.error_code == "DEST_SPEC_VIOLATION"
            and decision.reason == "This destination requires assisted posting."
        )
        if decision.outcome != "allowed" and not assisted_only:
            return {**base, "error_code": decision.error_code, "reason": decision.reason}

        if connector_key in self.disabled_connectors:
            return {**base, "error_code": "CONNECTOR_DISABLED", "reason": "Connector is disabled by configuration."}

        try:
            effective_rendition = decision.rendition or rendition
            validation = connector.validate(effective_rendition, destination_data, connection)
            if not validation["ok"]:
                return {
                    **base,
                    "error_code": "DEST_SPEC_VIOLATION",
                    "reason": "The content violates the destination specification.",
                    "details": validation["violations"],
                }
            if connector_key == "assisted":
                pack = connector.build_assist_pack(effective_rendition, destination_data)
                return {**base, "status": "assisted", "reason": "Posting pack generated for manual publication.", "posting_pack": pack}
            published = connector.publish(
                effective_rendition,
                destination_data,
                connection,
                f"{idempotency_key}:{destination_id}",
            )
            return {
                **base,
                "status": "published",
                "reason": "Published successfully.",
                "platform_post_id": published.get("platform_post_id"),
                "live_url": published.get("live_url"),
            }
        except AppError as error:
            logger.warning("destination publishing failed", extra={"destination_id": destination_id, "error_code": error.code})
            return {**base, "error_code": error.code, "reason": error.message}
        except Exception:
            logger.exception("destination publishing failed", extra={"destination_id": destination_id})
            return {**base, "error_code": "INTERNAL_ERROR", "reason": "Destination publishing failed."}

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
            "result": job.result,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)
