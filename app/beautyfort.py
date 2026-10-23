import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

import httpx

from .config import Settings


@dataclass(frozen=True)
class SupplierProduct:
    stock_code: str
    stock_level: int
    price: Decimal


class BeautyfortError(RuntimeError):
    pass


class BeautyfortClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def fetch_stock(self) -> list[SupplierProduct]:
        if not self.settings.beautyfort_username or not self.settings.beautyfort_secret:
            raise BeautyfortError("Beautyfort credentials are not configured")

        nonce = secrets.token_hex(16)
        created = datetime.now(timezone.utc).isoformat()
        # Legacy Beautyfort protocol: PHP sha1() returns a hexadecimal string,
        # and that ASCII string is then base64-encoded.
        digest = hashlib.sha1(
            f"{nonce}{created}{self.settings.beautyfort_secret}".encode(), usedforsecurity=False
        ).hexdigest()
        password = base64.b64encode(digest.encode()).decode()
        test_mode = "true" if self.settings.beautyfort_test_mode else "false"
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:bf="http://www.beautyfort.com/api/">
  <soap:Header>
    <bf:AuthHeader>
      <bf:Username>{_escape(self.settings.beautyfort_username)}</bf:Username>
      <bf:Nonce>{nonce}</bf:Nonce>
      <bf:Created>{created}</bf:Created>
      <bf:Password>{password}</bf:Password>
    </bf:AuthHeader>
  </soap:Header>
  <soap:Body>
    <bf:GetStockFileRequest>
      <bf:TestMode>{test_mode}</bf:TestMode>
      <bf:StockFileFormat>JSON</bf:StockFileFormat>
      <bf:StockFileFields>
        <bf:StockFileField>StockCode</bf:StockFileField>
        <bf:StockFileField>StockLevel</bf:StockFileField>
        <bf:StockFileField>Price</bf:StockFileField>
      </bf:StockFileFields>
      <bf:SortBy>StockCode</bf:SortBy>
      <bf:StockFileEncoding>UTF-8</bf:StockFileEncoding>
    </bf:GetStockFileRequest>
  </soap:Body>
</soap:Envelope>"""

        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
            response = await client.post(
                self.settings.beautyfort_api_url,
                content=xml.encode(),
                headers={"Content-Type": "text/xml; charset=UTF-8", "Accept": "text/xml"},
            )
        response.raise_for_status()
        return self._parse_response(response.text)

    @staticmethod
    def _parse_response(xml: str) -> list[SupplierProduct]:
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise BeautyfortError("Beautyfort returned invalid XML") from exc

        encoded = next((node.text for node in root.iter() if node.tag.split("}")[-1].split("_")[-1] == "File"), None)
        if not encoded:
            fault = next((node.text for node in root.iter() if node.tag.split("}")[-1] == "faultstring"), None)
            raise BeautyfortError(fault or "Beautyfort response contains no stock file")
        try:
            rows = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BeautyfortError("Beautyfort stock file is not valid base64 JSON") from exc

        products: list[SupplierProduct] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                products.append(
                    SupplierProduct(
                        stock_code=str(row["StockCode"]).strip(),
                        stock_level=max(0, int(row["StockLevel"])),
                        price=Decimal(str(row["Price"])),
                    )
                )
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
        return products


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
