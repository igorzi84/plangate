from typing import Protocol
from uuid import UUID

from plangate.domain.models import ApprovalRecord, Review


class ReviewStore(Protocol):
    def get_review(self, review_id: UUID) -> Review: ...

    def save_review(self, review: Review) -> None: ...

    def get_record(self, review_id: UUID) -> ApprovalRecord: ...

    def save_approval(self, record: ApprovalRecord) -> None: ...
