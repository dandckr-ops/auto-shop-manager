# Auto Shop Manager Starter

A small, dependency-free repair shop manager for a one-person shop.

## Run it with Docker

```sh
docker compose up -d --build
```

Run that from the folder containing `docker-compose.yml`. Compose reads the
service definitions from that file: the app image is built from this folder
because the `app` service has `build: .`, and Postgres is pulled from
`postgres:17` because the `db` service has `image: postgres:17`.

Then open `http://SERVER_IP:8787`.

Live data is stored in the `auto-shop-postgres-data` Docker volume. The app also
writes a JSON backup into the `auto-shop-app-data` Docker volume after each save.
The browser keeps a local fallback copy if the API is temporarily unavailable.

## SSO and Cloudflared

The app includes an optional SSO overlay:

```sh
cp .env.sso.example .env.sso
openssl rand -hex 24
docker compose --env-file .env.sso -f docker-compose.yml -f docker-compose.sso.yml up -d --build
```

The intended production path is:

```text
Internet -> Cloudflared tunnel -> oauth2-proxy -> Auto Shop Manager -> Postgres
```

Use a Keycloak OIDC client with this redirect URI:

```text
https://YOUR_SHOP_HOSTNAME/oauth2/callback
```

If Cloudflared is managed outside this Compose project, point the tunnel service
at `http://127.0.0.1:8787`. In SSO mode that port is the OAuth proxy, not the
app itself.

Set `POSTGRES_PASSWORD` in `.env.sso` before starting the stack. Do not commit
real `.env` or `.env.sso` files.

## Current scope

- Customers and vehicles
- NHTSA vPIC VIN decode and model suggestions for vehicle entry
- Estimates / repair orders
- Labor and parts lines
- Mock parts lookup with supplier pricing
- Parts order tracking
- PostgreSQL-backed shared data
- JSON export/import for extra backups
- SMTP estimate emails

## Email

Set these values in `.env` for local/LAN mode or `.env.sso` for SSO mode:

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=sender@example.com
SMTP_PASSWORD=
SMTP_FROM=sender@example.com
SMTP_TLS=true
```

## Parts API path

The app is built around a simple parts-provider layer in `app.js`. The mock
provider can later be replaced with a real adapter for a service such as
PartsTech, Nexpart, NAPA integrations, or another approved supplier API.

Avoid scraping retailer websites unless you have explicit permission. Parts
catalogs and wholesale pricing are usually covered by supplier terms.
