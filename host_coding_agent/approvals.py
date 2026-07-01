from __future__ import annotations

import sqlite3
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ApprovalError(ValueError):
    pass


class ApprovalStore:
    """State store used only by the external approval and patch-apply boundary."""

    def __init__(self, path: Path):
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE
                        REFERENCES proposals(proposal_id),
                    profile TEXT NOT NULL,
                    proposal_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'approved', 'rejected', 'expired',
                            'applying', 'applied', 'failed'
                        )),
                    requested_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT,
                    decision_channel TEXT,
                    applying_at TEXT,
                    completed_at TEXT,
                    failure_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS approvals_profile_status
                    ON approvals(profile, status, requested_at DESC);
                CREATE TABLE IF NOT EXISTS approval_events (
                    event_id TEXT PRIMARY KEY,
                    approval_id TEXT NOT NULL REFERENCES approvals(approval_id),
                    status TEXT NOT NULL,
                    actor TEXT,
                    channel TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS approval_events_approval_created
                    ON approval_events(approval_id, created_at);
                CREATE TRIGGER IF NOT EXISTS approval_events_no_update
                    BEFORE UPDATE ON approval_events
                    BEGIN
                        SELECT RAISE(ABORT, 'approval events are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS approval_events_no_delete
                    BEFORE DELETE ON approval_events
                    BEGIN
                        SELECT RAISE(ABORT, 'approval events are immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS approvals_identity_immutable
                    BEFORE UPDATE ON approvals
                    WHEN NEW.approval_id IS NOT OLD.approval_id
                      OR NEW.proposal_id IS NOT OLD.proposal_id
                      OR NEW.profile IS NOT OLD.profile
                      OR NEW.proposal_sha256 IS NOT OLD.proposal_sha256
                      OR NEW.requested_at IS NOT OLD.requested_at
                      OR NEW.expires_at IS NOT OLD.expires_at
                    BEGIN
                        SELECT RAISE(ABORT, 'approval identity is immutable');
                    END;
                CREATE TRIGGER IF NOT EXISTS approvals_valid_transition
                    BEFORE UPDATE OF status ON approvals
                    WHEN NOT (
                        (OLD.status = 'pending' AND NEW.status IN (
                            'approved', 'rejected', 'expired'
                        ))
                        OR (OLD.status = 'approved' AND NEW.status IN (
                            'applying', 'expired'
                        ))
                        OR (OLD.status = 'applying' AND NEW.status IN (
                            'applied', 'failed'
                        ))
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid approval status transition');
                    END;
                CREATE TRIGGER IF NOT EXISTS approvals_no_delete
                    BEFORE DELETE ON approvals
                    BEGIN
                        SELECT RAISE(ABORT, 'approval records cannot be deleted');
                    END;
                """
            )

    def create_pending(self, proposal: dict[str, Any]) -> dict[str, Any]:
        record = {
            "approval_id": uuid.uuid4().hex,
            "proposal_id": proposal["proposal_id"],
            "profile": proposal["profile"],
            "proposal_sha256": proposal["diff_sha256"],
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": proposal["expires_at"],
        }
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO approvals (
                        approval_id, proposal_id, profile, proposal_sha256,
                        requested_at, expires_at
                    ) VALUES (
                        :approval_id, :proposal_id, :profile, :proposal_sha256,
                        :requested_at, :expires_at
                    )
                    """,
                    record,
                )
                self._insert_event(
                    connection,
                    approval_id=record["approval_id"],
                    status="pending",
                )
        except sqlite3.IntegrityError as exc:
            raise ApprovalError("approval request already exists or is invalid") from exc
        return self.get(record["approval_id"], profile=proposal["profile"])

    def get(self, approval_id: str, *, profile: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ? AND profile = ?",
                (approval_id, profile),
            ).fetchone()
        if row is None:
            raise ApprovalError("approval request not found")
        return dict(row)

    def get_for_proposal(self, proposal_id: str, *, profile: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE proposal_id = ? AND profile = ?",
                (proposal_id, profile),
            ).fetchone()
        if row is None:
            raise ApprovalError("approval request not found")
        return dict(row)

    def list(self, *, profile: str, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100))
        parameters: list[Any] = [profile]
        where = "profile = ?"
        if status is not None:
            where += " AND status = ?"
            parameters.append(status)
        parameters.append(safe_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM approvals
                WHERE {where}
                ORDER BY requested_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def events(self, *, approval_id: str, profile: str) -> list[dict[str, Any]]:
        self.get(approval_id, profile=profile)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, approval_id, status, actor, channel,
                       details_json, created_at
                FROM approval_events
                WHERE approval_id = ?
                ORDER BY created_at, event_id
                """,
                (approval_id,),
            ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["details"] = json.loads(record.pop("details_json"))
            records.append(record)
        return records

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        approval_id: str,
        status: str,
        actor: str | None = None,
        channel: str | None = None,
        details: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO approval_events (
                event_id, approval_id, status, actor, channel,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                approval_id,
                status,
                actor,
                channel,
                json.dumps(details or {}, sort_keys=True),
                created_at or datetime.now(timezone.utc).isoformat(),
            ),
        )

    def decide(
        self,
        *,
        proposal_id: str,
        profile: str,
        proposal_sha256: str,
        approved: bool,
        decided_by: str,
        decision_channel: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        decision_time = now or datetime.now(timezone.utc)
        decision_iso = decision_time.isoformat()
        target_status = "approved" if approved else "rejected"
        expired = False
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT approval_id, expires_at
                FROM approvals
                WHERE proposal_id = ? AND profile = ?
                  AND proposal_sha256 = ? AND status = 'pending'
                """,
                (proposal_id, profile, proposal_sha256),
            ).fetchone()
            if row is None:
                raise ApprovalError("pending approval request not found")
            if datetime.fromisoformat(row["expires_at"]) <= decision_time:
                connection.execute(
                    """
                    UPDATE approvals
                    SET status = 'expired', decided_at = ?
                    WHERE approval_id = ? AND status = 'pending'
                    """,
                    (decision_iso, row["approval_id"]),
                )
                self._insert_event(
                    connection,
                    approval_id=row["approval_id"],
                    status="expired",
                    created_at=decision_iso,
                )
                expired = True
            else:
                cursor = connection.execute(
                    """
                    UPDATE approvals
                    SET status = ?, decided_at = ?, decided_by = ?,
                        decision_channel = ?
                    WHERE approval_id = ? AND status = 'pending'
                    """,
                    (
                        target_status,
                        decision_iso,
                        decided_by,
                        decision_channel,
                        row["approval_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ApprovalError("approval decision lost a concurrent race")
                self._insert_event(
                    connection,
                    approval_id=row["approval_id"],
                    status=target_status,
                    actor=decided_by,
                    channel=decision_channel,
                    created_at=decision_iso,
                )
        if expired:
            raise ApprovalError("approval request has expired")
        return self.get(row["approval_id"], profile=profile)

    def claim_for_apply(
        self,
        *,
        proposal_id: str,
        profile: str,
        proposal_sha256: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        claim_time = now or datetime.now(timezone.utc)
        claim_iso = claim_time.isoformat()
        expired = False
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT approval_id, expires_at
                FROM approvals
                WHERE proposal_id = ? AND profile = ?
                  AND proposal_sha256 = ? AND status = 'approved'
                """,
                (proposal_id, profile, proposal_sha256),
            ).fetchone()
            if row is None:
                raise ApprovalError("approved request not found")
            if datetime.fromisoformat(row["expires_at"]) <= claim_time:
                connection.execute(
                    """
                    UPDATE approvals
                    SET status = 'expired', completed_at = ?
                    WHERE approval_id = ? AND status = 'approved'
                    """,
                    (claim_iso, row["approval_id"]),
                )
                self._insert_event(
                    connection,
                    approval_id=row["approval_id"],
                    status="expired",
                    created_at=claim_iso,
                )
                expired = True
            else:
                cursor = connection.execute(
                    """
                    UPDATE approvals
                    SET status = 'applying', applying_at = ?
                    WHERE approval_id = ? AND status = 'approved'
                    """,
                    (claim_iso, row["approval_id"]),
                )
                if cursor.rowcount != 1:
                    raise ApprovalError("approval apply claim lost a concurrent race")
                self._insert_event(
                    connection,
                    approval_id=row["approval_id"],
                    status="applying",
                    created_at=claim_iso,
                )
        if expired:
            raise ApprovalError("approval request has expired")
        return self.get(row["approval_id"], profile=profile)

    def complete_apply(
        self,
        *,
        approval_id: str,
        profile: str,
        success: bool,
        failure_reason: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        completed_at = (now or datetime.now(timezone.utc)).isoformat()
        status = "applied" if success else "failed"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE approvals
                SET status = ?, completed_at = ?, failure_reason = ?
                WHERE approval_id = ? AND profile = ? AND status = 'applying'
                """,
                (
                    status,
                    completed_at,
                    None if success else (failure_reason or "patch apply failed")[:1000],
                    approval_id,
                    profile,
                ),
            )
            if cursor.rowcount != 1:
                raise ApprovalError("applying approval request not found")
            self._insert_event(
                connection,
                approval_id=approval_id,
                status=status,
                details={"failure_reason": failure_reason} if not success else {},
                created_at=completed_at,
            )
        return self.get(approval_id, profile=profile)
