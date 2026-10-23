# Architecture and migration notes

## Business flow

1. Read Shopify variants whose SKU starts with the configured prefix (`BR` by default).
2. Download `StockCode`, `StockLevel`, and `Price` from Beautyfort's SOAP stock file.
3. Match `BR{StockCode}` (and legacy raw `StockCode`) to the Shopify variant SKU.
4. Calculate a proposed retail price using the configured price band.
5. Create an auditable preview in PostgreSQL and generate CSV on demand without changing Shopify.
6. Apply only previewed changes after confirmation, or through the optional scheduled worker.

## Application components

- `app/main.py`: FastAPI web service, authentication, Shopify OAuth and admin routes.
- `app/beautyfort.py`: Beautyfort SOAP authentication and stock-feed parser.
- `app/shopify.py`: Shopify GraphQL reads, price updates and concurrency-safe inventory writes.
- `app/sync_service.py`: matching, safety checks, preview, application and CSV generation.
- `app/models.py`: shops, price bands, settings, synchronization runs and item-level audit log.
- `app/jobs.py`: durable PostgreSQL queue and atomic job claiming.
- `app/worker.py`: required Seenode worker for manual and scheduled jobs.

## Differences from the legacy application

- Credentials come from environment variables; OAuth tokens are encrypted in the database.
- Automatic scheduled updates are disabled by default and must be enabled explicitly.
- Every run creates a preview before changes are applied.
- Large price changes and oversized batches are blocked for manual review.
- Concurrent inventory writes use Shopify's compare-and-set and idempotency support.
- TLS verification stays enabled.
- SQLAlchemy generates parameterized SQL instead of interpolating user input.
- Admin writes use signed sessions and CSRF tokens.

## Power Body reference

The reference project informed the user-facing feature set: Shopify authorization, embedded-style admin settings, scheduled synchronization, supplier API handling and CSV exports. Its single-file implementation, legacy REST calls, filesystem settings and plaintext operational patterns were not copied.

## Production decisions still required

- Confirm whether this remains a single-store custom app or will be distributed to other merchants.
- Confirm which Shopify location should receive Beautyfort inventory. The current implementation chooses the first active location; a location selector should be added if the shop uses multiple locations.
- Confirm whether low supplier stock should become zero (legacy behavior) or remain unchanged.
- Rotate all credentials exposed in the legacy PHP project before connecting production.
