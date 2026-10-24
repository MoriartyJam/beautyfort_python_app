import base64
import hashlib
import hmac
import re
from urllib.parse import urlencode

from cryptography.fernet import Fernet
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import Settings


SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")


def valid_shop_domain(shop: str) -> bool:
    return bool(SHOP_RE.fullmatch(shop.strip().lower()))


def verify_shopify_hmac(params: dict[str, str], secret: str) -> bool:
    received = params.get("hmac", "")
    message = urlencode(sorted((k, v) for k, v in params.items() if k not in {"hmac", "signature"}))
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return bool(received) and hmac.compare_digest(received, expected)


def verify_shopify_webhook_hmac(body: bytes, received: str, secret: str) -> bool:
    """Verify Shopify's base64 HMAC signature for a raw webhook body."""
    if not received or not secret:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(received, expected)


def serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app_secret, salt="beautyfort-sync")


def sign_value(settings: Settings, payload: dict) -> str:
    return serializer(settings).dumps(payload)


def unsign_value(settings: Settings, value: str, max_age: int = 3600) -> dict | None:
    try:
        return serializer(settings).loads(value, max_age=max_age)
    except BadSignature:
        return None


def _fernet(settings: Settings) -> Fernet:
    if settings.token_encryption_key:
        key = settings.token_encryption_key.encode()
    else:
        digest = hashlib.sha256(settings.app_secret.encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_token(settings: Settings, token: str) -> str:
    return _fernet(settings).encrypt(token.encode()).decode()


def decrypt_token(settings: Settings, encrypted: str) -> str:
    return _fernet(settings).decrypt(encrypted.encode()).decode()
