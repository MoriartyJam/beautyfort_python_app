import json
import csv
import io
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import Base, SessionLocal, engine, get_db
from .defaults import get_app_settings, seed_defaults
from .models import AppSetting, PricingRule, ShopConnection, SyncItem, SyncRun
from .jobs import enqueue, enqueue_apply
from .security import decrypt_token, encrypt_token, sign_value, unsign_value, valid_shop_domain, verify_shopify_hmac
from .shopify import ShopifyClient


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# Uvicorn owns the production logging configuration. Using its error logger
# keeps application audit messages visible in Seenode Runtime Logs.
logger = logging.getLogger("uvicorn.error")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    config = get_settings()
    logger.info("Starting application initialization")
    if config.is_production:
        if config.app_secret == "change-me-in-production" or len(config.app_secret) < 32:
            raise RuntimeError("APP_SECRET must be a random value of at least 32 characters")
        if not config.token_encryption_key:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY is required in production")
        if config.database_url.startswith("sqlite"):
            raise RuntimeError("A PostgreSQL DATABASE_URL is required in production")
        if not config.app_url.startswith("https://"):
            raise RuntimeError("APP_URL must use HTTPS in production")
        if not config.shopify_client_id or not config.shopify_client_secret:
            raise RuntimeError("SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET are required in production")
        if not config.beautyfort_username or not config.beautyfort_secret:
            raise RuntimeError("BEAUTYFORT_USERNAME and BEAUTYFORT_SECRET are required in production")
    logger.info("Connecting to the application database")
    try:
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            seed_defaults(db)
    except Exception:
        logger.exception("Database initialization failed")
        raise
    logger.info("Database initialization completed")
    yield


app = FastAPI(title="Beautyfort Sync", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def connection_for(db: Session, shop: str) -> ShopConnection | None:
    return db.scalar(select(ShopConnection).where(ShopConnection.shop_domain == shop))


def current_shop(request: Request, db: Session, settings: Settings) -> str | None:
    cookie = request.cookies.get("bf_session", "")
    payload = unsign_value(settings, cookie, max_age=60 * 60 * 24 * 30) if cookie else None
    if payload and payload.get("shop") and (
        connection_for(db, payload["shop"])
        or settings.shopify_access_token
        or (settings.shopify_client_id and settings.shopify_client_secret)
    ):
        return payload["shop"]
    return None


def credentials(request: Request, db: Session, settings: Settings) -> tuple[str, str]:
    shop = current_shop(request, db, settings)
    if not shop:
        raise HTTPException(status_code=401, detail="Authentication required")
    connection = connection_for(db, shop)
    if connection:
        return shop, decrypt_token(settings, connection.encrypted_access_token)
    if settings.shopify_access_token and shop == settings.shopify_shop:
        return shop, settings.shopify_access_token
    if settings.shopify_client_id and settings.shopify_client_secret and shop == settings.shopify_shop:
        return shop, ""
    raise HTTPException(status_code=401, detail="Shopify access token is unavailable")


def csrf_token(settings: Settings, shop: str) -> str:
    return sign_value(settings, {"shop": shop, "purpose": "csrf"})


def verify_csrf(settings: Settings, shop: str, token: str) -> None:
    payload = unsign_value(settings, token, max_age=60 * 60 * 8)
    if not payload or payload.get("shop") != shop or payload.get("purpose") != "csrf":
        raise HTTPException(403, "Invalid or expired form token")


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    if current_shop(request, db, settings):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"settings": settings})


@app.post("/login")
def login(
    request: Request,
    password: str = Form(""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not settings.admin_password or not secrets.compare_digest(password, settings.admin_password):
        return templates.TemplateResponse(
            request=request, name="login.html", context={"settings": settings, "error": "Invalid password"}, status_code=401
        )
    if not settings.shopify_shop or not (
        settings.shopify_access_token or (settings.shopify_client_id and settings.shopify_client_secret)
    ):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"settings": settings, "error": "Shopify shop and credentials are not configured"},
            status_code=503,
        )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        "bf_session", sign_value(settings, {"shop": settings.shopify_shop}), httponly=True,
        secure=settings.app_url.startswith("https://"), samesite="lax", max_age=60 * 60 * 24 * 30,
    )
    return response


@app.get("/install")
def install(shop: str, settings: Settings = Depends(get_settings)):
    shop = shop.strip().lower()
    if not valid_shop_domain(shop):
        raise HTTPException(400, "Invalid Shopify shop domain")
    if not settings.shopify_client_id or not settings.shopify_client_secret:
        raise HTTPException(503, "Shopify OAuth is not configured")
    state = sign_value(settings, {"shop": shop, "nonce": secrets.token_urlsafe(16)})
    query = urlencode(
        {
            "client_id": settings.shopify_client_id,
            "scope": settings.shopify_scopes,
            "redirect_uri": settings.oauth_redirect_uri,
            "state": state,
        }
    )
    return RedirectResponse(f"https://{shop}/admin/oauth/authorize?{query}")


@app.get("/auth/callback")
async def oauth_callback(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    params = dict(request.query_params)
    shop = params.get("shop", "").lower()
    state = unsign_value(settings, params.get("state", ""), max_age=600)
    if not valid_shop_domain(shop) or not state or state.get("shop") != shop:
        raise HTTPException(400, "Invalid OAuth state or shop")
    if not verify_shopify_hmac(params, settings.shopify_client_secret):
        raise HTTPException(400, "Invalid Shopify HMAC")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": settings.shopify_client_id,
                "client_secret": settings.shopify_client_secret,
                "code": params.get("code"),
            },
        )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise HTTPException(400, "Shopify did not return an access token")
    connection = connection_for(db, shop) or ShopConnection(shop_domain=shop, encrypted_access_token="")
    connection.encrypted_access_token = encrypt_token(settings, token)
    db.add(connection)
    db.commit()
    redirect = RedirectResponse("/admin", status_code=303)
    redirect.set_cookie(
        "bf_session", sign_value(settings, {"shop": shop}), httponly=True, secure=True,
        samesite="none", max_age=60 * 60 * 24 * 30,
    )
    return redirect


@app.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("bf_session")
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    shop, _ = credentials(request, db, settings)
    latest = db.scalar(select(SyncRun).where(SyncRun.shop_domain == shop).order_by(SyncRun.id.desc()).limit(1))
    app_settings = get_app_settings(db)
    return templates.TemplateResponse(
        request=request, name="dashboard.html", context={
            "shop": shop, "latest": latest, "app_settings": app_settings, "csrf": csrf_token(settings, shop)
        }
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), config: Settings = Depends(get_settings)):
    shop, _ = credentials(request, db, config)
    rules = list(db.scalars(select(PricingRule).order_by(PricingRule.min_price)).all())
    return templates.TemplateResponse(
        request=request, name="settings.html", context={
            "shop": shop,
            "rules": rules,
            "rules_json": json.dumps([
                {
                    "id": rule.id,
                    "vat": str(rule.vat_percent),
                    "margin": str(rule.margin_percent),
                    "fee_percent": str(rule.payment_fee_percent),
                    "fee_fixed": str(rule.payment_fee_fixed),
                }
                for rule in rules
            ]),
            "app_settings": get_app_settings(db),
            "csrf": csrf_token(config, shop),
        }
    )


@app.post("/settings")
async def save_settings(request: Request, db: Session = Depends(get_db), config: Settings = Depends(get_settings)):
    shop, _ = credentials(request, db, config)
    form = await request.form()
    verify_csrf(config, shop, str(form.get("csrf", "")))
    allowed = {
        "sku_prefix", "minimum_supplier_stock", "missing_product_action",
        "max_price_change_percent", "max_items_per_apply", "automatic_apply",
    }
    if "automatic_apply" not in form:
        setting = db.get(AppSetting, "automatic_apply") or AppSetting(key="automatic_apply", value="false")
        setting.value = "false"
        db.add(setting)
    for key in allowed:
        if key in form:
            setting = db.get(AppSetting, key) or AppSetting(key=key, value="")
            setting.value = str(form[key]).strip()
            db.add(setting)
    selected_rule_id = form.get("selected_rule_id")
    if selected_rule_id:
        rule = db.get(PricingRule, int(selected_rule_id))
        if not rule:
            raise HTTPException(404, "Pricing rule not found")
        try:
            rule.vat_percent = Decimal(str(form["vat"]))
            rule.margin_percent = Decimal(str(form["margin"]))
            rule.payment_fee_percent = Decimal(str(form["payment_fee_percent"]))
            rule.payment_fee_fixed = Decimal(str(form["payment_fee_fixed"]))
        except (KeyError, InvalidOperation):
            raise HTTPException(400, "Invalid pricing values")
    else:
        for rule in db.scalars(select(PricingRule)).all():
            try:
                rule.vat_percent = Decimal(str(form[f"rule_{rule.id}_vat"]))
                rule.margin_percent = Decimal(str(form[f"rule_{rule.id}_margin"]))
                rule.payment_fee_percent = Decimal(str(form[f"rule_{rule.id}_fee_percent"]))
                rule.payment_fee_fixed = Decimal(str(form[f"rule_{rule.id}_fee_fixed"]))
                rule.enabled = f"rule_{rule.id}_enabled" in form
            except (KeyError, InvalidOperation):
                raise HTTPException(400, f"Invalid values for pricing rule {rule.id}")
    db.commit()
    for rule in db.scalars(select(PricingRule).order_by(PricingRule.min_price)).all():
        logger.info(
            "Pricing configuration saved shop=%s rule=%s range=%s-%s enabled=%s "
            "vat=%s%% margin=%s%% payment_fee=%s%% fixed_fee=%s; applies to future runs",
            shop,
            rule.id,
            rule.min_price,
            rule.max_price,
            rule.enabled,
            rule.vat_percent,
            rule.margin_percent,
            rule.payment_fee_percent,
            rule.payment_fee_fixed,
        )
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/sync/preview")
def start_preview(
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    config: Settings = Depends(get_settings),
):
    shop, _ = credentials(request, db, config)
    verify_csrf(config, shop, csrf)
    try:
        enqueue(db, shop, "preview")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse("/runs?started=preview", status_code=303)


@app.post("/sync/run")
def start_legacy_sync(
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    config: Settings = Depends(get_settings),
):
    shop, _ = credentials(request, db, config)
    verify_csrf(config, shop, csrf)
    logger.warning(
        "Synchronization requested for %s from ip=%s user_agent=%s",
        shop,
        request.client.host if request.client else "unknown",
        request.headers.get("user-agent", "unknown")[:300],
    )
    try:
        enqueue(db, shop, "sync")
    except RuntimeError:
        return RedirectResponse("/admin?busy=1", status_code=303)
    return RedirectResponse("/admin?started=1", status_code=303)


@app.post("/sync/{run_id}/apply")
def start_apply(
    run_id: int,
    request: Request,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    config: Settings = Depends(get_settings),
):
    shop, _ = credentials(request, db, config)
    verify_csrf(config, shop, csrf)
    try:
        enqueue_apply(db, run_id, shop)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/history", response_class=HTMLResponse)
def history(request: Request, db: Session = Depends(get_db), config: Settings = Depends(get_settings)):
    shop, _ = credentials(request, db, config)
    rows = db.execute(
        select(SyncItem, SyncRun)
        .join(SyncRun, SyncRun.id == SyncItem.run_id)
        .where(SyncRun.shop_domain == shop, SyncItem.result == "updated")
        .order_by(SyncRun.completed_at.desc(), SyncItem.id.desc())
        .limit(5000)
    ).all()
    history = []
    seen = set()
    for item, run in rows:
        if item.sku in seen:
            continue
        seen.add(item.sku)
        history.append({"sku": item.sku, "title": item.title, "updated_at": run.completed_at, "run_id": run.id})
    return templates.TemplateResponse(request=request, name="runs.html", context={
        "shop": shop, "history": history, "csrf": csrf_token(config, shop)
    })


@app.get("/runs", response_class=HTMLResponse)
@app.get("/runs/", response_class=HTMLResponse)
def run_logs(request: Request, db: Session = Depends(get_db), config: Settings = Depends(get_settings)):
    shop, _ = credentials(request, db, config)
    runs = list(
        db.scalars(
            select(SyncRun)
            .where(SyncRun.shop_domain == shop)
            .order_by(SyncRun.id.desc())
            .limit(100)
        ).all()
    )
    return templates.TemplateResponse(request=request, name="server_logs.html", context={
        "shop": shop, "runs": runs
    })


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int, request: Request, db: Session = Depends(get_db), config: Settings = Depends(get_settings)):
    shop, _ = credentials(request, db, config)
    run = db.get(SyncRun, run_id)
    if not run or run.shop_domain != shop:
        raise HTTPException(404)
    items = list(db.scalars(select(SyncItem).where(SyncItem.run_id == run_id).order_by(SyncItem.id).limit(500)).all())
    return templates.TemplateResponse(request=request, name="run_detail.html", context={
        "shop": shop, "run": run, "items": items, "csrf": csrf_token(config, shop)
    })


@app.get("/runs/{run_id}/csv")
def download_csv(run_id: int, request: Request, db: Session = Depends(get_db), config: Settings = Depends(get_settings)):
    shop, _ = credentials(request, db, config)
    run = db.get(SyncRun, run_id)
    if not run or run.shop_domain != shop:
        raise HTTPException(404)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Product", "Shopify SKU", "Beautyfort StockCode", "Old Price", "New Price",
                     "Old Stock", "New Stock", "Action", "Result", "Message"])
    for item in db.scalars(select(SyncItem).where(SyncItem.run_id == run_id).order_by(SyncItem.id)):
        writer.writerow([item.title, item.sku, item.supplier_code, item.old_price, item.new_price,
                         item.old_quantity, item.new_quantity, item.action, item.result, item.message])
    data = output.getvalue().encode("utf-8-sig")
    filename = f"beautyfort_sync_{run.id}.csv"
    return StreamingResponse(iter([data]), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'
    })
