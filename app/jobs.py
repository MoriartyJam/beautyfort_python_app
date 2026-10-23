import logging
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import SyncRun


logger = logging.getLogger(__name__)
LOCK_ID = 72459001


def enqueue(db: Session, shop: str, action: str) -> SyncRun:
    if action not in {"preview", "sync", "apply", "scheduled"}:
        raise ValueError("Unsupported synchronization action")
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": LOCK_ID})
    active = db.scalar(
        select(SyncRun.id).where(SyncRun.status.in_(("queued", "running"))).limit(1)
    )
    if active:
        raise RuntimeError("Another synchronization is already queued or running")
    run = SyncRun(shop_domain=shop, mode=action, status="queued")
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def enqueue_apply(db: Session, run_id: int, shop: str) -> SyncRun:
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": LOCK_ID})
    active = db.scalar(
        select(SyncRun.id).where(SyncRun.status.in_(("queued", "running"))).limit(1)
    )
    if active:
        raise RuntimeError("Another synchronization is already queued or running")
    run = db.get(SyncRun, run_id)
    if not run or run.shop_domain != shop or run.status != "preview_ready":
        raise RuntimeError("This preview cannot be applied")
    run.mode = "apply"
    run.status = "queued"
    db.commit()
    db.refresh(run)
    return run


def claim_next(db: Session) -> SyncRun | None:
    """Atomically claim one job. PostgreSQL advisory lock serializes workers."""
    if db.bind and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": LOCK_ID})

    if db.scalar(select(SyncRun.id).where(SyncRun.status == "running").limit(1)):
        db.rollback()
        return None

    statement = select(SyncRun).where(SyncRun.status == "queued").order_by(SyncRun.id).limit(1)
    if db.bind and db.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    run = db.scalar(statement)
    if not run:
        db.rollback()
        return None
    run.status = "running"
    run.started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run


def fail_run(db: Session, run_id: int, exc: Exception) -> None:
    db.rollback()
    run = db.get(SyncRun, run_id)
    if not run:
        return
    run.status = "failed"
    run.error_count += 1
    run.error_message = str(exc)[:4000]
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    logger.exception("Synchronization job %s failed", run_id)


def recover_interrupted(db: Session) -> int:
    """Close jobs left running when a previous worker was terminated."""
    runs = list(db.scalars(select(SyncRun).where(SyncRun.status == "running")).all())
    for run in runs:
        run.status = "failed"
        run.error_count += 1
        run.error_message = "Worker stopped before this synchronization completed. Review partial results before retrying."
        run.completed_at = datetime.now(timezone.utc)
    if runs:
        db.commit()
        logger.warning("Marked %s interrupted synchronization(s) as failed", len(runs))
    return len(runs)
