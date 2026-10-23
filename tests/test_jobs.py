from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.jobs import claim_next, enqueue, enqueue_apply, recover_interrupted
from app.models import SyncRun


def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_job_is_queued_and_claimed():
    db = session()
    queued = enqueue(db, "example.myshopify.com", "sync")
    assert queued.status == "queued"

    claimed = claim_next(db)
    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.status == "running"


def test_second_job_is_rejected_while_active():
    db = session()
    enqueue(db, "example.myshopify.com", "sync")
    try:
        enqueue(db, "example.myshopify.com", "preview")
    except RuntimeError as exc:
        assert "already" in str(exc)
    else:
        raise AssertionError("A second active job was accepted")


def test_interrupted_job_is_closed_on_worker_start():
    db = session()
    run = claim_next_after_enqueue(db)
    assert recover_interrupted(db) == 1
    db.refresh(run)
    assert run.status == "failed"
    assert "Worker stopped" in (run.error_message or "")


def test_completed_preview_can_be_queued_for_apply():
    db = session()
    preview = SyncRun(shop_domain="example.myshopify.com", mode="preview", status="preview_ready")
    db.add(preview)
    db.commit()
    db.refresh(preview)

    queued = enqueue_apply(db, preview.id, preview.shop_domain)
    assert queued.mode == "apply"
    assert queued.status == "queued"


def claim_next_after_enqueue(db):
    enqueue(db, "example.myshopify.com", "sync")
    claimed = claim_next(db)
    assert claimed is not None
    return claimed
