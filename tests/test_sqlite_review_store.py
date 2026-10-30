import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from plangate.domain.models import ApprovalRecord, Finding, Review, ReviewResult
from plangate.stores.sqlite import SQLiteReviewStore


@pytest.fixture
def database_file(tmp_path) -> Path:
    return tmp_path / "review.db"


def test_sqlite_constructor(database_file):
    store = SQLiteReviewStore(database_file)
    assert store.db_file == database_file
    assert database_file.exists()

    second_store = SQLiteReviewStore(database_file)
    assert second_store.db_file == database_file


def test_save_review(database_file):
    store = SQLiteReviewStore(database_file)

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
        approval_status="approved",
        plan_hash="hash",
    )
    store.save_review(review)

    second_store = SQLiteReviewStore(database_file)
    retrieved_review = second_store.get_review(review_id)

    assert retrieved_review == review


def test_get_unknown_review_id(database_file):
    review_id = uuid4()
    store = SQLiteReviewStore(database_file)
    with pytest.raises(KeyError):
        store.get_review(review_id)


def test_get_unknown_approval_record(database_file):
    review_id = uuid4()
    store = SQLiteReviewStore(database_file)
    with pytest.raises(KeyError):
        store.get_record(review_id)


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_save_approval(database_file, status):
    review_id = uuid4()
    store = SQLiteReviewStore(database_file)

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
    store.save_review(review)
    record = ApprovalRecord(
        review_id=review_id,
        status=status,
        reviewer="Igor",
        reason="Reason",
    )
    store.save_approval(record)

    second_store = SQLiteReviewStore(database_file)
    second_record = second_store.get_record(review_id)
    assert second_record == record

    second_review = second_store.get_review(review_id)
    assert second_review.approval_status == status
    assert second_review.result == review.result
    assert second_review.plan_hash == review.plan_hash


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_duplicate_approval(database_file, status):
    review_id = uuid4()
    store = SQLiteReviewStore(database_file)

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
    store.save_review(review)
    original_record = ApprovalRecord(
        review_id=review_id,
        status=status,
        reviewer="Igor",
        reason="Reason",
    )
    store.save_approval(original_record)
    review_after_first_decision = store.get_review(review_id)

    updated_status = "approved"
    if status == "approved":
        updated_status = "rejected"

    attempted_record = ApprovalRecord(
        review_id=review_id,
        status=updated_status,
        reviewer="Igor",
        reason="Second Reason",
    )
    with pytest.raises(ValueError):
        store.save_approval(attempted_record)

    reopened_store = SQLiteReviewStore(database_file)
    persisted_record = reopened_store.get_record(review_id)
    assert persisted_record == original_record

    persisted_review = reopened_store.get_review(review_id)
    assert persisted_review == review_after_first_decision


def test_unknown_review(database_file):
    review_id = uuid4()
    store = SQLiteReviewStore(database_file)
    record = ApprovalRecord(
        review_id=review_id,
        status="approved",
        reviewer="Igor",
        reason="Reason",
    )
    with pytest.raises(KeyError):
        store.save_approval(record)

    with pytest.raises(KeyError):
        store.get_record(record.review_id)


def test_rollback(database_file):
    review_id = uuid4()
    store = SQLiteReviewStore(database_file)

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
    store.save_review(review)

    # After saving a review, creating an SQL trigger to reject the INSERT
    conn = sqlite3.connect(database_file)
    try:
        conn.execute(
            """
            CREATE TRIGGER reject_approval_insert
            BEFORE INSERT ON approval_records
            BEGIN
                SELECT RAISE(ABORT, 'forced audit failure');
            END;
        """
        )
    finally:
        conn.close()

    record = ApprovalRecord(
        review_id=review_id,
        status="approved",
        reviewer="Igor",
        reason="Reason",
    )
    # Approval should raise an error
    with pytest.raises(sqlite3.IntegrityError, match="forced audit failure"):
        store.save_approval(record)

    reopened_store = SQLiteReviewStore(database_file)
    assert reopened_store.get_review(review.review_id) == review

    # Approval record shouldnt exist (rollback)
    with pytest.raises(KeyError):
        reopened_store.get_record(review.review_id)


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_identical_approval_retry(database_file, status):
    review_id = uuid4()
    store = SQLiteReviewStore(database_file)

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
    store.save_review(review)
    record = ApprovalRecord(
        review_id=review_id,
        status=status,
        reviewer="Igor",
        reason="Reason",
    )
    store.save_approval(record)

    # Trying to save the same approval record again
    review_after_first_save = store.get_review(review_id)
    store.save_approval(record)

    reopened_store = SQLiteReviewStore(database_file)
    assert reopened_store.get_record(review_id) == record
    assert reopened_store.get_review(review_id) == review_after_first_save
