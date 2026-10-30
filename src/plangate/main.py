import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import prometheus_client
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from plangate.api.v1.schemas import (
    ApprovalRecordResponse,
    ApprovalRequest,
    FindingResponse,
    ReviewRequest,
    ReviewResponse,
    StoredReviewResponse,
)
from plangate.config import AppSettings, load_app_settings, load_policy_config
from plangate.domain.deployment import can_deploy
from plangate.domain.errors import InvalidPlanError
from plangate.domain.models import ApprovalRecord, PolicyConfig, Review
from plangate.domain.plan_hash import hash_plan
from plangate.domain.plan_parser import parse_plan
from plangate.domain.review import evaluate_review
from plangate.explanation_providers.base import ExplanationProvider
from plangate.explanation_providers.deterministic import (
    DeterministicExplanationProvider,
)
from plangate.explanation_providers.ollama import OllamaExplanationProvider
from plangate.logging_config import configure_logging
from plangate.metrics import ReviewMetrics
from plangate.stores.base import ReviewStore
from plangate.stores.memory import InMemoryReviewStore
from plangate.stores.sqlite import SQLiteReviewStore
from plangate.temporal.workflow_starter import (
    ApprovalWorkflowStarter,
    TemporalApprovalWorkflowStarter,
)
from plangate.temporal.workflows.models import ApprovalWorkflowInput

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
POLICY_CONFIG_PATH = CONFIG_DIR / "policies.toml"
logger = logging.getLogger(__name__)


def to_stored_review_response(review: Review) -> StoredReviewResponse:
    findings = [
        FindingResponse(
            id=finding.id,
            severity=finding.severity,
            resource_address=finding.resource_address,
            evidence=finding.evidence,
            remediation=finding.remediation,
        )
        for finding in review.result.findings
    ]
    deploy = can_deploy(review)
    return StoredReviewResponse(
        review_id=review.review_id,
        plan_hash=review.plan_hash,
        decision=review.result.decision,
        findings=findings,
        approval_status=review.approval_status,
        can_deploy=deploy,
    )


def create_explanation_provider(settings: AppSettings) -> ExplanationProvider:
    map = {
        "deterministic": DeterministicExplanationProvider(),
        "ollama": OllamaExplanationProvider(settings),
    }
    try:
        provider = map[settings.llm_provider]
    except KeyError:
        raise ValueError(f"{settings.llm_provider} is unsupported value.")
    return provider


def create_app(
    policy_config: PolicyConfig,
    explanation_provider: ExplanationProvider,
    settings: AppSettings,
    review_store: ReviewStore | None = None,
    workflow_starter: ApprovalWorkflowStarter | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        nonlocal workflow_starter
        if workflow_starter is None:
            client = await Client.connect(
                settings.temporal_address, data_converter=pydantic_data_converter
            )
            workflow_starter = TemporalApprovalWorkflowStarter(client)
        yield

    app = FastAPI(title="PlanGate", lifespan=lifespan)
    metrics = ReviewMetrics()
    app.state.metrics = metrics

    if review_store is None:
        review_store = InMemoryReviewStore()

    @app.post("/reviews")
    async def create_review(request: ReviewRequest) -> ReviewResponse:
        started_at = time.monotonic()
        review_id = uuid4()
        plan_hash = hash_plan(request.plan)

        try:
            changes = await run_in_threadpool(parse_plan, request.plan)
        except InvalidPlanError as error:
            raise HTTPException(
                422, detail={"code": "invalid_plan", "message": str(error)}
            )

        result = await run_in_threadpool(evaluate_review, changes, policy_config)
        review = Review(review_id=review_id, result=result, plan_hash=plan_hash)
        await run_in_threadpool(review_store.save_review, review)

        findings = [
            FindingResponse(
                id=finding.id,
                severity=finding.severity,
                resource_address=finding.resource_address,
                evidence=finding.evidence,
                remediation=finding.remediation,
            )
            for finding in result.findings
        ]

        try:
            explanation_started_at = time.monotonic()
            explanation = await run_in_threadpool(explanation_provider.explain, result)
            metrics.record_explanation_duration(
                provider_name=explanation_provider.name,
                seconds=time.monotonic() - explanation_started_at,
            )
        except Exception:
            logger.exception(
                "Review explanation failed",
                extra={
                    "review_id": str(review_id),
                    "plan_hash": plan_hash,
                    "policy_result": result.decision,
                },
            )
            metrics.record_explanation_fallback(explanation_provider.name)
            fallback_provider = DeterministicExplanationProvider()
            fallback_started_at = time.monotonic()
            explanation = await run_in_threadpool(fallback_provider.explain, result)
            metrics.record_explanation_duration(
                provider_name=fallback_provider.name,
                seconds=time.monotonic() - fallback_started_at,
            )
        workflow_id = None
        if workflow_starter is not None:
            input = ApprovalWorkflowInput(
                review_id=str(review_id),
                policy_result=result.decision,
            )
            workflow_id = await workflow_starter.start(input)
        metrics.record_review(result.decision)
        for finding in result.findings:
            metrics.record_policy_findings(finding)
        duration = time.monotonic() - started_at
        metrics.record_review_duration(result.decision, duration)
        logger.info(
            "Review created",
            extra={
                "review_id": str(review_id),
                "plan_hash": plan_hash,
                "policy_result": result.decision,
                "workflow_id": workflow_id,
            },
        )
        return ReviewResponse(
            review_id=review_id,
            plan_hash=plan_hash,
            decision=result.decision,
            findings=findings,
            explanation=explanation,
        )

    @app.get("/reviews/{review_id}")
    def get_review(review_id: UUID) -> StoredReviewResponse:
        try:
            review = review_store.get_review(review_id)
        except KeyError:
            raise HTTPException(
                404, detail={"code": "review_not_found", "message": "Review not found"}
            )

        return to_stored_review_response(review)

    @app.post("/reviews/{review_id}/approval", status_code=202)
    async def approve_review(
        approval_request: ApprovalRequest, review_id: UUID
    ) -> StoredReviewResponse:
        try:
            review = await run_in_threadpool(review_store.get_review, review_id)
        except KeyError:
            raise HTTPException(
                404,
                detail={
                    "code": "review_not_found",
                    "message": "Review not found.",
                },
            )

        if review.approval_status != "pending":
            raise HTTPException(
                409,
                detail={
                    "code": "review_not_pending",
                    "message": "Review already has a human decision",
                },
            )

        approval_record = ApprovalRecord(
            review_id=review_id,
            status=approval_request.status,
            reviewer=approval_request.reviewer,
            reason=approval_request.reason,
        )

        if workflow_starter is not None:
            await workflow_starter.record_decision(approval_record)
        metrics.record_approval_submission(approval_request.status)
        review = await run_in_threadpool(review_store.get_review, review_id=review_id)

        logger.info(
            "Human decision submitted",
            extra={
                "review_id": str(review.review_id),
                "plan_hash": review.plan_hash,
                "policy_result": review.result.decision,
                "requested_approval_status": approval_request.status,
            },
        )
        return to_stored_review_response(review)

    @app.get("/reviews/{review_id}/approval")
    def get_record(review_id: UUID) -> ApprovalRecordResponse:
        try:
            record = review_store.get_record(review_id)
        except KeyError:
            raise HTTPException(
                404,
                detail={
                    "code": "approval_not_found",
                    "message": "Approval record not found.",
                },
            )

        return ApprovalRecordResponse(
            review_id=review_id,
            status=record.status,
            reviewer=record.reviewer,
            reason=record.reason,
            decided_at=record.decided_at,
        )

    @app.get("/metrics")
    def get_metrics() -> Response:
        return Response(
            prometheus_client.generate_latest(metrics.registry),
            headers={"Content-Type": prometheus_client.CONTENT_TYPE_LATEST},
        )

    return app


def main() -> None:
    configure_logging()
    policy_config = load_policy_config(POLICY_CONFIG_PATH)
    settings = load_app_settings()
    review_store = SQLiteReviewStore(settings.db_path)

    app = create_app(
        policy_config,
        explanation_provider=create_explanation_provider(settings),
        review_store=review_store,
        settings=settings,
    )
    uvicorn.run(app, host=settings.host, port=8000)
