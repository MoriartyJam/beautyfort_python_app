import asyncio
from dataclasses import dataclass
from decimal import Decimal

import httpx

from .config import Settings


class ShopifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShopifyVariant:
    product_id: str
    variant_id: str
    inventory_item_id: str
    title: str
    sku: str
    price: Decimal
    quantity: int


class ShopifyClient:
    def __init__(self, settings: Settings, shop: str, access_token: str):
        self.settings = settings
        self.shop = shop
        self.url = f"https://{shop}/admin/api/{settings.shopify_api_version}/graphql.json"
        self.headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}

    @staticmethod
    async def client_credentials_token(settings: Settings, shop: str) -> str:
        if not settings.shopify_client_id or not settings.shopify_client_secret:
            raise ShopifyError("Shopify Client ID and Client Secret are not configured")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"https://{shop}/admin/oauth/access_token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.shopify_client_id,
                    "client_secret": settings.shopify_client_secret,
                },
            )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise ShopifyError("Shopify did not return an access token")
        return token

    async def graphql(self, query: str, variables: dict | None = None, attempts: int = 5) -> dict:
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=90) as client:
            for attempt in range(attempts):
                try:
                    response = await client.post(
                        self.url, headers=self.headers, json={"query": query, "variables": variables or {}}
                    )
                except httpx.RequestError as exc:
                    last_error = exc
                    if attempt + 1 < attempts:
                        await asyncio.sleep(min(2 ** attempt, 8))
                        continue
                    raise ShopifyError(f"Shopify request failed after {attempts} attempts: {exc}") from exc
                if response.status_code == 429:
                    await asyncio.sleep(float(response.headers.get("Retry-After", 2 ** attempt)))
                    continue
                if response.status_code >= 500:
                    last_error = ShopifyError(f"Shopify temporarily returned HTTP {response.status_code}")
                    if attempt + 1 < attempts:
                        await asyncio.sleep(min(2 ** attempt, 8))
                        continue
                    raise last_error
                response.raise_for_status()
                payload = response.json()
                if payload.get("errors"):
                    if any(error.get("extensions", {}).get("code") == "THROTTLED" for error in payload["errors"]):
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise ShopifyError(str(payload["errors"]))
                return payload.get("data", {})
        raise ShopifyError(f"Shopify retry budget was exhausted: {last_error or 'rate limited'}")

    async def fetch_variants(self, sku_prefix: str) -> list[ShopifyVariant]:
        query = """
        query Variants($first: Int!, $after: String, $query: String!) {
          productVariants(first: $first, after: $after, query: $query) {
            nodes {
              id sku price inventoryQuantity
              displayName
              product { id title }
              inventoryItem { id }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        variants: list[ShopifyVariant] = []
        cursor = None
        while True:
            data = await self.graphql(
                query, {"first": 250, "after": cursor, "query": f"sku:{sku_prefix}*"}
            )
            connection = data["productVariants"]
            for node in connection["nodes"]:
                sku = (node.get("sku") or "").strip()
                inventory_item = node.get("inventoryItem") or {}
                if not sku or not inventory_item.get("id"):
                    continue
                variants.append(
                    ShopifyVariant(
                        product_id=node["product"]["id"],
                        variant_id=node["id"],
                        inventory_item_id=inventory_item["id"],
                        title=node.get("displayName") or node["product"]["title"],
                        sku=sku,
                        price=Decimal(node["price"]),
                        quantity=int(node.get("inventoryQuantity") or 0),
                    )
                )
            if not connection["pageInfo"]["hasNextPage"]:
                return variants
            cursor = connection["pageInfo"]["endCursor"]

    async def update_price(self, variant_id: str, price: Decimal) -> None:
        mutation = """
        mutation UpdateVariant($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            userErrors { field message }
          }
        }
        """
        # productVariantsBulkUpdate requires product id, supplied separately by caller through GID split cache.
        raise NotImplementedError("Use update_variant with product_id")

    async def update_variant(
        self,
        product_id: str,
        variant_id: str,
        inventory_item_id: str,
        old_quantity: int,
        new_price: Decimal,
        new_quantity: int,
        location_id: str,
        idempotency_key: str,
    ) -> None:
        inventory_query = """
        query InventoryAtLocation($inventoryItemId: ID!, $locationId: ID!) {
          inventoryItem(id: $inventoryItemId) {
            inventoryLevel(locationId: $locationId) {
              quantities(names: ["available"]) { name quantity }
            }
          }
        }
        """
        inventory_data = await self.graphql(
            inventory_query,
            {"inventoryItemId": inventory_item_id, "locationId": location_id},
        )
        inventory_item = inventory_data.get("inventoryItem") or {}
        inventory_level = inventory_item.get("inventoryLevel") or {}
        quantities = inventory_level.get("quantities") or []
        current_quantity = next(
            (int(entry["quantity"]) for entry in quantities if entry.get("name") == "available"),
            None,
        )
        if current_quantity is None:
            raise ShopifyError(f"Inventory item is not stocked at location {location_id}")

        price_mutation = """
        mutation UpdateVariant($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            userErrors { field message }
          }
        }
        """
        data = await self.graphql(
            price_mutation,
            {"productId": product_id, "variants": [{"id": variant_id, "price": str(new_price)}]},
        )
        errors = data["productVariantsBulkUpdate"]["userErrors"]
        if errors:
            raise ShopifyError(str(errors))

        inventory_mutation = """
        mutation SetInventory($input: InventorySetQuantitiesInput!, $key: String!) {
          inventorySetQuantities(input: $input) @idempotent(key: $key) {
            userErrors { code field message }
          }
        }
        """
        variables = {
            "key": idempotency_key,
            "input": {
                "name": "available",
                "reason": "correction",
                "referenceDocumentUri": f"gid://beautyfort-sync/Sync/{idempotency_key}",
                "quantities": [
                    {
                        "inventoryItemId": inventory_item_id,
                        "locationId": location_id,
                        "quantity": new_quantity,
                        "changeFromQuantity": current_quantity,
                    }
                ],
            },
        }
        data = await self.graphql(inventory_mutation, variables)
        errors = data["inventorySetQuantities"]["userErrors"]
        if errors:
            raise ShopifyError(str(errors))

    async def primary_location_id(self) -> str:
        configured = self.settings.shopify_location_id.strip()
        if configured:
            return configured if configured.startswith("gid://") else f"gid://shopify/Location/{configured}"
        data = await self.graphql("{ locations(first: 20, query: \"active:true\") { nodes { id name } } }")
        locations = data["locations"]["nodes"]
        if not locations:
            raise ShopifyError("No active Shopify location was found")
        return locations[0]["id"]
