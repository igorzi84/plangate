from dataclasses import asdict
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from plangate.domain.models import ApprovalRecord, Finding, Review, ReviewResult


class PostgresReviewStore:
    def __init__(
        self, host: str, port: int, dbname: str, user: str, password: SecretStr
    ) -> None:
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password

        self._initialize_database()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=self.password.get_secret_value(),
        )

    def _initialize_database(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id UUID PRIMARY KEY,
                    decision TEXT NOT NULL,
                    findings_json JSONB NOT NULL,
                    plan_hash TEXT NOT NULL,
                    approval_status TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_records (
                    review_id UUID PRIMARY KEY REFERENCES reviews(review_id),
                    status TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    decided_at TIMESTAMPTZ NOT NULL
                )
                """
            )

    def save_review(self, review: Review) -> None:
        findings_json = Jsonb([asdict(finding) for finding in review.result.findings])
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reviews (
                    review_id, decision, findings_json, plan_hash, approval_status
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    review.review_id,
                    review.result.decision,
                    findings_json,
                    review.plan_hash,
                    review.approval_status,
                ),
            )

    def get_review(self, review_id: UUID) -> Review:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT review_id, decision, findings_json, plan_hash, approval_status
                FROM reviews
                WHERE review_id = %s
                """,
                (review_id,),
            ).fetchone()

        if row is None:
            raise KeyError(review_id)

        findings = tuple(Finding(**item) for item in row[2])
        review = Review(
            review_id=row[0],
            result=ReviewResult(decision=row[1], findings=findings),
            plan_hash=row[3],
            approval_status=row[4],
        )

        return review

    def get_record(self, review_id: UUID) -> ApprovalRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT review_id, status, reviewer, reason, decided_at
                FROM approval_records
                WHERE review_id = %s
                """,
                (review_id,),
            ).fetchone()

        if row is None:
            raise KeyError(review_id)

        record = ApprovalRecord(
            review_id=row[0],
            status=row[1],
            reviewer=row[2],
            reason=row[3],
            decided_at=row[4],
        )

        return record

    def save_approval(self, record: ApprovalRecord) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT approval_status
                FROM reviews
                WHERE review_id = %s
                FOR UPDATE
                """,
                (record.review_id,),
            ).fetchone()

            if row is None:
                raise KeyError(record.review_id)
            if row[0] != "pending":
                existing_row = conn.execute(
                    """
                    SELECT review_id, status, reviewer, reason, decided_at
                    FROM approval_records
                    WHERE review_id = %s
                    """,
                    (record.review_id,),
                ).fetchone()

                if existing_row is None:
                    raise ValueError("Finalized review has no approval record")

                existing_record = ApprovalRecord(
                    review_id=existing_row[0],
                    status=existing_row[1],
                    reviewer=existing_row[2],
                    reason=existing_row[3],
                    decided_at=existing_row[4],
                )

                if record == existing_record:
                    return
                else:
                    raise ValueError("Review is not pending")

            conn.execute(
                """
                UPDATE reviews
                SET approval_status = %s
                WHERE review_id = %s
                """,
                (
                    record.status,
                    record.review_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO approval_records (
                    review_id, status, reviewer, reason, decided_at
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    record.review_id,
                    record.status,
                    record.reviewer,
                    record.reason,
                    record.decided_at,
                ),
            )
