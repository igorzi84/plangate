from uuid import UUID

from plangate.domain.models import ApprovalRecord, Review


class InMemoryReviewStore:
    def __init__(self) -> None:
        self.reviews: dict[UUID, Review] = {}
        self.records: dict[UUID, ApprovalRecord] = {}

    def get_review(self, review_id: UUID) -> Review:
        return self.reviews[review_id]

    def save_review(self, review: Review) -> None:
        self.reviews[review.review_id] = review

    def get_record(self, review_id: UUID) -> ApprovalRecord:
        return self.records[review_id]

    def _save_record(self, record: ApprovalRecord) -> None:
        self.records[record.review_id] = record

    def save_approval(self, record: ApprovalRecord) -> None:
        review = self.get_review(record.review_id)

        if review.approval_status != "pending":
            raise ValueError("Review is not pending.")

        updated_review = Review(
            review_id=record.review_id,
            result=review.result,
            approval_status=record.status,
            plan_hash=review.plan_hash,
        )
        self._save_record(record)
        self.save_review(updated_review)
