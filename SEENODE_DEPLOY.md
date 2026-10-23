# Seenode deployment

## 1. PostgreSQL

Create a PostgreSQL Database in the same Seenode workspace. Copy its internal connection URI into `DATABASE_URL` for both services. The application automatically accepts `postgres://`, `postgresql://`, and `postgresql+psycopg://` URIs.

## 2. Web service

- Type: Web
- Root directory: repository root
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port 80`
- Port: `80`
- Health check: `/health`

## 3. Worker

- Type: Worker
- Same repository and branch as Web
- Build: `pip install -r requirements.txt`
- Start: `python -m app.worker`
- No public port

The Worker is required even when timed automatic updates are disabled. Manual update buttons place durable jobs in PostgreSQL and the Worker executes them.

## 4. Environment

Copy all keys from `.env.example` into both Web and Worker. Their values must be identical, especially `DATABASE_URL`, `APP_SECRET`, and `TOKEN_ENCRYPTION_KEY`.

Generate secrets locally:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Do not upload `.env`. Start with `AUTOMATIC_APPLY=false`. Manual updates still run, but the Worker will not create timed jobs. Set it to `true` only after testing.

## 5. Shopify

Set the app configuration to:

- App URL: `https://YOUR-SERVICE.seenode.app`
- Allowed redirect URL: `https://YOUR-SERVICE.seenode.app/auth/callback`
- Scopes: `write_products,write_inventory,read_locations`

Set `SHOPIFY_LOCATION_ID` to the selected location in the target store, not the development-store location.

## 6. First production check

1. Deploy PostgreSQL, Web, and Worker.
2. Verify `/health` returns `{"status":"ok"}`.
3. Install or reconnect the Shopify app.
4. Verify all pricing ranges in Configuration.
5. Run Preview and download its CSV.
6. Apply a deliberately small test set before enabling full updates.
7. Enable automatic application only after the manual result is accepted.

If a Worker is restarted during a synchronization, the interrupted run is marked failed. Successfully committed item updates remain recorded, allowing the operator to review partial results before retrying.
