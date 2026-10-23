import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .config import get_settings
from .database import Base, SessionLocal, engine
from .defaults import get_app_settings, seed_defaults
from .models import ShopConnection
from .models import SyncRun
from .jobs import claim_next, enqueue, fail_run, recover_interrupted
from .security import decrypt_token
from .shopify import ShopifyClient
from .sync_service import apply_preview, create_preview


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def token_for(config, db, shop: str) -> str:
    connection = db.scalar(select(ShopConnection).where(ShopConnection.shop_domain == shop))
    if connection:
        return decrypt_token(config, connection.encrypted_access_token)
    if config.shopify_access_token:
        return config.shopify_access_token
    return await ShopifyClient.client_credentials_token(config, shop)


async def process_next(config) -> bool:
    with SessionLocal() as db:
        run = claim_next(db)
        if not run:
            return False
        run_id, shop, action = run.id, run.shop_domain, run.mode
        logger.info("Claimed synchronization run=%s mode=%s shop=%s", run_id, action, shop)
        try:
            token = await token_for(config, db, shop)
            if action == "apply":
                await apply_preview(db, config, run_id, shop, token)
            else:
                preview = await create_preview(db, config, shop, token, run_id=run_id)
                if action in {"sync", "scheduled"}:
                    await apply_preview(db, config, preview.id, shop, token)
            logger.info("Finished synchronization run=%s", run_id)
            return True
        except Exception as exc:
            logger.exception("Synchronization run=%s failed: %s", run_id, exc)
            fail_run(db, run_id, exc)
            return True


def enqueue_scheduled_if_due(config) -> None:
    with SessionLocal() as db:
        if not config.automatic_apply:
            return
        latest = db.scalar(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1))
        due_before = datetime.now(timezone.utc) - timedelta(minutes=config.sync_interval_minutes)
        if latest and latest.started_at.replace(tzinfo=latest.started_at.tzinfo or timezone.utc) > due_before:
            return
        try:
            connections = list(db.scalars(select(ShopConnection)).all())
            shops = [row.shop_domain for row in connections] or ([config.shopify_shop] if config.shopify_shop else [])
            if shops:
                enqueue(db, shops[0], "scheduled")
        except RuntimeError:
            pass


async def run_forever() -> None:
    config = get_settings()
    logger.info(
        "Beautyfort worker starting poll_seconds=%s automatic_apply=%s",
        config.worker_poll_seconds,
        config.automatic_apply,
    )
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_defaults(db)
        recovered = recover_interrupted(db)
    logger.info("Beautyfort worker ready recovered_runs=%s", recovered)
    while True:
        enqueue_scheduled_if_due(config)
        processed = await process_next(config)
        if not processed:
            await asyncio.sleep(config.worker_poll_seconds)


if __name__ == "__main__":
    asyncio.run(run_forever())
