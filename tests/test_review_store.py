from uuid import uuid4

import pytest

from plangate.domain.models import ApprovalRecord, Review, ReviewResult
from plangate.stores.memory import InMemoryReviewStore


@pytest.fixture(scope="function")
def pending_review_store():
    store = InMemoryReviewStore()
    review_id = uuid4()
    review = Review(
        review_id=review_id,
        result=ReviewResult(decision="block", findings=()),
        plan_hash="hash",
    )
    store.save_review(review)
    return store, review


def test_save_and_retrieve_review(pending_review_store):
    store, review = pending_review_store

    retrieved_review = store.get_review(review.review_id)
    assert retrieved_review == review


def test_stores_isolation(pending_review_store):
    _, review = pending_review_store
    second_store = InMemoryReviewStore()

    with pytest.raises(KeyError):
        second_store.get_review(review.review_id)


def test_save_approval(pending_review_store):
    store, review = pending_review_store

    record = ApprovalRecord(
        review_id=review.review_id,
        status="approved",
        reviewer="Igor",
        reason="Some reason",
    )
    store.save_approval(record)
    stored_review = store.get_review(review.review_id)
    assert stored_review.approval_status == "approved"
    assert stored_review.result == review.result
    assert stored_review.plan_hash == review.plan_hash

    stored_record = store.get_record(review.review_id)
    assert stored_record == record


def test_second_approval_raises_error(pending_review_store):
    store, review = pending_review_store

    record = ApprovalRecord(
        review_id=review.review_id,
        status="approved",
        reviewer="Igor",
        reason="Some reason",
    )
    store.save_approval(record)
    stored_review = store.get_review(review.review_id)
    assert stored_review.approval_status == "approved"
    assert stored_review.result == review.result

    modified_record = ApprovalRecord(
        review_id=review.review_id,
        status="rejected",
        reviewer="Igor",
        reason="Some reason",
    )
    with pytest.raises(ValueError, match="Review is not pending"):
        store.save_approval(modified_record)

    assert store.get_review(review.review_id) == stored_review
    assert store.get_record(review.review_id) == record


def test_rejected_record(pending_review_store):
    store, review = pending_review_store

    record = ApprovalRecord(
        review_id=review.review_id,
        status="rejected",
        reviewer="Igor",
        reason="Some reason",
    )
    store.save_approval(record)
    stored_review = store.get_review(review.review_id)
    assert stored_review.approval_status == "rejected"
    assert stored_review.result == review.result
    assert stored_review.plan_hash == review.plan_hash

    stored_record = store.get_record(review.review_id)
    assert stored_record == record


def test_approval_unknown_review():
    store = InMemoryReviewStore()
    review_id = uuid4()

    record = ApprovalRecord(
        review_id=review_id, status="rejected", reviewer="Igor", reason="Some reason"
    )
    with pytest.raises(KeyError):
        store.save_approval(record)

    with pytest.raises(KeyError):
        store.get_record(review_id)
