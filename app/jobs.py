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
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.callbacks import CallbackAdapter
from app.database import CertificationRun, Destination, IdempotencyKey, Job, RuleDecision
from app.errors import AppError
from app.logging_config import job_id_var
from app.rules import check_rules

logger = logging.getLogger(__name__)


class JobService:
    def __init__(self, session_factory, callback: CallbackAdapter, echo_sleep_seconds: float, connector_registry=None, disabled_connectors=frozenset(), *, simulation_mode: bool = True, evidence_dir: str = "artifacts/certification", catalogue_import_max_rows: int = 5000, catalogue_reachability_enabled: bool = False, artifact_store=None):
        self.session_factory = session_factory
        self.callback = callback
        self.echo_sleep_seconds = echo_sleep_seconds
        self.connector_registry = connector_registry
        self.disabled_connectors = disabled_connectors
        self.simulation_mode = simulation_mode
        self.evidence_dir = Path(evidence_dir)
        self.catalogue_import_max_rows = catalogue_import_max_rows
        self.catalogue_reachability_enabled = catalogue_reachability_enabled
        self.artifact_store = artifact_store
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

    def create_catalogue_import(self, key: str, content: str, *, check_reachability: bool = False):
        body = {"content": content, "check_reachability": check_reachability}
        digest = self.body_hash(body)
        job_id = str(uuid.uuid4())
        try:
            with self.session_factory.begin() as session:
                session.add(Job(id=job_id, job_type="catalogue_import", status="queued", progress=0, result_links=[], result={}))
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
        self.executor.submit(self._run_catalogue_import, job_id, content, check_reachability)
        return job, True

    def _run_catalogue_import(self, job_id: str, content: str, check_reachability: bool) -> None:
        token = job_id_var.set(job_id)
        try:
            self._update(job_id, status="running", progress=1)
            from app.catalogue import CatalogueImporter, HttpReachabilityChecker
            checker = HttpReachabilityChecker() if check_reachability and self.catalogue_reachability_enabled else None
            connector_keys = set(self.connector_registry.manifests) if self.connector_registry else None
            connector_surfaces = {
                key: {surface["key"] for surface in manifest.get("surfaces", [])}
                for key, manifest in self.connector_registry.manifests.items()
            } if self.connector_registry else None
            importer = CatalogueImporter(self.session_factory, max_rows=self.catalogue_import_max_rows, reachability_checker=checker, connector_keys=connector_keys, connector_surfaces=connector_surfaces)
            report = importer.import_csv(content, progress=lambda value: self._update(job_id, progress=value))
            rejected, accepted = report["totals"]["rejected"], report["totals"]["accepted"]
            status = "succeeded" if not rejected else "partial" if accepted else "failed"
            self._update(job_id, status=status, progress=100, result={"catalogue_import": report})
        except Exception:
            logger.exception("catalogue import failed")
            self._update(job_id, status="failed", progress=100, result={"catalogue_import": {"error_code": "CATALOGUE_IMPORT_FAILED", "message": "Catalogue import could not be completed."}})
        finally:
            job_id_var.reset(token)

    def create_tailoring_preview(self, key: str, body: dict):
        digest = self.body_hash(body)
        job_id = str(uuid.uuid4())
        try:
            with self.session_factory.begin() as session:
                session.add(Job(id=job_id, job_type="tailoring_preview", status="queued", progress=0, result_links=[], result={}))
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
        self.executor.submit(self._run_tailoring_preview, job_id, body)
        return job, True

    def _run_tailoring_preview(self, job_id: str, body: dict) -> None:
        token = job_id_var.set(job_id)
        try:
            self._update(job_id, status="running", progress=10)
            from app.tailoring import TailoringService
            tailor = TailoringService(self.session_factory, self.connector_registry)
            report = tailor.preview(
                body["content_item"],
                body["destination_ids"],
                body.get("connections", {}),
                body.get("options", {}),
                progress=lambda value: self._update(job_id, progress=value),
            )
            status = "succeeded" if report.get("ready_for_publishing") else "partial"
            self._update(job_id, status=status, progress=100, result={"tailoring_preview": report})
        except Exception:
            logger.exception("tailoring preview failed")
            self._update(job_id, status="failed", progress=100, result={"tailoring_preview": {"error_code": "TAILORING_FAILED", "message": "Content tailoring preview failed."}})
        finally:
            job_id_var.reset(token)

    def create_orchestration(self, key: str, body: dict):
        digest = self.body_hash(body)
        job_id = str(uuid.uuid4())
        try:
            with self.session_factory.begin() as session:
                session.add(Job(id=job_id, job_type="orchestration", status="queued", progress=0, result_links=[], result={}))
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
        self.executor.submit(self._run_orchestration, job_id, body, key)
        return job, True

    def _run_orchestration(self, job_id: str, body: dict, idempotency_key: str) -> None:
        token = job_id_var.set(job_id)
        started = perf_counter()
        try:
            self._update(job_id, status="running", progress=5)
            from app.orchestrator import PublishingOrchestrator
            orchestrator = PublishingOrchestrator(
                self.session_factory,
                self.connector_registry,
                self.disabled_connectors,
                simulation_mode=self.simulation_mode,
            )
            report = orchestrator.run_workflow(
                body,
                idempotency_key,
                progress=lambda value: self._update(job_id, progress=value),
            )
            job_status = report.get("job_status", "succeeded")
            artifact_refs = {}
            if self.artifact_store is not None:
                try:
                    from app.artifacts import store_orchestration_artifacts
                    destination_ids = [
                        item.get("destination_id")
                        for item in report.get("destinations", [])
                        if item.get("status") in {"published", "outcome_unknown"}
                    ]
                    with self.session_factory() as session:
                        destinations = [
                            destination
                            for destination_id in destination_ids
                            if destination_id and (destination := session.get(Destination, destination_id)) is not None
                        ]
                    connector_names = [
                        {
                            "type": "publishing_connector",
                            "name": destination.connector_key,
                            "version": (self.connector_registry.manifests.get(destination.connector_key, {}).get("version") if self.connector_registry else None) or "unknown",
                        }
                        for destination in destinations
                    ]
                    artifact_refs = store_orchestration_artifacts(
                        self.artifact_store,
                        job_id=job_id,
                        request_body=body,
                        report=report,
                        settings={
                            "simulation_mode": self.simulation_mode,
                            "disabled_connectors": sorted(self.disabled_connectors),
                        },
                        connector_names=connector_names,
                        duration_ms=round((perf_counter() - started) * 1000),
                    )
                except AppError as error:
                    logger.warning("workflow artifact storage failed", extra={"error_code": error.code})
                    report["warnings"] = list(report.get("warnings", [])) + [error.message]
                    report["artifact_error"] = {"code": error.code, "message": error.message}
                except Exception:
                    logger.exception("workflow artifact manifest generation failed")
                    report["warnings"] = list(report.get("warnings", [])) + ["Workflow result artifact generation failed."]
                    report["artifact_error"] = {"code": "ARTIFACT_STORAGE_FAILED", "message": "Workflow result artifacts could not be generated."}
            result_links = [
                {"rel": "destination", "href": item["live_url"], "destination_id": item["destination_id"]}
                for item in report.get("destinations", [])
                if item.get("live_url")
            ]
            result_links.extend(
                {"rel": name, "href": value["download_url"], "artifact_uri": value["uri"]}
                for name, value in artifact_refs.items()
            )
            self._update(
                job_id,
                status=job_status,
                progress=100,
                result={"orchestration": report, "artifacts": artifact_refs},
                result_links=result_links,
            )
            logger.info("orchestration job completed", extra={"job_status": job_status, "totals": report.get("totals")})
        except AppError as error:
            logger.warning("orchestration job app error", extra={"error_code": error.code})
            report = {"job_status": "failed", "error_code": error.code, "message": error.message, "destinations": [], "totals": {}}
            artifacts = self._store_failure_artifacts(job_id, body, report, started)
            if self.artifact_store is not None and not artifacts:
                report["artifact_error"] = {"code": "ARTIFACT_STORAGE_FAILED", "message": "Workflow result artifacts could not be stored."}
            self._update(job_id, status="failed", progress=100, result={"orchestration": report, "artifacts": artifacts}, result_links=self._artifact_links(artifacts))
        except Exception:
            logger.exception("orchestration job execution failed")
            report = {"job_status": "failed", "error_code": "ORCHESTRATION_FAILED", "message": "Publishing orchestration failed.", "destinations": [], "totals": {}}
            artifacts = self._store_failure_artifacts(job_id, body, report, started)
            if self.artifact_store is not None and not artifacts:
                report["artifact_error"] = {"code": "ARTIFACT_STORAGE_FAILED", "message": "Workflow result artifacts could not be stored."}
            self._update(job_id, status="failed", progress=100, result={"orchestration": report, "artifacts": artifacts}, result_links=self._artifact_links(artifacts))
        finally:
            job_id_var.reset(token)

    def _store_failure_artifacts(self, job_id: str, body: dict, report: dict, started: float) -> dict:
        if self.artifact_store is None:
            return {}
        try:
            from app.artifacts import store_orchestration_artifacts
            return store_orchestration_artifacts(
                self.artifact_store,
                job_id=job_id,
                request_body=body,
                report=report,
                settings={"simulation_mode": self.simulation_mode, "disabled_connectors": sorted(self.disabled_connectors)},
                connector_names=[],
                duration_ms=round((perf_counter() - started) * 1000),
            )
        except Exception:
            logger.exception("failed workflow artifacts could not be stored")
            return {}

    @staticmethod
    def _artifact_links(artifacts: dict) -> list[dict]:
        return [
            {"rel": name, "href": value["download_url"], "artifact_uri": value["uri"]}
            for name, value in artifacts.items()
        ]

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
            unknown = sum(item["status"] == "outcome_unknown" for item in outcomes)
            failed = len(outcomes) - successful - unknown
            status = "succeeded" if failed == 0 and unknown == 0 else "partial" if successful or unknown else "failed"
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
            success = outcome["status"] in {"certified", "assisted", "cleanup_verified", "simulated"}
            if success:
                with self.session_factory.begin() as session:
                    destination = session.get(Destination, destination_id)
                    if destination is not None and outcome["status"] == "certified":
                        destination.last_certified_at = datetime.now(timezone.utc)
                        if destination.level in {"L0", "L2"}:
                            destination.level = "L3"
                    elif destination is not None and outcome["status"] == "assisted" and destination.level == "L0":
                        destination.last_certified_at = datetime.now(timezone.utc)
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
            "environment": self._environment_classification(body),
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
        if connector_key == "lemmy" and connection.get("local_test") is True:
            connection.update({
                "destination_id": destination_data["id"],
                "destination_url": destination_data["url"],
                "connector_key": connector_key,
                "surface": destination_data["surface"],
            })
        if connector_key == "lemmy" and "community_id" not in connection:
            connection["community_id"] = destination_data["category"]
        decision = check_rules(rendition, destination_data, [])
        with self.session_factory.begin() as session:
            session.add(RuleDecision(destination_id=destination_id, outcome=decision.outcome, reason=decision.reason, error_code=decision.error_code))
        assisted_only = connector_key == "assisted" and decision.error_code == "DEST_SPEC_VIOLATION" and decision.reason == "This destination requires assisted posting."
        if decision.outcome != "allowed" and not assisted_only:
            return self._finish_certification(run_id, {**outcome, "error_code": decision.error_code, "reason": decision.reason})
        if connector_key != "assisted":
            capabilities = set(getattr(connector, "manifest", {}).get("capabilities", []))
            if "publish" not in capabilities or "status" not in capabilities or not capabilities.intersection({"delete", "unpublish"}):
                return self._finish_certification(
                    run_id,
                    {
                        **outcome,
                        "error_code": "CERTIFICATION_FAILED",
                        "reason": "This connector does not expose verifiable status and cleanup capabilities.",
                    },
                )

        effective_rendition = decision.rendition or rendition
        try:
            validation = connector.validate(effective_rendition, destination_data, connection)
            if not validation["ok"]:
                return self._finish_certification(run_id, {**outcome, "error_code": "DEST_SPEC_VIOLATION", "reason": "The certification rendition violates the destination specification."})
            if connector_key == "assisted":
                pack = connector.build_assist_pack(effective_rendition, destination_data)
                return self._finish_certification(run_id, {**outcome, "status": "assisted", "reason": "Posting pack generated; manual publication is required.", "posting_pack": pack})

            published = connector.publish(effective_rendition, destination_data, connection, f"{idempotency_key}:{destination_id}")
            platform_post_id = published.get("platform_post_id")
            if not isinstance(platform_post_id, (str, int)) or not str(platform_post_id).strip():
                return self._finish_certification(
                    run_id,
                    {**outcome, "error_code": "CERTIFICATION_FAILED", "reason": "The connector did not return a valid platform post ID."},
                )
            outcome.update(
                platform_post_id=str(platform_post_id),
                live_url=published.get("live_url"),
                **{
                    key: published[key]
                    for key in ("topic_id", "community_id", "username")
                    if key in published
                },
            )
            status_connection = {**connection}
            if published.get("topic_id") is not None:
                status_connection["topic_id"] = published["topic_id"]
            status_result = connector.get_status(outcome["platform_post_id"], status_connection)
            outcome["status_result"] = status_result
            returned_id = status_result.get("platform_post_id")
            if returned_id is not None and str(returned_id) != outcome["platform_post_id"]:
                return self._finish_certification(run_id, {**outcome, "error_code": "CERTIFICATION_FAILED", "reason": "The status response identified a different post."})
            if status_result.get("status") not in {"live", "published"}:
                return self._finish_certification(run_id, {**outcome, "error_code": "CERTIFICATION_FAILED", "reason": "The test post did not report a live status."})
            delete_connection = {
                **status_connection,
                "updated_at": status_result.get("updated_at"),
            }
            delete_result = connector.delete(outcome["platform_post_id"], delete_connection)
            outcome["delete_result"] = delete_result
            if not delete_result.get("deleted"):
                return self._finish_certification(run_id, {**outcome, "error_code": "CERTIFICATION_FAILED", "reason": "The test post could not be deleted."})
            if delete_result.get("cleanup") == "unpublished":
                return self._finish_certification(
                    run_id,
                    {
                        **outcome,
                        "status": "cleanup_verified",
                        "reason": "Test post was published, confirmed live, and unpublished; permanent deletion is not supported by this connector.",
                    },
                )
            if self.simulation_mode:
                return self._finish_certification(
                    run_id,
                    {
                        **outcome,
                        "status": "simulated",
                        "reason": "The simulated connector completed publication, status, and cleanup checks; no real-platform certification was performed.",
                    },
                )
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

    def _environment_classification(self, body: dict) -> str:
        connection = body.get("connection", {})
        if connection.get("local_test"):
            return "local_emulator"
        if self.simulation_mode:
            return "simulation"
        requested = connection.get("environment")
        if requested in {"approved_sandbox", "real_owned_instance", "self_hosted_test_instance"}:
            return requested
        return "unclassified"

    def _write_certification_artifact(self, outcome: dict) -> None:
        """Write review evidence without copying connection credentials."""
        connector = outcome.get("connector") or "unknown"
        artifact = {
            key: outcome.get(key)
            for key in (
                "certification_run_id",
                "destination_id",
                "connector",
                "environment",
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
        directory = self.evidence_dir / str(connector)
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

        if body.get("enable_tailoring"):
            from app.tailoring import tailor_for_destination
            manifest = self.connector_registry.manifests.get(connector_key) if self.connector_registry else None
            tailor_res = tailor_for_destination(
                rendition,
                destination_data,
                connector=connector,
                manifest=manifest,
                connection=connection,
                options=body.get("options", {}),
            )
            base["tailored_content"] = tailor_res["tailored_content"]
            base["applied_transformations"] = tailor_res["applied_transformations"]
            if not tailor_res["ready_for_publishing"]:
                reason = tailor_res["validation_errors"][0] if tailor_res["validation_errors"] else "Content tailoring validation failed."
                return {
                    **base,
                    "error_code": "DEST_SPEC_VIOLATION",
                    "reason": reason,
                    "details": tailor_res["validation_errors"],
                }
            rendition = tailor_res["tailored_content"]

        decision = check_rules(rendition, destination_data, self._confirmed_post_history(destination_id))
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

        if connector_key != "assisted":
            manifest = self.connector_registry.manifests.get(connector_key) if self.connector_registry else None
            capabilities = set((manifest or {}).get("capabilities", []))
            certified = (
                destination_data.get("level") in {"L3", "L4"}
                and destination_data.get("last_certified_at") is not None
            )
            if "publish" not in capabilities or not certified:
                return {
                    **base,
                    "error_code": "CONNECTOR_NOT_READY",
                    "reason": "Automated publication requires a publish-capable connector and verified L3/L4 certification.",
                    "readiness": {
                        "connector_key": connector_key,
                        "publish_capable": "publish" in capabilities,
                        "certification_level": destination_data.get("level", "L0"),
                        "last_certified_at": destination_data.get("last_certified_at"),
                    },
                }

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
                **{
                    key: published[key]
                    for key in ("topic_id", "community_id", "username")
                    if key in published
                },
            }
        except AppError as error:
            logger.warning("destination publishing failed", extra={"destination_id": destination_id, "error_code": error.code})
            status = "outcome_unknown" if error.code == "PUBLISH_OUTCOME_UNKNOWN" else base["status"]
            return {**base, "status": status, "error_code": error.code, "reason": error.message}
        except Exception:
            logger.exception("destination publishing failed", extra={"destination_id": destination_id})
            return {**base, "error_code": "INTERNAL_ERROR", "reason": "Destination publishing failed."}

    def _confirmed_post_history(self, destination_id: str) -> list[dict]:
        """Return persisted timestamps only for confirmed publishing-job outcomes."""
        history = []
        with self.session_factory() as session:
            # Both publishing jobs and orchestrations can confirm a post. Read
            # the same durable evidence across workflow types so switching API
            # paths cannot bypass a destination frequency cap.
            jobs = session.scalars(select(Job).where(Job.job_type.in_(("publishing", "orchestration")))).all()
            for job in jobs:
                result = job.result or {}
                report = result.get("orchestration") or result
                for item in report.get("destinations", []):
                    if item.get("destination_id") == destination_id and item.get("status") == "published":
                        # The schema has no per-post timestamp; terminal job update time is the durable proxy.
                        history.append({"posted_at": job.updated_at or job.created_at})
        return history

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
