import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import UUID

from plangate.domain.models import ApprovalRecord, Finding, Review, ReviewResult


class SQLiteReviewStore:
    def __init__(self, database_file: Path):
        self.db_file = database_file
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize_database(self):
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    decision TEXT NOT NULL,
                    findings_json TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    approval_status TEXT NOT NULL
                )
                """
            )
            conn.commit()

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_records (
                    review_id TEXT PRIMARY KEY REFERENCES reviews(review_id),
                    status TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def save_review(self, review: Review) -> None:
        findings_json = json.dumps(
            [asdict(finding) for finding in review.result.findings]
        )
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO reviews (
                    review_id, decision, findings_json, plan_hash, approval_status
                ) 
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(review.review_id),
                    review.result.decision,
                    findings_json,
                    review.plan_hash,
                    review.approval_status,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_review(self, review_id: UUID) -> Review:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT review_id, decision, findings_json, plan_hash, approval_status
                FROM reviews
                WHERE review_id = ?
                """,
                (str(review_id),),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            raise KeyError(review_id)

        findings = tuple(Finding(**item) for item in json.loads(row[2]))
        review = Review(
            review_id=UUID(row[0]),
            result=ReviewResult(decision=row[1], findings=findings),
            plan_hash=row[3],
            approval_status=row[4],
        )

        return review

    def get_record(self, review_id: UUID) -> ApprovalRecord:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT review_id, status, reviewer, reason, decided_at
                FROM approval_records
                WHERE review_id = ?
                """,
                (str(review_id),),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            raise KeyError(review_id)

        record = ApprovalRecord(
            review_id=UUID(row[0]),
            status=row[1],
            reviewer=row[2],
            reason=row[3],
            decided_at=datetime.fromisoformat(row[4]),
        )

        return record

    def save_approval(self, record: ApprovalRecord) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT approval_status
                FROM reviews
                WHERE review_id = ?
                """,
                (str(record.review_id),),
            ).fetchone()

            if row is None:
                raise KeyError(record.review_id)
            if row[0] != "pending":
                existing_row = conn.execute(
                    """
                    SELECT review_id, status, reviewer, reason, decided_at
                    FROM approval_records
                    WHERE review_id = ?
                    """,
                    (str(record.review_id),),
                ).fetchone()

                if existing_row is None:
                    raise ValueError("Finalized review has no approval record")

                existing_record = ApprovalRecord(
                    review_id=UUID(existing_row[0]),
                    status=existing_row[1],
                    reviewer=existing_row[2],
                    reason=existing_row[3],
                    decided_at=datetime.fromisoformat(existing_row[4]),
                )

                if record == existing_record:
                    conn.commit()
                    return
                else:
                    raise ValueError("Review is not pending")

            conn.execute(
                """
                UPDATE reviews
                SET approval_status = ?
                WHERE review_id = ?
                """,
                (
                    record.status,
                    str(record.review_id),
                ),
            )
            conn.execute(
                """
                INSERT INTO approval_records (
                    review_id, status, reviewer, reason, decided_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(record.review_id),
                    record.status,
                    record.reviewer,
                    record.reason,
                    record.decided_at.isoformat(),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
