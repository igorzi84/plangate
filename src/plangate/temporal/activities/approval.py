import logging

from temporalio import activity

from plangate.domain.models import ApprovalRecord
from plangate.stores.base import ReviewStore

logger = logging.getLogger(__name__)


class ApprovalActivities:
    def __init__(self, store: ReviewStore) -> None:
        self.store = store

    @activity.defn(name="save_approval")
    def save_approval(self, approval_record: ApprovalRecord) -> None:
        workflow_id = activity.info().workflow_id
        self.store.save_approval(approval_record)
        logger.info(
            "Human decision persisted",
            extra={
                "review_id": str(approval_record.review_id),
                "approval_outcome": approval_record.status,
                "workflow_id": workflow_id,
            },
        )
