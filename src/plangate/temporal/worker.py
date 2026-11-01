import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from plangate.config import load_app_settings
from plangate.logging_config import configure_logging
from plangate.stores.factory import create_review_store
from plangate.temporal.activities.approval import ApprovalActivities
from plangate.temporal.workflows.approval import ApprovalWorkflow

logger = logging.getLogger("plangate.worker")


async def main():
    configure_logging()
    settings = load_app_settings()

    client = await Client.connect(
        settings.temporal_address, data_converter=pydantic_data_converter
    )

    review_store = create_review_store(settings)
    approval_activities = ApprovalActivities(review_store)

    with ThreadPoolExecutor(max_workers=4) as executor:
        worker = Worker(
            client,
            task_queue="plangate-approval",
            workflows=[ApprovalWorkflow],
            activities=[approval_activities.save_approval],
            activity_executor=executor,
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "annotated_types"
                ),
            ),
        )
        logger.info("Starting Temporal Worker")
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
