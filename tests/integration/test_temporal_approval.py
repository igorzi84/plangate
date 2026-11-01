import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from plangate.config import AppSettings
from plangate.domain.models import (
    ApprovalRecord,
    PolicyConfig,
    Review,
    ReviewResult,
)
from plangate.explanation_providers.deterministic import (
    DeterministicExplanationProvider,
)
from plangate.main import create_app
from plangate.stores.sqlite import SQLiteReviewStore
from plangate.temporal.activities.approval import ApprovalActivities
from plangate.temporal.workflow_starter import TemporalApprovalWorkflowStarter
from plangate.temporal.workflows.approval import ApprovalWorkflow
from plangate.temporal.workflows.models import ApprovalWorkflowInput


@pytest.mark.parametrize(
    "plan_fixture, expected_decision, approval_status, can_deploy",
    [
        ("aws_sg_update_plan", "block", "approved", False),
        ("aws_sg_update_plan", "block", "rejected", False),
        ("create_plan", "approve", "approved", True),
        ("create_plan", "approve", "rejected", False),
    ],
)
def test_temporal_approval(
    tmp_path, request, plan_fixture, expected_decision, approval_status, can_deploy
):
    plan = request.getfixturevalue(plan_fixture)

    async def run_test():
        async with await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter,
        ) as env:
            store = SQLiteReviewStore(tmp_path / "reviews.db")
            activities = ApprovalActivities(store)

            with ThreadPoolExecutor(max_workers=2) as executor:
                async with Worker(
                    env.client,
                    task_queue="plangate-approval",
                    workflows=[ApprovalWorkflow],
                    activities=[activities.save_approval],
                    activity_executor=executor,
                    workflow_runner=SandboxedWorkflowRunner(
                        restrictions=SandboxRestrictions.default.with_passthrough_modules(
                            "annotated_types"
                        ),
                    ),
                ):
                    app = create_app(
                        PolicyConfig(),
                        DeterministicExplanationProvider(),
                        settings=AppSettings(
                            sqlite_db_path=tmp_path / "reviews.db",
                            host="127.0.0.1",
                            temporal_address=env.client.service_client.config.target_host,
                            llm_provider="deterministic",
                            llm_model="qwen3:4b",
                            llm_timeout=30
                        ),
                        review_store=store,
                        workflow_starter=TemporalApprovalWorkflowStarter(env.client),
                    )

                    async with (
                        app.router.lifespan_context(app),
                        AsyncClient(
                            transport=ASGITransport(app=app),
                            base_url="http://test",
                        ) as client,
                    ):
                        response = await client.post("/reviews", json={"plan": plan})
                        assert response.status_code == 200
                        review_id = response.json()["review_id"]
                        assert response.json()["decision"] == expected_decision

                        response = await client.get(f"/reviews/{review_id}")
                        assert response.status_code == 200
                        assert response.json()["approval_status"] == "pending"

                        response = await client.post(
                            f"/reviews/{review_id}/approval",
                            json={
                                "status": approval_status,
                                "reviewer": "Igor",
                                "reason": "Reviewed the plan",
                            },
                        )
                        assert response.status_code == 202

                        handle = env.client.get_workflow_handle(f"approval-{review_id}")
                        result = await asyncio.wait_for(handle.result(), timeout=15)
                        assert result == approval_status

                        response = await client.get(f"/reviews/{review_id}")
                        assert response.status_code == 200
                        assert response.json()["approval_status"] == approval_status
                        assert response.json()["can_deploy"] is can_deploy

                        response = await client.get(f"/reviews/{review_id}/approval")
                        assert response.status_code == 200
                        audit = response.json()
                        assert audit["status"] == approval_status
                        assert audit["reviewer"] == "Igor"
                        assert audit["reason"] == "Reviewed the plan"

    asyncio.run(run_test())


def test_temporal_approval_retries_after_database_commit(tmp_path, monkeypatch):
    async def run_test():
        async with await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter,
        ) as env:
            sqlite_db_path = tmp_path / "reviews.db"
            store = SQLiteReviewStore(sqlite_db_path)
            record = ApprovalRecord(
                review_id=uuid4(),
                status="approved",
                reviewer="Igor",
                reason="Reviewed the plan",
                decided_at=datetime(2026, 10, 19, 14, 0, tzinfo=UTC),
            )
            store.save_review(
                Review(
                    review_id=record.review_id,
                    result=ReviewResult(decision="approve", findings=()),
                    plan_hash="retry-test-plan",
                )
            )
            save_approval = store.save_approval
            attempts = []
            persisted_records = []

            def save_then_fail_once(approval_record: ApprovalRecord) -> None:
                attempts.append(activity.info().attempt)
                save_approval(approval_record)
                # Read through a new connection to prove the commit succeeded
                # before simulating failure to acknowledge activity completion.
                persisted_records.append(store.get_record(approval_record.review_id))
                if len(attempts) == 1:
                    raise RuntimeError("Simulated failure after database commit")

            monkeypatch.setattr(store, "save_approval", save_then_fail_once)
            activities = ApprovalActivities(store)

            with ThreadPoolExecutor(max_workers=2) as executor:
                async with Worker(
                    env.client,
                    task_queue="plangate-approval",
                    workflows=[ApprovalWorkflow],
                    activities=[activities.save_approval],
                    activity_executor=executor,
                    workflow_runner=SandboxedWorkflowRunner(
                        restrictions=SandboxRestrictions.default.with_passthrough_modules(
                            "annotated_types"
                        ),
                    ),
                ):
                    starter = TemporalApprovalWorkflowStarter(env.client)
                    workflow_id = await starter.start(
                        ApprovalWorkflowInput(
                            review_id=str(record.review_id), policy_result="approve"
                        )
                    )
                    await starter.record_decision(record)
                    handle = env.client.get_workflow_handle(workflow_id)
                    result = await asyncio.wait_for(handle.result(), timeout=15)

                    assert result == "approved"
                    assert attempts == [1, 2]
                    assert persisted_records == [record, record]
                    reopened_store = SQLiteReviewStore(sqlite_db_path)
                    assert reopened_store.get_record(record.review_id) == record
                    assert (
                        reopened_store.get_review(record.review_id).approval_status
                        == "approved"
                    )

    asyncio.run(run_test())
