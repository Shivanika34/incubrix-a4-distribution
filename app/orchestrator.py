"""End-to-end multi-destination publishing orchestrator service."""

from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy import select

from app.connectors.registry import ConnectorRegistry
from app.connectors_api import server_connection
from app.database import Destination, Job, RuleDecision
from app.errors import AppError
from app.recommender import RecommendationService
from app.rules import check_rules
from app.tailoring import compute_content_ref, tailor_for_destination

logger = logging.getLogger(__name__)


class PublishingOrchestrator:
    """Coordinates recommendation, tailoring, safety rules, and multi-destination dispatch."""

    def __init__(
        self,
        session_factory,
        connector_registry: ConnectorRegistry | None = None,
        disabled_connectors: frozenset[str] = frozenset(),
        *,
        simulation_mode: bool = True,
    ):
        self.session_factory = session_factory
        self.connector_registry = connector_registry
        self.disabled_connectors = disabled_connectors
        self.simulation_mode = simulation_mode

    def _confirmed_post_history(self, destination_id: str) -> list[dict[str, Any]]:
        """Return timestamps only for publications with confirmed success evidence."""
        history: list[dict[str, Any]] = []
        with self.session_factory() as session:
            jobs = session.scalars(select(Job)).all()
            for job in jobs:
                result = job.result or {}
                report = result.get("orchestration") or result
                for item in report.get("destinations", []):
                    if item.get("destination_id") == destination_id and item.get("status") == "published":
                        # Job timestamps are the available durable attempt timestamps in this schema.
                        history.append({"posted_at": job.updated_at or job.created_at})
        return history

    def run_workflow(
        self,
        body: dict[str, Any],
        idempotency_key: str,
        progress: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        """Execute the end-to-end publishing orchestration workflow."""
        content_item = body.get("content_item")
        if not content_item or not isinstance(content_item, dict):
            raise AppError("REQUEST_VALIDATION_ERROR", "content_item is required and must be an object.", False)

        content_ref = compute_content_ref(content_item)
        destination_ids = body.get("destination_ids")
        recommendation_settings = body.get("recommendation") or {}
        dispatch = bool(body.get("dispatch", False))
        connections = body.get("connections") or {}
        options = body.get("options") or {}

        # 1. Resolve target destinations (explicit vs recommender)
        targets: list[dict[str, Any]] = []
        selection_basis = "explicit"

        if destination_ids and len(destination_ids) > 0:
            selection_basis = "explicit"
            with self.session_factory() as session:
                for dest_id in destination_ids:
                    dest_record = session.get(Destination, dest_id)
                    if dest_record is None:
                        targets.append({
                            "destination_id": dest_id,
                            "destination_data": None,
                            "selection_basis": "explicit",
                            "recommendation_score": None,
                            "recommendation_rank": None,
                            "recommendation_explanation": None,
                        })
                    else:
                        d_dict = {col.name: getattr(dest_record, col.name) for col in Destination.__table__.columns}
                        targets.append({
                            "destination_id": dest_id,
                            "destination_data": d_dict,
                            "selection_basis": "explicit",
                            "recommendation_score": None,
                            "recommendation_rank": None,
                            "recommendation_explanation": None,
                        })
        else:
            selection_basis = "recommendation"
            recommender = RecommendationService(
                self.session_factory,
                self.connector_registry,
                self.disabled_connectors,
            )
            top_k = int(recommendation_settings.get("top_k", 5))
            min_score = float(recommendation_settings.get("min_score", 0.0))
            rec_result = recommender.recommend(
                content_item,
                topic=recommendation_settings.get("topic"),
                audience_tags=recommendation_settings.get("audience_tags"),
                candidate_destination_ids=recommendation_settings.get("candidate_destination_ids"),
                creator_preferences=recommendation_settings.get("creator_preferences"),
                top_k=top_k,
                min_score=min_score,
                include_excluded=False,
            )

            with self.session_factory() as session:
                for rec_item in rec_result["recommendations"]:
                    dest_id = rec_item["destination_id"]
                    dest_record = session.get(Destination, dest_id)
                    if dest_record is not None:
                        d_dict = {col.name: getattr(dest_record, col.name) for col in Destination.__table__.columns}
                        targets.append({
                            "destination_id": dest_id,
                            "destination_data": d_dict,
                            "selection_basis": "recommendation",
                            "recommendation_score": rec_item["score"],
                            "recommendation_rank": rec_item["rank"],
                            "recommendation_explanation": rec_item["explanation"],
                            "recommendation_eligibility": rec_item["eligibility"],
                        })

        if not targets:
            return {
                "mode": "dispatch" if dispatch else "preview",
                "dispatch_enabled": dispatch,
                "selection_basis": selection_basis,
                "content_ref": content_ref,
                "totals": {
                    "total": 0,
                    "published": 0,
                    "assisted": 0,
                    "preview": 0,
                    "blocked": 0,
                    "excluded": 0,
                    "skipped": 0,
                    "failed": 0,
                    "outcome_unknown": 0,
                },
                "destinations": [],
            }

        # 2. Process each target destination
        outcomes: list[dict[str, Any]] = []
        total_targets = len(targets)

        for index, target in enumerate(targets, start=1):
            dest_id = target["destination_id"]
            dest_data = target["destination_data"]

            base_outcome: dict[str, Any] = {
                "destination_id": dest_id,
                "destination_name": dest_data["name"] if dest_data else "Unknown",
                "selection_basis": target["selection_basis"],
                "recommendation_score": target.get("recommendation_score"),
                "recommendation_rank": target.get("recommendation_rank"),
                "recommendation_explanation": target.get("recommendation_explanation"),
                "status": "failed",
                "error_code": None,
                "reason": None,
                "platform_post_id": None,
                "live_url": None,
                "tailored_content": None,
                "applied_transformations": [],
                "warnings": [],
            }

            if dest_data is None:
                base_outcome.update({
                    "status": "excluded",
                    "error_code": "DESTINATION_NOT_FOUND",
                    "reason": "Destination was not found in the catalogue.",
                })
                outcomes.append(base_outcome)
                if progress:
                    progress(int(index * 100 / total_targets))
                continue

            connector_key = dest_data.get("connector_key", "")
            connector = self.connector_registry.get(connector_key) if self.connector_registry else None
            manifest = (
                self.connector_registry.manifests.get(connector_key)
                if self.connector_registry
                else None
            )
            dest_connection = {
                **connections.get(dest_id, {}),
                "base_url": connections.get(dest_id, {}).get("base_url") or dest_data["url"],
            }
            if not self.simulation_mode:
                # Resolve secrets only in worker memory at the connector boundary.
                # They never enter the persisted job request or browser payload.
                dest_connection = server_connection(connector_key, dest_data, dest_connection)

            # Check connector disabled
            if connector_key in self.disabled_connectors:
                base_outcome.update({
                    "status": "skipped",
                    "error_code": "CONNECTOR_DISABLED",
                    "reason": "Connector is disabled by configuration.",
                })
                outcomes.append(base_outcome)
                if progress:
                    progress(int(index * 100 / total_targets))
                continue

            # Check connector registered
            if connector is None:
                base_outcome.update({
                    "status": "skipped",
                    "error_code": "CONNECTOR_NOT_FOUND",
                    "reason": f"Connector '{connector_key}' is not registered.",
                })
                outcomes.append(base_outcome)
                if progress:
                    progress(int(index * 100 / total_targets))
                continue

            # 3. Content Tailoring per destination
            tailoring_res = tailor_for_destination(
                content_item,
                dest_data,
                connector=connector,
                manifest=manifest,
                connection=dest_connection,
                options=options,
            )
            tailored_content = tailoring_res["tailored_content"]
            applied_transformations = tailoring_res["applied_transformations"]
            base_outcome["tailored_content"] = tailored_content
            base_outcome["applied_transformations"] = applied_transformations
            base_outcome["warnings"].extend(tailoring_res.get("warnings", []))

            # 4. Posting Rules Evaluation on tailored content
            rule_decision = check_rules(
                tailored_content,
                dest_data,
                post_history=self._confirmed_post_history(dest_id),
            )
            with self.session_factory.begin() as session:
                session.add(RuleDecision(
                    destination_id=dest_id,
                    outcome=rule_decision.outcome,
                    reason=rule_decision.reason,
                    error_code=rule_decision.error_code,
                ))

            assisted_reason = "This destination requires assisted posting."
            assisted_only_block = (
                rule_decision.outcome == "blocked"
                and rule_decision.reason == assisted_reason
            )
            is_assisted = (
                connector_key == "assisted"
                or not dest_data.get("automation_allowed", True)
                or assisted_only_block
            )

            # Check rule violations (never bypassed by recommendation score)
            if rule_decision.outcome == "blocked" and not assisted_only_block:
                base_outcome.update({
                    "status": "blocked",
                    "error_code": rule_decision.error_code or "DEST_SPEC_VIOLATION",
                    "reason": rule_decision.reason,
                })
                outcomes.append(base_outcome)
                if progress:
                    progress(int(index * 100 / total_targets))
                continue

            if rule_decision.outcome == "reschedule":
                base_outcome.update({
                    "status": "blocked",
                    "error_code": rule_decision.error_code or "DEST_RULE_FREQUENCY_CAP",
                    "reason": rule_decision.reason,
                    "retry_at": rule_decision.retry_at,
                })
                outcomes.append(base_outcome)
                if progress:
                    progress(int(index * 100 / total_targets))
                continue

            if not tailoring_res["ready_for_publishing"]:
                base_outcome.update({
                    "status": "blocked",
                    "error_code": "DEST_SPEC_VIOLATION",
                    "reason": tailoring_res["validation_errors"][0] if tailoring_res["validation_errors"] else "Content tailoring validation failed.",
                    "details": tailoring_res["validation_errors"],
                })
                outcomes.append(base_outcome)
                if progress:
                    progress(int(index * 100 / total_targets))
                continue

            if not is_assisted:
                capabilities = set((manifest or {}).get("capabilities", []))
                certified = dest_data.get("level") in {"L3", "L4"} and dest_data.get("last_certified_at") is not None
                if "publish" not in capabilities or not certified:
                    base_outcome.update({
                        "status": "skipped",
                        "error_code": "CONNECTOR_NOT_READY",
                        "reason": "Automated publication requires a publish-capable connector and verified L3/L4 certification.",
                        "readiness": {
                            "connector_key": connector_key,
                            "publish_capable": "publish" in capabilities,
                            "certification_level": dest_data.get("level", "L0"),
                            "last_certified_at": dest_data.get("last_certified_at"),
                        },
                    })
                    outcomes.append(base_outcome)
                    if progress:
                        progress(int(index * 100 / total_targets))
                    continue

            effective_rendition = rule_decision.rendition or tailored_content

            # 5. Handle Assisted Destinations
            if is_assisted:
                if hasattr(connector, "build_assist_pack"):
                    pack = connector.build_assist_pack(effective_rendition, dest_data)
                else:
                    caption = "\n\n".join(v for v in [effective_rendition.get("title", ""), effective_rendition.get("body", "")] if v).strip()
                    pack = {
                        "files": effective_rendition.get("files", []),
                        "caption": caption,
                        "deep_link": dest_data.get("submit_url") or dest_data.get("url"),
                        "checklist": ["Review text and links", "Confirm destination rules", "Open submit link and post manually"],
                    }
                base_outcome.update({
                    "status": "assisted",
                    "reason": "Posting pack generated; manual publication is required.",
                    "posting_pack": pack,
                })
                outcomes.append(base_outcome)
                if progress:
                    progress(int(index * 100 / total_targets))
                continue

            # 6. Automated Publishing or Preview
            if not dispatch:
                # Preview / dry-run mode: ZERO publication calls
                base_outcome.update({
                    "status": "preview",
                    "reason": "Validated and ready for automated publication (dispatch not requested).",
                    "ready_to_publish": True,
                })
                outcomes.append(base_outcome)
                if progress:
                    progress(int(index * 100 / total_targets))
                continue

            # Dispatch mode enabled: execute publication
            try:
                validation = connector.validate(effective_rendition, dest_data, dest_connection)
                if not validation.get("ok"):
                    base_outcome.update({
                        "status": "failed",
                        "error_code": "DEST_SPEC_VIOLATION",
                        "reason": "The content violates the destination specification.",
                        "details": validation.get("violations", []),
                    })
                    outcomes.append(base_outcome)
                    if progress:
                        progress(int(index * 100 / total_targets))
                    continue

                published = connector.publish(
                    effective_rendition,
                    dest_data,
                    dest_connection,
                    f"{idempotency_key}:{dest_id}",
                )
                if not isinstance(published, dict) or not published.get("platform_post_id"):
                    raise AppError(
                        "PUBLISH_OUTCOME_UNKNOWN",
                        "The connector returned no confirmed publication identifier. Check the destination before retrying.",
                        True,
                    )
                base_outcome.update({
                    "status": "published",
                    "reason": "Published successfully.",
                    "platform_post_id": published.get("platform_post_id"),
                    "live_url": published.get("live_url"),
                    **{
                        k: published[k]
                        for k in ("topic_id", "community_id", "username")
                        if k in published
                    },
                })
            except AppError as error:
                logger.warning("orchestration destination publish failed", extra={"destination_id": dest_id, "error_code": error.code})
                status = "outcome_unknown" if error.code == "PUBLISH_OUTCOME_UNKNOWN" else "failed"
                base_outcome.update({
                    "status": status,
                    "error_code": error.code,
                    "reason": error.message,
                })
            except Exception:
                logger.exception("orchestration destination publish encountered unexpected error", extra={"destination_id": dest_id})
                base_outcome.update({
                    "status": "failed",
                    "error_code": "INTERNAL_ERROR",
                    "reason": "Destination publishing failed unexpectedly.",
                })

            outcomes.append(base_outcome)
            if progress:
                progress(int(index * 100 / total_targets))

        # 7. Aggregate job status
        published_count = sum(1 for d in outcomes if d["status"] == "published")
        assisted_count = sum(1 for d in outcomes if d["status"] == "assisted")
        preview_count = sum(1 for d in outcomes if d["status"] == "preview")
        blocked_count = sum(1 for d in outcomes if d["status"] == "blocked")
        excluded_count = sum(1 for d in outcomes if d["status"] == "excluded")
        skipped_count = sum(1 for d in outcomes if d["status"] == "skipped")
        failed_count = sum(1 for d in outcomes if d["status"] == "failed")
        uncertain_count = sum(1 for d in outcomes if d["status"] == "outcome_unknown")

        successful = published_count + assisted_count + preview_count

        if not outcomes:
            job_status = "succeeded"
        else:
            if failed_count == 0 and uncertain_count == 0 and blocked_count == 0 and excluded_count == 0 and skipped_count == 0:
                job_status = "succeeded"
            elif successful > 0 or uncertain_count > 0:
                job_status = "partial"
            else:
                job_status = "failed"

        result = {
            "mode": "dispatch" if dispatch else "preview",
            "dispatch_enabled": dispatch,
            "selection_basis": selection_basis,
            "content_ref": content_ref,
            "job_status": job_status,
            "totals": {
                "total": len(outcomes),
                "published": published_count,
                "assisted": assisted_count,
                "preview": preview_count,
                "blocked": blocked_count,
                "excluded": excluded_count,
                "skipped": skipped_count,
                "failed": failed_count,
                "outcome_unknown": uncertain_count,
            },
            "destinations": outcomes,
        }
        return result
