# Beautyfort Sync

A clean Python replacement for the legacy Beautyfort PHP application. It synchronizes Beautyfort supplier prices and inventory with Shopify while preserving the original business rules and adding a review-first workflow.

## Included functionality

- Shopify OAuth or a single-store custom-app token.
- Beautyfort SOAP `GetStockFileRequest` integration.
- Shopify GraphQL Admin API `2026-10` integration.
- Legacy SKU matching: Shopify `BR{StockCode}` or raw `StockCode`.
- The six original price bands with editable VAT, margin and payment fees.
- Configurable minimum supplier stock (legacy default: `5`).
- Safe behavior for products missing from the supplier feed (default: review, optional: zero).
- Preview before update, maximum price-change protection and maximum batch size.
- Price and inventory application to Shopify.
- Synchronization history, per-item results and CSV reports generated from PostgreSQL.
- Durable PostgreSQL job queue and a worker protected against overlapping runs.

## Local setup

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   . .venv/bin/activate
   pip install -r requirements-dev.txt
   ```

2. Copy `.env.example` to `.env` and configure it. Never commit `.env`.

3. Generate secure values:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

4. Start the web application:

   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

5. Open `http://localhost:8000`.

For a one-store app in your Shopify organization, set `SHOPIFY_SHOP`, `SHOPIFY_CLIENT_ID`, and `SHOPIFY_CLIENT_SECRET`. The app obtains an Admin API token through Shopify's client credentials grant. A static `SHOPIFY_ACCESS_TOKEN` remains available only for legacy custom-app testing. For OAuth installation on stores outside your organization, set `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`, and `APP_URL`, then open:

`https://your-app.example/install?shop=your-store.myshopify.com`

## Seenode deployment

Create these resources in one Seenode workspace:

1. A managed PostgreSQL database.
2. A Python Web Service using this directory as the project root.
3. A Python Background Worker for manual and scheduled synchronization.

Web service configuration:

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port 80`
- Port: `80`
- Health check: `/health`

Worker configuration:

- Build command: `pip install -r requirements.txt`
- Start command: `python -m app.worker`

Set `DATABASE_URL` to Seenode's PostgreSQL connection string using the `postgresql+psycopg://` scheme and add every value from `.env.example` in Seenode Envs. The web service and worker must share the same database and encryption keys. Set `APP_ENVIRONMENT=production`; startup then rejects SQLite, weak secrets, and non-HTTPS URLs.

CSV files are generated on demand from the audit data in PostgreSQL, so no persistent filesystem is required.

## Safe rollout

1. Rotate all credentials found in the legacy PHP project.
2. Use a Shopify development store or duplicate products for the first test.
3. Keep `AUTOMATIC_APPLY=false`; in that state no timed job is created. Manual updates still work through the Worker.
4. Run a preview and validate the CSV.
5. Apply a small batch, then compare Shopify prices and inventory.
6. Keep the worker running: it performs manual jobs too. Set `AUTOMATIC_APPLY=true` in the Worker only after the manual test is accepted.

## Tests

```bash
pytest -q
```
