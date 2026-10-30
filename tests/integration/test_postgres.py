from uuid import uuid4

import psycopg
import pytest

from plangate.config import load_app_settings
from plangate.domain.models import ApprovalRecord, Finding, Review, ReviewResult
from plangate.stores.postgres import PostgresReviewStore


@pytest.fixture()
def postgres_store(start_postgres) -> PostgresReviewStore:
    settings = load_app_settings()
    assert settings.postgres_password is not None

    return PostgresReviewStore(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname="plangate_test",
        user=settings.postgres_user,
        password=settings.postgres_password,
    )


def test_review_round_trip(postgres_store):
    review_id = uuid4()
    finding = Finding(
        id="public-inbound-access",
        severity="high",
        resource_address="aws_security_group.web",
        evidence="ingress.cidr_blocks contains 0.0.0.0/0",
        remediation="Restrict ingress to approved networks.",
    )
    review = Review(
        review_id=review_id,
        result=ReviewResult(
            decision="block",
            findings=(finding,),
        ),
        approval_status="pending",
        plan_hash="hash",
    )
    postgres_store.save_review(review)
    assert postgres_store.get_review(review_id) == review


def test_unsaved_review(postgres_store):
    review_id = uuid4()
    with pytest.raises(KeyError):
        postgres_store.get_review(review_id)


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_save_approval(postgres_store, status):
    review_id = uuid4()
    finding = Finding(
        id="public-inbound-access",
        severity="high",
        resource_address="aws_security_group.web",
        evidence="ingress.cidr_blocks contains 0.0.0.0/0",
        remediation="Restrict ingress to approved networks.",
    )
    review = Review(
        review_id=review_id,
        result=ReviewResult(
            decision="block",
            findings=(finding,),
        ),
        approval_status="pending",
        plan_hash="hash",
    )
    postgres_store.save_review(review)

    record = ApprovalRecord(
        review_id=review_id,
        status=status,
        reviewer="Igor",
        reason="Reason",
    )
    postgres_store.save_approval(record)

    assert postgres_store.get_record(review_id) == record
    assert postgres_store.get_review(review_id).approval_status == record.status


def test_save_twice_same_record(postgres_store):
    review_id = uuid4()
    finding = Finding(
        id="public-inbound-access",
        severity="high",
        resource_address="aws_security_group.web",
        evidence="ingress.cidr_blocks contains 0.0.0.0/0",
        remediation="Restrict ingress to approved networks.",
    )
    review = Review(
        review_id=review_id,
        result=ReviewResult(
            decision="block",
            findings=(finding,),
        ),
        approval_status="pending",
        plan_hash="hash",
    )
    postgres_store.save_review(review)
    record = ApprovalRecord(
        review_id=review_id,
        status="approved",
        reviewer="Igor",
        reason="Reason",
    )
    postgres_store.save_approval(record)
    postgres_store.save_approval(record)
    assert postgres_store.get_record(review_id) == record
    assert postgres_store.get_review(review_id).approval_status == record.status


def test_conflicting_approval_preserves_original(postgres_store):
    review_id = uuid4()
    finding = Finding(
        id="public-inbound-access",
        severity="high",
        resource_address="aws_security_group.web",
        evidence="ingress.cidr_blocks contains 0.0.0.0/0",
        remediation="Restrict ingress to approved networks.",
    )
    review = Review(
        review_id=review_id,
        result=ReviewResult(
            decision="block",
            findings=(finding,),
        ),
        approval_status="pending",
        plan_hash="hash",
    )
    postgres_store.save_review(review)
    original_record = ApprovalRecord(
        review_id=review_id,
        status="approved",
        reviewer="Igor",
        reason="Reason",
    )
    postgres_store.save_approval(original_record)

    conflicting_record = ApprovalRecord(
        review_id=review_id,
        status="rejected",
        reviewer="Igor",
        reason="Reason",
    )

    with pytest.raises(ValueError):
        postgres_store.save_approval(conflicting_record)

    assert postgres_store.get_record(review_id) == original_record
    assert postgres_store.get_review(review_id).approval_status == "approved"


def test_rollback(postgres_store):
    review_id = uuid4()
    finding = Finding(
        id="public-inbound-access",
        severity="high",
        resource_address="aws_security_group.web",
        evidence="ingress.cidr_blocks contains 0.0.0.0/0",
        remediation="Restrict ingress to approved networks.",
    )
    review = Review(
        review_id=review_id,
        result=ReviewResult(
            decision="block",
            findings=(finding,),
        ),
        approval_status="pending",
        plan_hash="hash",
    )
    postgres_store.save_review(review)

    record = ApprovalRecord(
        review_id=review_id,
        status="approved",
        reviewer="Igor",
        reason="Reason",
    )

    # Simulate failure
    with postgres_store._connect() as conn:
        conn.execute("""
            ALTER TABLE approval_records
            ADD CONSTRAINT reject_test_insert CHECK (false) NOT VALID
        """)

    try:
        with pytest.raises(psycopg.errors.CheckViolation) as exc:
            postgres_store.save_approval(record)

        assert exc.value.diag.constraint_name == "reject_test_insert"
        assert postgres_store.get_review(review_id).approval_status == "pending"

        with pytest.raises(KeyError):
            postgres_store.get_record(review_id)
    finally:
        with postgres_store._connect() as conn:
            conn.execute("""
                ALTER TABLE approval_records
                DROP CONSTRAINT reject_test_insert
            """)
