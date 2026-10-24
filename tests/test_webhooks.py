import base64
import asyncio
import hashlib
import hmac

import pytest

from app.config import Settings
from app.security import verify_shopify_webhook_hmac
from app.shopify import ShopifyClient, ShopifyError


def test_verify_shopify_webhook_hmac_uses_raw_body():
    body = b'{"id":123,"domain":"example.myshopify.com"}'
    signature = base64.b64encode(
        hmac.new(b"secret", body, hashlib.sha256).digest()
    ).decode()

    assert verify_shopify_webhook_hmac(body, signature, "secret")
    assert not verify_shopify_webhook_hmac(body + b" ", signature, "secret")
    assert not verify_shopify_webhook_hmac(body, "", "secret")


def test_uninstall_webhook_registration_is_idempotent(monkeypatch):
    client = ShopifyClient(Settings(), "example.myshopify.com", "token")
    calls = []

    async def graphql(query, variables=None, attempts=5):
        calls.append((query, variables))
        return {
            "webhookSubscriptions": {
                "nodes": [{"id": "gid://shopify/WebhookSubscription/1", "uri": "https://app.test/webhooks/app/uninstalled"}]
            }
        }

    monkeypatch.setattr(client, "graphql", graphql)
    asyncio.run(client.ensure_app_uninstalled_webhook("https://app.test/webhooks/app/uninstalled"))

    assert len(calls) == 1


def test_uninstall_webhook_registration_reports_shopify_errors(monkeypatch):
    client = ShopifyClient(Settings(), "example.myshopify.com", "token")

    async def graphql(query, variables=None, attempts=5):
        if "query AppUninstalledWebhooks" in query:
            return {"webhookSubscriptions": {"nodes": []}}
        return {
            "webhookSubscriptionCreate": {
                "webhookSubscription": None,
                "userErrors": [{"field": ["webhookSubscription", "uri"], "message": "Invalid URI"}],
            }
        }

    monkeypatch.setattr(client, "graphql", graphql)
    with pytest.raises(ShopifyError, match="Unable to register"):
        asyncio.run(client.ensure_app_uninstalled_webhook("not-a-url"))
