import logging
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import prometheus_client
import pytest
from fastapi.testclient import TestClient
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment

from plangate.config import AppSettings
from plangate.domain.models import (
    ApprovalRecord,
    PolicyConfig,
    ReviewResult,
)
from plangate.domain.plan_hash import hash_plan
from plangate.explanation_providers.deterministic import (
    DeterministicExplanationProvider,
)
from plangate.explanation_providers.ollama import OllamaExplanationProvider
from plangate.main import create_app, create_explanation_provider
from plangate.stores.memory import InMemoryReviewStore
from plangate.stores.sqlite import SQLiteReviewStore
from plangate.temporal.activities.approval import ApprovalActivities
from plangate.temporal.workflows.models import ApprovalWorkflowInput


class FakeExplanationProvider:
    name = "fake provider"

    def explain(self, review: ReviewResult) -> str:
        return "Mocked explanation."


class BadExplanationProvider:
    name = "bad provider"

    def explain(self, review: ReviewResult) -> str:
        raise RuntimeError()


class FakeApprovalWorkflowStarter:
    def __init__(self):
        self.inputs: list[ApprovalWorkflowInput] = []
        self.decisions: list[ApprovalRecord] = []

    async def start(self, input: ApprovalWorkflowInput) -> str:
        self.inputs.append(input)
        return "workflow_id"

    async def record_decision(self, approval_record: ApprovalRecord) -> None:
        self.decisions.append(approval_record)


class BadApprovalWorkflowStarter:
    async def start(self, input: ApprovalWorkflowInput) -> str:
        raise RuntimeError("Temporal unavailable")

    async def record_decision(self, approval_record: ApprovalRecord) -> None:
        raise RuntimeError("Temporal unavailable")


TEST_SETTINGS = AppSettings(
    db_path=Path("unused-test.db"),
    host="127.0.0.1",
    temporal_address="temporal.test:7233",
    llm_provider="deterministic",
    llm_model="qwen3:4b",
    llm_timeout=30,
)


app = create_app(
    PolicyConfig(),
    DeterministicExplanationProvider(),
    settings=TEST_SETTINGS,
    workflow_starter=FakeApprovalWorkflowStarter(),
)
client = TestClient(app)


@pytest.fixture
def approval_store():
    return InMemoryReviewStore()


@pytest.fixture
def approval_client(approval_store):
    starter = FakeApprovalWorkflowStarter()
    test_app = create_app(
        PolicyConfig(),
        DeterministicExplanationProvider(),
        settings=TEST_SETTINGS,
        review_store=approval_store,
        workflow_starter=starter,
    )
    with TestClient(test_app) as test_client:
        yield test_client, starter


def test_lifespan_reuses_temporal_client(monkeypatch, create_plan):
    temporal_client = Mock()
    temporal_client.start_workflow = AsyncMock(return_value=Mock(id="workflow_id"))
    connect = AsyncMock(return_value=temporal_client)
    monkeypatch.setattr("plangate.main.Client.connect", connect)
    test_app = create_app(
        PolicyConfig(), FakeExplanationProvider(), settings=TEST_SETTINGS
    )

    with TestClient(test_app) as test_client:
        connect.assert_awaited_once_with(
            TEST_SETTINGS.temporal_address,
            data_converter=pydantic_data_converter,
        )
        for _ in range(2):
            response = test_client.post("/reviews", json={"plan": create_plan})
            assert response.status_code == 200
            body = response.json()
            assert temporal_client.start_workflow.await_args.args[1] == (
                ApprovalWorkflowInput(
                    review_id=body["review_id"], policy_result=body["decision"]
                )
            )
            assert temporal_client.start_workflow.await_args.kwargs["id"] == (
                f"approval-{body['review_id']}"
            )

    connect.assert_awaited_once()
    assert temporal_client.start_workflow.await_count == 2


def test_lifespan_uses_injected_starter(monkeypatch, create_plan):
    connect = AsyncMock(side_effect=AssertionError("Unexpected Temporal connection"))
    monkeypatch.setattr("plangate.main.Client.connect", connect)
    starter = FakeApprovalWorkflowStarter()
    test_app = create_app(
        PolicyConfig(),
        FakeExplanationProvider(),
        settings=TEST_SETTINGS,
        workflow_starter=starter,
    )

    with TestClient(test_app) as test_client:
        response = test_client.post("/reviews", json={"plan": create_plan})
        assert response.status_code == 200
        body = response.json()

    connect.assert_not_awaited()
    assert starter.inputs == [
        ApprovalWorkflowInput(
            review_id=body["review_id"], policy_result=body["decision"]
        )
    ]


def test_lifespan_connection_failure_prevents_startup(monkeypatch):
    connect = AsyncMock(side_effect=RuntimeError("Temporal unavailable"))
    monkeypatch.setattr("plangate.main.Client.connect", connect)
    test_app = create_app(
        PolicyConfig(), FakeExplanationProvider(), settings=TEST_SETTINGS
    )

    with (
        pytest.raises(RuntimeError, match="Temporal unavailable"),
        TestClient(test_app),
    ):
        pytest.fail("App started despite a failed Temporal connection")

    connect.assert_awaited_once_with(
        TEST_SETTINGS.temporal_address,
        data_converter=pydantic_data_converter,
    )


def test_create_review_approve(create_plan):
    plan_hash = hash_plan(create_plan)
    response = client.post("/reviews", json={"plan": create_plan})
    assert response.status_code == 200
    response_json = response.json()

    assert response_json["decision"] == "approve"
    assert response_json["findings"] == []
    assert "approve" in response_json["explanation"]
    assert "Human approval is still required" in response_json["explanation"]
    assert response_json["plan_hash"] == plan_hash


def test_create_review_block(aws_sg_update_plan):
    finding = {
        "id": "public-inbound-access",
        "severity": "high",
        "resource_address": "aws_security_group.web",
        "evidence": "ingress.cidr_blocks contains 0.0.0.0/0",
        "remediation": "Restrict ingress to approved networks.",
    }
    response = client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200

    response_json = response.json()
    assert response_json["decision"] == "block"
    assert response_json["findings"] == [finding]
    assert "explanation" in response_json
    assert finding["id"] in response_json["explanation"]
    assert finding["severity"] in response_json["explanation"]
    assert finding["evidence"] in response_json["explanation"]
    assert finding["resource_address"] in response_json["explanation"]


def test_custom_app_approve(aws_sg_update_plan):
    """We are using custom app with empty policy config, so it shouldnt block anything"""

    custom_app = create_app(
        PolicyConfig(enabled_policy_ids=frozenset()),
        DeterministicExplanationProvider(),
        settings=TEST_SETTINGS,
        workflow_starter=FakeApprovalWorkflowStarter(),
    )
    custom_client = TestClient(custom_app)

    response = custom_client.post("/reviews", json={"plan": aws_sg_update_plan})
    response_json = response.json()
    assert response.status_code == 200
    assert response_json["decision"] == "approve"
    assert response_json["findings"] == []
    assert "approve" in response_json["explanation"]
    assert "Human approval is still required" in response_json["explanation"]


def test_parse_plan_requires_resource_changes():
    response = client.post("/reviews", json={"plan": {}})
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "invalid_plan",
            "message": "resource_changes is required",
        }
    }


def test_parse_plan_resource_changes_is_list():
    response = client.post("/reviews", json={"plan": {"resource_changes": {}}})
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "invalid_plan",
            "message": "resource_changes must be a list",
        }
    }


def test_parse_plan_item_in_resource_changes_is_dict():
    response = client.post("/reviews", json={"plan": {"resource_changes": [None]}})
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "invalid_plan",
            "message": "resource_changes entries must be objects",
        }
    }


def test_parse_plan_resource_changes_change_is_dict():
    response = client.post(
        "/reviews", json={"plan": {"resource_changes": [{"change": None}]}}
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "invalid_plan",
            "message": "resource change change field must be an object",
        }
    }


def test_fake_explanation_provider(aws_sg_update_plan):
    custom_app = create_app(
        PolicyConfig(enabled_policy_ids=frozenset({"public-inbound-access"})),
        FakeExplanationProvider(),
        settings=TEST_SETTINGS,
        workflow_starter=FakeApprovalWorkflowStarter(),
    )
    custom_client = TestClient(custom_app)
    finding = {
        "id": "public-inbound-access",
        "severity": "high",
        "resource_address": "aws_security_group.web",
        "evidence": "ingress.cidr_blocks contains 0.0.0.0/0",
        "remediation": "Restrict ingress to approved networks.",
    }
    response = custom_client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200

    response_json = response.json()
    assert response_json["findings"] == [finding]
    assert response_json["decision"] == "block"
    assert response_json["explanation"] == "Mocked explanation."


def test_unique_review_id(create_plan):
    first_response = client.post("/reviews", json={"plan": create_plan})
    second_response = client.post("/reviews", json={"plan": create_plan})

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    first_id = UUID(first_response.json()["review_id"])
    second_id = UUID(second_response.json()["review_id"])

    assert first_id.version == 4
    assert second_id.version == 4
    assert first_id != second_id


def test_get_saved_review(aws_sg_update_plan):
    plan_hash = hash_plan(aws_sg_update_plan)
    finding = {
        "id": "public-inbound-access",
        "severity": "high",
        "resource_address": "aws_security_group.web",
        "evidence": "ingress.cidr_blocks contains 0.0.0.0/0",
        "remediation": "Restrict ingress to approved networks.",
    }

    response = client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200

    review = response.json()
    review_id = review["review_id"]
    assert review["plan_hash"] == plan_hash

    response = client.get(f"/reviews/{review_id}")
    assert response.status_code == 200

    review = response.json()
    assert review["review_id"] == review_id
    assert review["findings"] == [finding]
    assert review["decision"] == "block"
    assert review["approval_status"] == "pending"
    assert review["plan_hash"] == plan_hash


def test_get_unknown_uuid():
    review_id = uuid4()
    response = client.get(f"/reviews/{review_id}")
    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "code": "review_not_found",
            "message": "Review not found",
        }
    }


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_record_human_decision(aws_sg_update_plan, status, approval_client):
    client, starter = approval_client
    finding = {
        "id": "public-inbound-access",
        "severity": "high",
        "resource_address": "aws_security_group.web",
        "evidence": "ingress.cidr_blocks contains 0.0.0.0/0",
        "remediation": "Restrict ingress to approved networks.",
    }
    response = client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    response = client.post(
        f"/reviews/{review_id}/approval",
        json={"status": status, "reviewer": "Igor", "reason": "Reason"},
    )
    assert response.status_code == 202

    review = response.json()
    assert review["review_id"] == review_id
    assert review["findings"] == [finding]
    assert review["decision"] == "block"
    assert review["approval_status"] == "pending"
    assert review["can_deploy"] is False
    assert len(starter.decisions) == 1
    record = starter.decisions[0]
    assert str(record.review_id) == review_id
    assert record.status == status
    assert record.reviewer == "Igor"
    assert record.reason == "Reason"
    assert record.decided_at.utcoffset() == timedelta(0)

    # Sending a signal does not execute the persistence activity.
    response = client.get(f"/reviews/{review_id}")
    assert response.status_code == 200

    review = response.json()
    assert review["review_id"] == review_id
    assert review["findings"] == [finding]
    assert review["decision"] == "block"
    assert review["approval_status"] == "pending"
    assert client.get(f"/reviews/{review_id}/approval").status_code == 404


def test_duplicate_approve_review(aws_sg_update_plan, approval_client, approval_store):
    client, starter = approval_client
    finding = {
        "id": "public-inbound-access",
        "severity": "high",
        "resource_address": "aws_security_group.web",
        "evidence": "ingress.cidr_blocks contains 0.0.0.0/0",
        "remediation": "Restrict ingress to approved networks.",
    }

    # Create a review
    response = client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    # Approve it
    response = client.post(
        f"/reviews/{review_id}/approval",
        json={"status": "approved", "reviewer": "Igor", "reason": "Reason"},
    )
    assert response.status_code == 202

    review = response.json()
    assert review["review_id"] == review_id
    assert review["findings"] == [finding]
    assert review["decision"] == "block"
    assert review["approval_status"] == "pending"
    # Explicitly simulate the worker persisting the first signal.
    ActivityEnvironment().run(
        ApprovalActivities(approval_store).save_approval, starter.decisions[0]
    )

    # Try to approve again with different status
    response = client.post(
        f"/reviews/{review_id}/approval",
        json={"status": "rejected", "reviewer": "Igor", "reason": "Reason"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "review_not_pending"
    assert len(starter.decisions) == 1
    assert str(starter.decisions[0].review_id) == review_id
    assert starter.decisions[0].status == "approved"

    # Get the review and verify it didnt change
    response = client.get(f"/reviews/{review_id}")
    assert response.status_code == 200

    review = response.json()
    assert review["review_id"] == review_id
    assert review["findings"] == [finding]
    assert review["decision"] == "block"
    assert review["approval_status"] == "approved"


def test_approval_with_unknown_uuid(approval_client):
    client, starter = approval_client
    review_id = uuid4()
    response = client.post(
        f"/reviews/{review_id}/approval",
        json={"status": "rejected", "reviewer": "Igor", "reason": "Reason"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "review_not_found"
    assert starter.decisions == []


@pytest.mark.parametrize(
    "status, reviewer, reason, error_type, field",
    [
        ("pending", "Igor", "Reason", "literal_error", "status"),
        ("approved", "", "Reason", "value_error", "reviewer"),
        ("approved", "   ", "Reason", "value_error", "reviewer"),
        ("approved", "Igor", "", "value_error", "reason"),
        ("approved", "Igor", "   ", "value_error", "reason"),
    ],
)
def test_invalid_approval_input(
    aws_sg_update_plan, status, reviewer, reason, error_type, field, approval_client
):
    """
    1. Verifies "pending" is rejected
    2. Verifies error on empty reviewer
    3. Verifies whitespace only reviewer
    4. Verifies error on empty reason
    5. Verifies whitespace only reason
    """
    client, starter = approval_client
    response = client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]
    response = client.post(
        f"/reviews/{review_id}/approval",
        json={"status": status, "reviewer": reviewer, "reason": reason},
    )
    assert response.status_code == 422

    detail = response.json()["detail"][0]
    assert detail["type"] == error_type
    assert detail["loc"] == ["body", field]
    assert starter.decisions == []


def test_get_approval_record(aws_sg_update_plan, approval_client, approval_store):
    client, starter = approval_client
    response = client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    response = client.post(
        f"/reviews/{review_id}/approval",
        json={"status": "approved", "reviewer": "Igor", "reason": "Reason"},
    )
    assert response.status_code == 202

    review = response.json()
    assert review["review_id"] == review_id
    assert review["decision"] == "block"
    assert review["approval_status"] == "pending"
    assert client.get(f"/reviews/{review_id}/approval").status_code == 404
    ActivityEnvironment().run(
        ApprovalActivities(approval_store).save_approval, starter.decisions[0]
    )

    response = client.get(f"/reviews/{review_id}/approval")
    assert response.status_code == 200

    record = response.json()
    assert record["review_id"] == review_id
    assert record["status"] == "approved"
    assert record["reviewer"] == "Igor"
    assert record["reason"] == "Reason"

    # Verify UTC offset ia zero
    decided_at = datetime.fromisoformat(record["decided_at"])
    assert decided_at.utcoffset() == timedelta(0)

    # Get again
    response = client.get(f"/reviews/{review_id}/approval")
    assert response.status_code == 200

    record = response.json()
    assert datetime.fromisoformat(record["decided_at"]) == decided_at


def test_get_approval_record_for_pending_review(aws_sg_update_plan):
    response = client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    response = client.get(f"/reviews/{review_id}/approval")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "approval_not_found"


def test_pending_review_can_deploy_false(aws_sg_update_plan):
    """Test that a pending review returns can_deploy = False"""
    response = client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    response = client.get(f"/reviews/{review_id}")
    assert response.status_code == 200
    assert response.json()["can_deploy"] is False


def test_blocked_review_can_deploy_false(
    aws_sg_update_plan, approval_client, approval_store
):
    client, starter = approval_client
    """Test blocked review can_deploy = False even if approved by human"""
    response = client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    # Approve review
    response = client.post(
        f"/reviews/{review_id}/approval",
        json={"status": "approved", "reviewer": "Igor", "reason": "Reason"},
    )
    assert response.status_code == 202
    assert response.json()["can_deploy"] is False

    ActivityEnvironment().run(
        ApprovalActivities(approval_store).save_approval, starter.decisions[0]
    )
    response = client.get(f"/reviews/{review_id}")
    assert response.status_code == 200
    assert response.json()["approval_status"] == "approved"
    assert response.json()["can_deploy"] is False


def test_approved_review_can_deploy(create_plan, approval_client, approval_store):
    client, starter = approval_client
    """Test approved review can_deploy = True"""
    response = client.post("/reviews", json={"plan": create_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    # Get review before human approval, can_deploy should be False
    response = client.get(f"/reviews/{review_id}")
    assert response.status_code == 200
    assert response.json()["can_deploy"] is False

    # Approve review
    response = client.post(
        f"/reviews/{review_id}/approval",
        json={"status": "approved", "reviewer": "Igor", "reason": "Reason"},
    )
    assert response.status_code == 202
    assert response.json()["can_deploy"] is False

    ActivityEnvironment().run(
        ApprovalActivities(approval_store).save_approval, starter.decisions[0]
    )

    response = client.get(f"/reviews/{review_id}")
    assert response.status_code == 200
    assert response.json()["can_deploy"] is True


def test_supplied_store(create_plan):
    review_store = InMemoryReviewStore()
    custom_app = create_app(
        PolicyConfig(enabled_policy_ids=frozenset()),
        DeterministicExplanationProvider(),
        review_store=review_store,
        settings=TEST_SETTINGS,
        workflow_starter=FakeApprovalWorkflowStarter(),
    )
    custom_client = TestClient(custom_app)

    response = custom_client.post("/reviews", json={"plan": create_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    stored_review = review_store.get_review(UUID(review_id))
    assert stored_review.review_id == UUID(review_id)
    assert stored_review.plan_hash == hash_plan(create_plan)


def test_sqlite_database(tmp_path, create_plan):
    database_file = tmp_path / "reviews.db"
    review_store = SQLiteReviewStore(database_file)
    starter = FakeApprovalWorkflowStarter()
    custom_app = create_app(
        PolicyConfig(enabled_policy_ids=frozenset()),
        DeterministicExplanationProvider(),
        review_store=review_store,
        settings=TEST_SETTINGS,
        workflow_starter=starter,
    )
    custom_client = TestClient(custom_app)

    response = custom_client.post("/reviews", json={"plan": create_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    response = custom_client.post(
        f"/reviews/{review_id}/approval",
        json={"status": "approved", "reviewer": "Igor", "reason": "reason"},
    )
    assert response.status_code == 202
    assert response.json()["approval_status"] == "pending"
    ActivityEnvironment().run(
        ApprovalActivities(review_store).save_approval, starter.decisions[0]
    )
    response = custom_client.get(f"/reviews/{review_id}")
    assert response.status_code == 200
    expected_review = response.json()

    response = custom_client.get(f"/reviews/{review_id}/approval")
    assert response.status_code == 200
    expected_record = response.json()

    reopened_store = SQLiteReviewStore(database_file)
    reopened_app = create_app(
        PolicyConfig(enabled_policy_ids=frozenset()),
        DeterministicExplanationProvider(),
        review_store=reopened_store,
        settings=TEST_SETTINGS,
        workflow_starter=FakeApprovalWorkflowStarter(),
    )
    reopened_client = TestClient(reopened_app)

    reopened_record = reopened_client.get(f"/reviews/{review_id}/approval")
    assert reopened_record.status_code == 200
    assert reopened_record.json() == expected_record

    reopened_review = reopened_client.get(f"/reviews/{review_id}")
    assert reopened_review.status_code == 200
    assert reopened_review.json() == expected_review


def test_explanation_provider_failure_uses_fallback(aws_sg_update_plan, caplog):
    starter = FakeApprovalWorkflowStarter()
    store = InMemoryReviewStore()
    custom_app = create_app(
        PolicyConfig(enabled_policy_ids=frozenset({"public-inbound-access"})),
        BadExplanationProvider(),
        settings=TEST_SETTINGS,
        workflow_starter=starter,
        review_store=store,
    )
    custom_client = TestClient(custom_app)

    logger = logging.getLogger("plangate")
    logger.addHandler(caplog.handler)
    try:
        response = custom_client.post("/reviews", json={"plan": aws_sg_update_plan})
    finally:
        logger.removeHandler(caplog.handler)

    assert response.status_code == 200
    body = response.json()
    review_id = UUID(body["review_id"])
    stored_review = store.get_review(review_id)
    assert body["decision"] == stored_review.result.decision == "block"
    assert body["findings"] == [
        {
            "id": "public-inbound-access",
            "severity": "high",
            "resource_address": "aws_security_group.web",
            "evidence": "ingress.cidr_blocks contains 0.0.0.0/0",
            "remediation": "Restrict ingress to approved networks.",
        }
    ]
    assert body["explanation"] == DeterministicExplanationProvider().explain(
        stored_review.result
    )
    assert starter.inputs == [
        ApprovalWorkflowInput(review_id=str(review_id), policy_result="block")
    ]
    persisted_response = custom_client.get(f"/reviews/{review_id}")
    assert persisted_response.json()["approval_status"] == "pending"
    assert persisted_response.json()["can_deploy"] is False

    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "Review explanation failed"
    )
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.plan_hash == hash_plan(aws_sg_update_plan)
    assert record.policy_result == "block"

    assert (
        custom_app.state.metrics.registry.get_sample_value(
            "plangate_explanation_fallbacks_total",
            {"provider_name": "bad provider"},
        )
        == 1
    )

    # A completed review with a fallback still counts as successful.
    assert (
        custom_app.state.metrics.registry.get_sample_value(
            "plangate_reviews_total",
            {"policy_result": "block"},
        )
        == 1
    )

    assert (
        custom_app.state.metrics.registry.get_sample_value(
            "plangate_review_duration_seconds_count",
            {"policy_result": "block"},
        )
        == 1
    )

    assert (
        custom_app.state.metrics.registry.get_sample_value(
            "plangate_explanation_duration_seconds_count",
            {"provider_name": "bad provider"},
        )
        is None
    )
    assert (
        custom_app.state.metrics.registry.get_sample_value(
            "plangate_explanation_duration_seconds_count",
            {"provider_name": "deterministic"},
        )
        == 1
    )


def test_reviews_metric(create_plan):
    # Creating an isolated app to get isolated metrics
    test_app = create_app(
        PolicyConfig(enabled_policy_ids=frozenset()),
        FakeExplanationProvider(),
        settings=TEST_SETTINGS,
        workflow_starter=FakeApprovalWorkflowStarter(),
    )
    test_client = TestClient(test_app)

    response = test_client.post("/reviews", json={"plan": create_plan})
    assert response.status_code == 200

    assert (
        test_app.state.metrics.registry.get_sample_value(
            "plangate_reviews_total",
            {"policy_result": "approve"},
        )
        == 1
    )

    assert (
        test_app.state.metrics.registry.get_sample_value(
            "plangate_review_duration_seconds_count",
            {"policy_result": "approve"},
        )
        == 1
    )

    assert (
        test_app.state.metrics.registry.get_sample_value(
            "plangate_explanation_duration_seconds_count",
            {"provider_name": "fake provider"},
        )
        == 1
    )


def test_get_metrics(create_plan):
    test_app = create_app(
        PolicyConfig(enabled_policy_ids=frozenset()),
        FakeExplanationProvider(),
        settings=TEST_SETTINGS,
        workflow_starter=FakeApprovalWorkflowStarter(),
    )
    test_client = TestClient(test_app)

    response = test_client.post("/reviews", json={"plan": create_plan})
    assert response.status_code == 200

    metrics = test_client.get("/metrics")
    assert metrics.status_code == 200
    assert metrics.headers["content-type"] == prometheus_client.CONTENT_TYPE_LATEST
    assert 'plangate_reviews_total{policy_result="approve"} 1.0' in metrics.text


def test_approval_metric(create_plan, aws_sg_update_plan):
    # Creating an isolated app to get isolated metrics
    test_app = create_app(
        PolicyConfig(enabled_policy_ids=frozenset()),
        FakeExplanationProvider(),
        settings=TEST_SETTINGS,
        workflow_starter=FakeApprovalWorkflowStarter(),
    )
    test_client = TestClient(test_app)

    response = test_client.post("/reviews", json={"plan": create_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    approved_record = {
        "status": "approved",
        "reviewer": "Igor",
        "reason": "Reason",
    }
    response = test_client.post(f"/reviews/{review_id}/approval", json=approved_record)
    assert response.status_code == 202

    response = test_client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200
    review_id = response.json()["review_id"]

    rejected_record = {
        "status": "rejected",
        "reviewer": "Igor",
        "reason": "Reason",
    }
    response = test_client.post(f"/reviews/{review_id}/approval", json=rejected_record)
    assert response.status_code == 202

    assert (
        test_app.state.metrics.registry.get_sample_value(
            "plangate_approval_submissions_total",
            {"requested_status": "approved"},
        )
        == 1
    )

    assert (
        test_app.state.metrics.registry.get_sample_value(
            "plangate_approval_submissions_total",
            {"requested_status": "rejected"},
        )
        == 1
    )


def test_record_findings(aws_sg_update_plan):
    test_app = create_app(
        PolicyConfig(),
        FakeExplanationProvider(),
        settings=TEST_SETTINGS,
        workflow_starter=FakeApprovalWorkflowStarter(),
    )
    test_client = TestClient(test_app)
    test_client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert (
        test_app.state.metrics.registry.get_sample_value(
            "plangate_policy_findings_total",
            {"policy_id": "public-inbound-access", "severity": "high"},
        )
        == 1
    )


def test_record_findings_absent_on_workflow_error(aws_sg_update_plan):
    test_app = create_app(
        PolicyConfig(),
        FakeExplanationProvider(),
        settings=TEST_SETTINGS,
        workflow_starter=BadApprovalWorkflowStarter(),
    )
    test_client = TestClient(test_app)
    with pytest.raises(RuntimeError, match="Temporal unavailable"):
        test_client.post("/reviews", json={"plan": aws_sg_update_plan})

    assert (
        test_app.state.metrics.registry.get_sample_value(
            "plangate_policy_findings_total",
            {"policy_id": "public-inbound-access", "severity": "high"},
        )
        is None
    )


def test_workflow_starter(aws_sg_update_plan):
    starter = FakeApprovalWorkflowStarter()

    test_app = create_app(
        policy_config=PolicyConfig(),
        explanation_provider=FakeExplanationProvider(),
        workflow_starter=starter,
        settings=TEST_SETTINGS,
    )
    test_client = TestClient(test_app)
    response = test_client.post("/reviews", json={"plan": aws_sg_update_plan})
    assert response.status_code == 200

    body = response.json()

    assert starter.inputs == [
        ApprovalWorkflowInput(
            review_id=body["review_id"],
            policy_result=body["decision"],
        )
    ]


def test_bad_approval_workflow_starter(aws_sg_update_plan):
    starter = BadApprovalWorkflowStarter()

    test_app = create_app(
        policy_config=PolicyConfig(),
        explanation_provider=FakeExplanationProvider(),
        workflow_starter=starter,
        settings=TEST_SETTINGS,
    )
    test_client = TestClient(test_app)
    with pytest.raises(RuntimeError, match="Temporal unavailable"):
        test_client.post("/reviews", json={"plan": aws_sg_update_plan})

    assert (
        test_app.state.metrics.registry.get_sample_value(
            "plangate_policy_findings_total",
            {"policy_id": "public-inbound-access", "severity": "high"},
        )
        is None
    )

    assert (
        test_app.state.metrics.registry.get_sample_value(
            "plangate_reviews_total",
            {"policy_result": "block"},
        )
        is None
    )


def test_create_explanation_provider_unknown():
    settings = TEST_SETTINGS.model_copy(update={"llm_provider": "unknown"})
    with pytest.raises(ValueError, match="unknown is unsupported value"):
        create_explanation_provider(settings)


def test_explanation_provider_fallback(monkeypatch, create_plan):
    provider = OllamaExplanationProvider(TEST_SETTINGS)
    ask = Mock(side_effect=TimeoutError("Ollama unavailable"))
    monkeypatch.setattr(provider, "ask", ask)
    test_app = create_app(
        PolicyConfig(),
        provider,
        settings=TEST_SETTINGS,
        workflow_starter=FakeApprovalWorkflowStarter(),
    )
    test_client = TestClient(test_app)
    response = test_client.post("/reviews", json={"plan": create_plan})

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "approve"
    assert body["findings"] == []
    assert body["explanation"] == DeterministicExplanationProvider().explain(
        ReviewResult(decision="approve", findings=())
    )
    ask.assert_called_once()
