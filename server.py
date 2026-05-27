import base64
import hashlib
import json
import math
import os
import secrets
import smtplib
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode, unquote, urlparse, parse_qs

import psycopg


APP_DIR = Path(__file__).resolve().parent
APP_DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/data"))
NHTSA_API_BASE = "https://vpic.nhtsa.dot.gov/api/vehicles"
AUTH_SETTINGS_KEY = "auth"
SESSION_COOKIE = "asm_session"
SESSION_SECONDS = 60 * 60 * 12
LOGIN_STATE_SECONDS = 60 * 10
TAX_RATE = 0.08125
ORDER_STATUSES = [
    "estimate created",
    "estimate sent",
    "estimate approved, order parts",
    "waiting on parts",
    "ready to be completed",
    "work done",
    "paid/close",
]
LEGACY_STATUS_MAP = {
    "Estimate": "estimate created",
    "Approved": "estimate approved, order parts",
    "In Progress": "ready to be completed",
    "Waiting Parts": "waiting on parts",
    "Ready": "ready to be completed",
    "Paid": "paid/close",
}


def database_url() -> str:
    return os.environ["DATABASE_URL"]


def connect():
    return psycopg.connect(database_url())


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            create table if not exists customers (
                id text primary key,
                name text not null,
                phone text not null default '',
                email text not null default '',
                notes text not null default '',
                created_at timestamptz not null default now(),
                updated_at timestamptz not null default now()
            )
            """
        )
        conn.execute(
            """
            create table if not exists vehicles (
                id text primary key,
                customer_id text not null references customers(id) on delete cascade,
                year text not null default '',
                make text not null default '',
                model text not null default '',
                engine text not null default '',
                vin text not null default '',
                created_at timestamptz not null default now(),
                updated_at timestamptz not null default now()
            )
            """
        )
        conn.execute(
            """
            create table if not exists repair_orders (
                id text primary key,
                customer_id text not null references customers(id) on delete cascade,
                vehicle_id text references vehicles(id) on delete set null,
                status text not null default 'estimate created',
                odometer text not null default '',
                concern text not null default '',
                created_at timestamptz not null default now(),
                updated_at timestamptz not null default now()
            )
            """
        )
        conn.execute(
            """
            create table if not exists order_lines (
                id text primary key,
                order_id text not null references repair_orders(id) on delete cascade,
                line_type text not null check (line_type in ('labor', 'part')),
                description text not null default '',
                qty numeric(10, 2) not null default 0,
                rate numeric(10, 2) not null default 0,
                position integer not null default 0
            )
            """
        )
        conn.execute(
            """
            create table if not exists parts_orders (
                id text primary key,
                supplier text not null default '',
                part_name text not null default '',
                cost numeric(10, 2) not null default 0,
                retail numeric(10, 2) not null default 0,
                eta text not null default '',
                status text not null default 'Quoted',
                created_at timestamptz not null default now(),
                updated_at timestamptz not null default now()
            )
            """
        )
        conn.execute(
            """
            create table if not exists parts_providers (
                id text primary key,
                provider_key text not null unique,
                display_name text not null,
                description text not null default '',
                enabled boolean not null default false,
                status text not null default 'staged',
                api_base_url text not null default '',
                account_id text not null default '',
                created_at timestamptz not null default now(),
                updated_at timestamptz not null default now()
            )
            """
        )
        conn.execute("create index if not exists idx_vehicles_customer on vehicles(customer_id)")
        conn.execute("create index if not exists idx_orders_customer on repair_orders(customer_id)")
        conn.execute("create index if not exists idx_order_lines_order on order_lines(order_id)")
        conn.execute("create index if not exists idx_parts_orders_status on parts_orders(status)")
        conn.execute("create table if not exists app_state_archive (id text primary key, data jsonb not null, updated_at timestamptz not null default now())")
        conn.execute(
            """
            create table if not exists app_settings (
                key text primary key,
                value jsonb not null,
                updated_at timestamptz not null default now()
            )
            """
        )
        conn.execute(
            """
            create table if not exists auth_sessions (
                id text primary key,
                provider text not null,
                username text not null default '',
                email text not null default '',
                display_name text not null default '',
                expires_at timestamptz not null,
                created_at timestamptz not null default now()
            )
            """
        )
        conn.execute(
            """
            create table if not exists auth_login_states (
                state text primary key,
                nonce text not null,
                code_verifier text not null,
                next_path text not null default '/',
                expires_at timestamptz not null,
                created_at timestamptz not null default now()
            )
            """
        )
        conn.execute(
            """
            create table if not exists auth_users (
                username text primary key,
                password_hash text not null,
                display_name text not null default '',
                active boolean not null default true,
                updated_at timestamptz not null default now()
            )
            """
        )
        conn.execute("alter table vehicles add column if not exists trim text not null default ''")
        conn.execute("alter table vehicles add column if not exists body text not null default ''")
        conn.execute("alter table vehicles add column if not exists source text not null default ''")
        conn.execute("alter table vehicles add column if not exists plate text not null default ''")
        conn.execute("alter table vehicles add column if not exists plate_state text not null default ''")
        conn.execute("alter table vehicles add column if not exists rockauto_url text not null default ''")
        conn.execute("alter table repair_orders alter column status set default 'estimate created'")
        migrate_order_statuses(conn)
        seed_parts_providers(conn)
        seed_auth_settings(conn)
        seed_local_user(conn)
    migrate_blob_state()


def normalize_order_status(status: str | None) -> str:
    if status in LEGACY_STATUS_MAP:
        return LEGACY_STATUS_MAP[status]
    if status in ORDER_STATUSES:
        return status
    return ORDER_STATUSES[0]


def migrate_order_statuses(conn) -> None:
    for old_status, new_status in LEGACY_STATUS_MAP.items():
        conn.execute(
            "update repair_orders set status = %s where status = %s",
            (new_status, old_status),
        )


def seed_parts_providers(conn) -> None:
    providers = [
        ("partstech", "PartsTech", "Aggregator for live supplier pricing and ordering"),
        ("nexpart", "Nexpart", "Multi-seller aftermarket catalog and ordering"),
        ("napa", "NAPA Pro", "Professional account integration placeholder"),
        ("rockauto", "RockAuto", "Manual catalog handoff for vehicle-based lookups"),
        ("manual", "Manual Supplier", "Fallback for phone quotes and local vendors"),
    ]
    for provider_key, display_name, description in providers:
        conn.execute(
            """
            insert into parts_providers (id, provider_key, display_name, description, enabled, status)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (provider_key) do nothing
            """,
            (
                str(uuid.uuid4()),
                provider_key,
                display_name,
                description,
                provider_key in {"manual", "rockauto"},
                "manual lookup"
                if provider_key == "rockauto"
                else ("mock-ready" if provider_key == "manual" else "needs credentials"),
            ),
        )


def default_auth_settings() -> dict:
    issuer_url = os.environ.get("OIDC_ISSUER_URL", "").strip()
    client_id = os.environ.get("OIDC_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
    return {
        "localEnabled": os.environ.get("LOCAL_AUTH_ENABLED", "true").lower() == "true",
        "oidcEnabled": bool(issuer_url and client_id and client_secret),
        "issuerUrl": issuer_url,
        "clientId": client_id,
        "clientSecret": client_secret,
        "publicUrl": os.environ.get("SHOP_PUBLIC_URL", "").strip(),
        "emailDomains": os.environ.get("OAUTH2_PROXY_EMAIL_DOMAINS", "*").strip() or "*",
    }


def seed_auth_settings(conn) -> None:
    exists = conn.execute(
        "select exists (select 1 from app_settings where key = %s)",
        (AUTH_SETTINGS_KEY,),
    ).fetchone()[0]
    if exists:
        return
    conn.execute(
        """
        insert into app_settings (key, value)
        values (%s, %s::jsonb)
        """,
        (AUTH_SETTINGS_KEY, json.dumps(default_auth_settings())),
    )


def seed_local_user(conn) -> None:
    password = os.environ.get("LOCAL_AUTH_PASSWORD", "")
    if not password:
        return
    username = os.environ.get("LOCAL_AUTH_USERNAME", "admin").strip() or "admin"
    exists = conn.execute("select exists (select 1 from auth_users where username = %s)", (username,)).fetchone()[0]
    if exists:
        return
    conn.execute(
        """
        insert into auth_users (username, password_hash, display_name, active)
        values (%s, %s, %s, true)
        """,
        (username, hash_password(password), username),
    )


def migrate_blob_state() -> None:
    with connect() as conn:
        has_customers = conn.execute("select exists (select 1 from customers)").fetchone()[0]
        app_state_exists = conn.execute("select to_regclass('public.app_state') is not null").fetchone()[0]
        if has_customers or not app_state_exists:
            return
        row = conn.execute("select data from app_state where id = 'shop-state'").fetchone()
        if row and isinstance(row[0], dict):
            write_state(row[0], conn)
            conn.execute(
                """
                insert into app_state_archive (id, data, updated_at)
                values ('shop-state-imported', %s, now())
                on conflict (id) do update
                set data = excluded.data,
                    updated_at = now()
                """,
                (json.dumps(row[0]),),
            )


def empty_state() -> dict:
    return {"customers": [], "orders": [], "partsOrders": []}


def decode_json_value(value):
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def get_auth_settings(include_secret: bool = True) -> dict:
    with connect() as conn:
        row = conn.execute(
            "select value from app_settings where key = %s",
            (AUTH_SETTINGS_KEY,),
        ).fetchone()
        if not row:
            settings = default_auth_settings()
            conn.execute(
                """
                insert into app_settings (key, value)
                values (%s, %s::jsonb)
                """,
                (AUTH_SETTINGS_KEY, json.dumps(settings)),
            )
        else:
            settings = {**default_auth_settings(), **decode_json_value(row[0])}
        if not include_secret:
            client_secret_configured = bool(settings.get("clientSecret"))
            settings = {key: value for key, value in settings.items() if key != "clientSecret"}
            settings["clientSecretConfigured"] = client_secret_configured
        local_user = conn.execute("select username from auth_users where active = true order by username limit 1").fetchone()
    settings["localUsername"] = local_user[0] if local_user else os.environ.get("LOCAL_AUTH_USERNAME", "admin")
    settings["localUserConfigured"] = bool(local_user)
    return settings


def update_auth_settings(data: dict) -> dict:
    current = get_auth_settings(include_secret=True)
    next_settings = {
        "localEnabled": bool(data.get("localEnabled")),
        "oidcEnabled": bool(data.get("oidcEnabled")),
        "issuerUrl": str(data.get("issuerUrl") or "").strip().rstrip("/"),
        "clientId": str(data.get("clientId") or "").strip(),
        "clientSecret": str(data.get("clientSecret") or "").strip() or current.get("clientSecret", ""),
        "publicUrl": str(data.get("publicUrl") or "").strip().rstrip("/"),
        "emailDomains": str(data.get("emailDomains") or "*").strip() or "*",
    }
    username = str(data.get("localUsername") or current.get("localUsername") or "admin").strip() or "admin"
    password = str(data.get("localPassword") or "")

    with connect() as conn:
        conn.execute(
            """
            insert into app_settings (key, value, updated_at)
            values (%s, %s::jsonb, now())
            on conflict (key) do update
            set value = excluded.value,
                updated_at = now()
            """,
            (AUTH_SETTINGS_KEY, json.dumps(next_settings)),
        )
        if password:
            conn.execute(
                """
                insert into auth_users (username, password_hash, display_name, active, updated_at)
                values (%s, %s, %s, true, now())
                on conflict (username) do update
                set password_hash = excluded.password_hash,
                    display_name = excluded.display_name,
                    active = true,
                    updated_at = now()
                """,
                (username, hash_password(password), username),
            )
        elif data.get("localUsername") and current.get("localUserConfigured"):
            conn.execute(
                """
                update auth_users
                set username = %s,
                    display_name = %s,
                    updated_at = now()
                where username = %s
                """,
                (username, username, current.get("localUsername")),
            )
    return get_auth_settings(include_secret=False)


def hash_password(password: str) -> str:
    iterations = 260000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, iterations, salt_text, digest_text = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def auth_is_enabled() -> bool:
    settings = get_auth_settings()
    return bool(settings.get("localEnabled") or settings.get("oidcEnabled"))


def safe_next_path(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def public_url_from_headers(headers) -> str:
    settings = get_auth_settings()
    if settings.get("publicUrl"):
        return settings["publicUrl"].rstrip("/")
    proto = headers.get("X-Forwarded-Proto") or "http"
    host = headers.get("Host") or "127.0.0.1"
    return f"{proto}://{host}"


def oidc_redirect_uri(headers) -> str:
    return f"{public_url_from_headers(headers)}/oauth2/callback"


def fetch_json_url(url: str, headers: dict | None = None, data: bytes | None = None) -> dict:
    request_headers = {"User-Agent": "AutoShopManager/1.0"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(detail or exc.reason) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Provider returned an invalid response") from exc


def oidc_discovery(settings: dict) -> dict:
    issuer = str(settings.get("issuerUrl") or "").strip().rstrip("/")
    if not issuer:
        raise RuntimeError("OIDC issuer URL is not configured")
    return fetch_json_url(f"{issuer}/.well-known/openid-configuration")


def oidc_allowed_email(email: str, settings: dict) -> bool:
    rules = [rule.strip().lower() for rule in str(settings.get("emailDomains") or "*").split(",") if rule.strip()]
    if "*" in rules:
        return True
    email = email.lower()
    domain = email.split("@")[-1] if "@" in email else ""
    return email in rules or domain in rules


def create_auth_session(provider: str, username: str, email: str = "", display_name: str = "") -> str:
    session_id = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)
    with connect() as conn:
        conn.execute(
            """
            insert into auth_sessions (id, provider, username, email, display_name, expires_at)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (session_id, provider, username, email, display_name, expires_at),
        )
    return session_id


def find_auth_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            select id, provider, username, email, display_name, expires_at
            from auth_sessions
            where id = %s
            """,
            (session_id,),
        ).fetchone()
        if not row:
            return None
        if row[5] <= datetime.now(timezone.utc):
            conn.execute("delete from auth_sessions where id = %s", (session_id,))
            return None
    return {
        "id": row[0],
        "provider": row[1],
        "username": row[2],
        "email": row[3],
        "displayName": row[4],
    }


def delete_auth_session(session_id: str | None) -> None:
    if not session_id:
        return
    with connect() as conn:
        conn.execute("delete from auth_sessions where id = %s", (session_id,))


def local_login(data: dict) -> tuple[str, dict]:
    settings = get_auth_settings()
    if not settings.get("localEnabled"):
        raise ValueError("Local login is not enabled")
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        raise ValueError("Username and password are required")
    with connect() as conn:
        row = conn.execute(
            """
            select username, password_hash, display_name
            from auth_users
            where username = %s and active = true
            """,
            (username,),
        ).fetchone()
    if not row:
        raise ValueError("Local login is not configured yet")
    if not verify_password(password, row[1]):
        raise ValueError("Invalid username or password")
    session_id = create_auth_session("local", row[0], "", row[2] or row[0])
    return session_id, {"provider": "local", "username": row[0], "displayName": row[2] or row[0]}


def save_oidc_login_state(state: str, nonce: str, code_verifier: str, next_path: str) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=LOGIN_STATE_SECONDS)
    with connect() as conn:
        conn.execute("delete from auth_login_states where expires_at <= now()")
        conn.execute(
            """
            insert into auth_login_states (state, nonce, code_verifier, next_path, expires_at)
            values (%s, %s, %s, %s, %s)
            """,
            (state, nonce, code_verifier, safe_next_path(next_path), expires_at),
        )


def pop_oidc_login_state(state: str) -> dict:
    with connect() as conn:
        row = conn.execute(
            """
            select state, nonce, code_verifier, next_path, expires_at
            from auth_login_states
            where state = %s
            """,
            (state,),
        ).fetchone()
        conn.execute("delete from auth_login_states where state = %s or expires_at <= now()", (state,))
    if not row or row[4] <= datetime.now(timezone.utc):
        raise RuntimeError("Login session expired. Please try again.")
    return {"state": row[0], "nonce": row[1], "codeVerifier": row[2], "nextPath": row[3]}


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def exchange_oidc_code(code: str, login_state: dict, headers) -> tuple[str, dict]:
    settings = get_auth_settings()
    if not settings.get("oidcEnabled"):
        raise RuntimeError("Keycloak login is not enabled")
    discovery = oidc_discovery(settings)
    form = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": settings.get("clientId", ""),
            "client_secret": settings.get("clientSecret", ""),
            "code": code,
            "redirect_uri": oidc_redirect_uri(headers),
            "code_verifier": login_state["codeVerifier"],
        }
    ).encode("utf-8")
    token_data = fetch_json_url(
        discovery["token_endpoint"],
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=form,
    )
    userinfo = fetch_json_url(
        discovery["userinfo_endpoint"],
        headers={"Authorization": f"Bearer {token_data.get('access_token', '')}"},
    )
    email = userinfo.get("email") or userinfo.get("preferred_username") or userinfo.get("sub") or ""
    if not oidc_allowed_email(email, settings):
        raise RuntimeError("This account is not allowed to access the shop app")
    display_name = userinfo.get("name") or userinfo.get("preferred_username") or email
    username = userinfo.get("preferred_username") or email
    session_id = create_auth_session("keycloak", username, email, display_name)
    return session_id, {"provider": "keycloak", "username": username, "email": email, "displayName": display_name}


def get_state() -> dict:
    with connect() as conn:
        customers = conn.execute(
            "select id, name, phone, email, notes from customers order by name"
        ).fetchall()
        vehicles = conn.execute(
            """
            select id, customer_id, year, make, model, engine, vin, trim, body, source, plate, plate_state, rockauto_url
            from vehicles
            order by year desc, make, model
            """
        ).fetchall()
        orders = conn.execute(
            "select id, customer_id, vehicle_id, status, odometer, concern, updated_at from repair_orders order by updated_at desc"
        ).fetchall()
        lines = conn.execute(
            "select order_id, line_type, description, qty, rate from order_lines order by order_id, position"
        ).fetchall()
        parts_orders = conn.execute(
            "select id, supplier, part_name, cost, retail, eta, status from parts_orders order by updated_at desc"
        ).fetchall()

    vehicles_by_customer: dict[str, list[dict]] = {}
    for row in vehicles:
        vehicles_by_customer.setdefault(row[1], []).append(
            {
                "id": row[0],
                "year": row[2],
                "make": row[3],
                "model": row[4],
                "engine": row[5],
                "vin": row[6],
                "trim": row[7],
                "body": row[8],
                "source": row[9],
                "plate": row[10],
                "plateState": row[11],
                "rockAutoUrl": row[12],
            }
        )

    lines_by_order: dict[str, dict[str, list[dict]]] = {}
    for row in lines:
        order_lines = lines_by_order.setdefault(row[0], {"labor": [], "parts": []})
        bucket = "labor" if row[1] == "labor" else "parts"
        order_lines[bucket].append(
            {
                "description": row[2],
                "qty": float(row[3]),
                "rate": float(row[4]),
            }
        )

    return {
        "customers": [
            {
                "id": row[0],
                "name": row[1],
                "phone": row[2],
                "email": row[3],
                "notes": row[4],
                "vehicles": vehicles_by_customer.get(row[0], []),
            }
            for row in customers
        ],
        "orders": [
            {
                "id": row[0],
                "customerId": row[1],
                "vehicleId": row[2] or "",
                "status": normalize_order_status(row[3]),
                "odometer": row[4],
                "concern": row[5],
                "updatedAt": row[6].isoformat(),
                "labor": lines_by_order.get(row[0], {}).get("labor", []),
                "parts": lines_by_order.get(row[0], {}).get("parts", []),
            }
            for row in orders
        ],
        "partsOrders": [
            {
                "id": row[0],
                "supplier": row[1],
                "partName": row[2],
                "cost": float(row[3]),
                "retail": float(row[4]),
                "eta": row[5],
                "status": row[6],
            }
            for row in parts_orders
        ],
    }


def get_parts_providers() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            select provider_key, display_name, description, enabled, status
            from parts_providers
            order by enabled desc, display_name
            """
        ).fetchall()
    return [
        {
            "key": row[0],
            "displayName": row[1],
            "description": row[2],
            "enabled": row[3],
            "status": row[4],
        }
        for row in rows
    ]


def retail_price(cost: float) -> float:
    if cost < 25:
        return round(cost * 1.8, 2)
    if cost < 100:
        return round(cost * 1.6, 2)
    if cost < 300:
        return round(cost * 1.4, 2)
    return round(cost * 1.25, 2)


def search_parts(data: dict) -> list[dict]:
    state = get_state()
    vehicle = None
    for customer in state["customers"]:
        vehicle = next((item for item in customer.get("vehicles", []) if item["id"] == data.get("vehicleId")), None)
        if vehicle:
            break

    category = data.get("category") or "Part"
    keyword = data.get("keyword") or ""
    base = {
        "Brake Pads": 46,
        "Rotor": 58,
        "Oil Filter": 8,
        "Air Filter": 17,
        "Battery": 142,
        "Alternator": 215,
        "Starter": 188,
    }.get(category, 40)
    suppliers = [
        {"name": "Manual Supplier", "eta": "Call to confirm", "stock": 1, "factor": 1.0},
        {"name": "PartsTech staged quote", "eta": "Credentials needed", "stock": 0, "factor": 1.04},
        {"name": "Nexpart staged quote", "eta": "Credentials needed", "stock": 0, "factor": 0.96},
    ]

    vehicle_prefix = f"{vehicle.get('make', '')} {vehicle.get('model', '')} " if vehicle else ""
    quotes = []
    for index, supplier in enumerate(suppliers):
        cost = round(base * supplier["factor"] + index * 3, 2)
        quotes.append(
            {
                "id": str(uuid.uuid4()),
                "supplier": supplier["name"],
                "partName": f"{vehicle_prefix}{category}".strip(),
                "partNumber": f"{category[:3].upper()}-{str(base * 10 + index * 7).zfill(4)}",
                "description": f"{keyword} match" if keyword else "Staged replacement part quote",
                "cost": cost,
                "retail": retail_price(cost),
                "eta": supplier["eta"],
                "stock": supplier["stock"],
            }
        )
    return quotes


def get_query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or [""]
    return values[0].strip()


def fetch_nhtsa(path: str) -> dict:
    url = f"{NHTSA_API_BASE}/{path}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AutoShopManager/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"NHTSA lookup failed: {exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("NHTSA lookup returned an invalid response") from exc


def clean_vehicle_value(value: str | None) -> str:
    return "" if value in {None, "Not Applicable"} else str(value).strip()


def build_engine_label(result: dict) -> str:
    parts = []
    displacement = clean_vehicle_value(result.get("DisplacementL"))
    cylinders = clean_vehicle_value(result.get("EngineCylinders"))
    engine_model = clean_vehicle_value(result.get("EngineModel"))
    fuel_type = clean_vehicle_value(result.get("FuelTypePrimary"))

    if displacement:
        parts.append(f"{displacement}L")
    if cylinders:
        parts.append(f"{cylinders} cyl")
    if engine_model:
        parts.append(engine_model)
    if fuel_type:
        parts.append(fuel_type)
    return " ".join(parts)


def format_vehicle_make(value: str) -> str:
    cleaned = clean_vehicle_value(value)
    special_names = {"BMW", "GMC", "MINI", "RAM", "SRT"}
    if cleaned.upper() in special_names:
        return cleaned.upper()
    return cleaned.title()


def decode_vehicle_vin(query: dict[str, list[str]]) -> dict:
    vin = get_query_value(query, "vin").upper().replace(" ", "")
    model_year = get_query_value(query, "year")
    if len(vin) < 8:
        return {"ok": False, "error": "Enter at least 8 VIN characters."}

    path = f"DecodeVinValues/{quote(vin)}?format=json"
    if model_year:
        path += f"&modelyear={quote(model_year)}"
    data = fetch_nhtsa(path)
    results = data.get("Results") or []
    result = results[0] if results else {}

    warnings = []
    error_code = clean_vehicle_value(result.get("ErrorCode"))
    error_text = clean_vehicle_value(result.get("ErrorText"))
    if error_code and error_code != "0" and error_text:
        warnings.append(error_text)

    vehicle = {
        "vin": vin,
        "year": clean_vehicle_value(result.get("ModelYear")) or model_year,
        "make": format_vehicle_make(result.get("Make")),
        "model": clean_vehicle_value(result.get("Model")),
        "engine": build_engine_label(result),
        "trim": clean_vehicle_value(result.get("Trim")),
        "body": clean_vehicle_value(result.get("BodyClass")),
        "source": "NHTSA vPIC",
    }
    return {"ok": True, "vehicle": vehicle, "warnings": warnings}


def get_vehicle_models(query: dict[str, list[str]]) -> dict:
    make = get_query_value(query, "make")
    year = get_query_value(query, "year")
    if not make or not year:
        return {"ok": False, "error": "Make and year are required."}

    rows = []
    vehicle_types = ["passenger car", "truck", "multipurpose passenger vehicle"]
    for vehicle_type in vehicle_types:
        path = (
            f"GetModelsForMakeYear/make/{quote(make)}/modelyear/{quote(year)}"
            f"/vehicletype/{quote(vehicle_type)}?format=json"
        )
        rows.extend(fetch_nhtsa(path).get("Results") or [])

    if not rows:
        path = f"GetModelsForMakeYear/make/{quote(make)}/modelyear/{quote(year)}?format=json"
        rows = fetch_nhtsa(path).get("Results") or []

    models = sorted({clean_vehicle_value(row.get("Model_Name")) for row in rows if clean_vehicle_value(row.get("Model_Name"))})
    return {"ok": True, "models": models, "source": "NHTSA vPIC"}


def write_state(data: dict, existing_conn=None) -> None:
    conn_context = existing_conn if existing_conn is not None else connect()
    should_close = existing_conn is None
    try:
        conn = conn_context
        conn.execute("delete from order_lines")
        conn.execute("delete from repair_orders")
        conn.execute("delete from vehicles")
        conn.execute("delete from customers")
        conn.execute("delete from parts_orders")

        for customer in data.get("customers", []):
            customer_id = customer.get("id") or str(uuid.uuid4())
            conn.execute(
                """
                insert into customers (id, name, phone, email, notes)
                values (%s, %s, %s, %s, %s)
                """,
                (
                    customer_id,
                    customer.get("name") or "Unnamed Customer",
                    customer.get("phone") or "",
                    customer.get("email") or "",
                    customer.get("notes") or "",
                ),
            )
            for vehicle in customer.get("vehicles", []):
                conn.execute(
                    """
                    insert into vehicles (
                        id, customer_id, year, make, model, engine, vin, trim, body, source, plate, plate_state, rockauto_url
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        vehicle.get("id") or str(uuid.uuid4()),
                        customer_id,
                        vehicle.get("year") or "",
                        vehicle.get("make") or "",
                        vehicle.get("model") or "",
                        vehicle.get("engine") or "",
                        vehicle.get("vin") or "",
                        vehicle.get("trim") or "",
                        vehicle.get("body") or "",
                        vehicle.get("source") or "",
                        vehicle.get("plate") or "",
                        vehicle.get("plateState") or vehicle.get("plate_state") or "",
                        vehicle.get("rockAutoUrl") or vehicle.get("rockauto_url") or "",
                    ),
                )

        for order in data.get("orders", []):
            order_id = order.get("id") or str(uuid.uuid4())
            conn.execute(
                """
                insert into repair_orders (id, customer_id, vehicle_id, status, odometer, concern, updated_at)
                values (%s, %s, nullif(%s, ''), %s, %s, %s, now())
                """,
                (
                    order_id,
                    order.get("customerId"),
                    order.get("vehicleId") or "",
                    normalize_order_status(order.get("status")),
                    order.get("odometer") or "",
                    order.get("concern") or "",
                ),
            )
            insert_lines(conn, order_id, "labor", order.get("labor", []))
            insert_lines(conn, order_id, "part", order.get("parts", []))

        for part_order in data.get("partsOrders", []):
            conn.execute(
                """
                insert into parts_orders (id, supplier, part_name, cost, retail, eta, status)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    part_order.get("id") or str(uuid.uuid4()),
                    part_order.get("supplier") or "",
                    part_order.get("partName") or "",
                    part_order.get("cost") or 0,
                    part_order.get("retail") or 0,
                    part_order.get("eta") or "",
                    part_order.get("status") or "Quoted",
                ),
            )
        if should_close:
            conn.commit()
    finally:
        if should_close:
            conn.close()
    write_backup(data)


def insert_lines(conn, order_id: str, line_type: str, lines: list[dict]) -> None:
    for index, line in enumerate(lines):
        conn.execute(
            """
            insert into order_lines (id, order_id, line_type, description, qty, rate, position)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                order_id,
                line_type,
                line.get("description") or "",
                line.get("qty") or 0,
                line.get("rate") or 0,
                index,
            ),
        )


def write_backup(data: dict) -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (APP_DATA_DIR / "state-backup.json").write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


def delete_order(order_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("delete from repair_orders where id = %s", (order_id,))
        return cursor.rowcount > 0


def order_total(order: dict) -> dict:
    subtotal = sum(float(line.get("qty") or 0) * float(line.get("rate") or 0) for line in order.get("labor", []) + order.get("parts", []))
    parts_subtotal = sum(float(line.get("qty") or 0) * float(line.get("rate") or 0) for line in order.get("parts", []))
    tax = round_currency(parts_subtotal * TAX_RATE)
    return {"subtotal": round_currency(subtotal), "tax": tax, "total": round_currency(subtotal + tax)}


def round_currency(value: float) -> float:
    return math.floor((value * 100) + 0.5) / 100


def format_money(value: float) -> str:
    return f"${value:,.2f}"


def find_order(order_id: str) -> tuple[dict, dict, dict | None]:
    state = get_state()
    order = next((item for item in state["orders"] if item["id"] == order_id), None)
    if not order:
        raise ValueError("Order not found")
    customer = next((item for item in state["customers"] if item["id"] == order["customerId"]), None)
    vehicle = None
    if customer:
        vehicle = next((item for item in customer.get("vehicles", []) if item["id"] == order.get("vehicleId")), None)
    if not customer:
        raise ValueError("Customer not found")
    return order, customer, vehicle


def build_estimate_email(order_id: str, to_address: str | None = None) -> EmailMessage:
    order, customer, vehicle = find_order(order_id)
    recipient = to_address or customer.get("email")
    if not recipient:
        raise ValueError("Customer does not have an email address")

    total = order_total(order)
    vehicle_label = " ".join(str(vehicle.get(key, "")).strip() for key in ("year", "make", "model", "engine")).strip() if vehicle else "your vehicle"
    lines = [
        f"Hi {customer.get('name', '').strip() or 'there'},",
        "",
        f"Here is the current {normalize_order_status(order.get('status'))} for {vehicle_label}.",
        "",
        f"Requested work: {order.get('concern') or 'Not specified'}",
        "",
        "Labor:",
    ]
    lines.extend(format_email_lines(order.get("labor", [])))
    lines.append("")
    lines.append("Parts:")
    lines.extend(format_email_lines(order.get("parts", [])))
    lines.extend(
        [
            "",
            f"Subtotal: {format_money(total['subtotal'])}",
            f"Tax: {format_money(total['tax'])}",
            f"Total: {format_money(total['total'])}",
            "",
            "Reply to this email if you have any questions.",
        ]
    )

    msg = EmailMessage()
    msg["To"] = recipient
    msg["From"] = os.environ.get("SMTP_FROM", "")
    msg["Subject"] = f"{normalize_order_status(order.get('status'))} for {vehicle_label}"
    msg.set_content("\n".join(lines))
    return msg


def format_email_lines(lines: list[dict]) -> list[str]:
    if not lines:
        return ["  None"]
    return [
        f"  {line.get('description') or 'Line item'} - {line.get('qty') or 0} x {format_money(float(line.get('rate') or 0))}"
        for line in lines
    ]


def send_email(msg: EmailMessage) -> None:
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    use_tls = os.environ.get("SMTP_TLS", "true").lower() == "true"
    if not host or not msg["From"]:
        raise RuntimeError("SMTP_HOST and SMTP_FROM must be configured")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
        return

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def current_session_id(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def current_user(self) -> dict | None:
        if not auth_is_enabled():
            return {"provider": "disabled", "username": "local", "displayName": "Local Access"}
        return find_auth_session(self.current_session_id())

    def wants_json(self) -> bool:
        return self.path.startswith("/api/")

    def session_cookie(self, session_id: str) -> str:
        parts = [
            f"{SESSION_COOKIE}={session_id}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={SESSION_SECONDS}",
        ]
        if public_url_from_headers(self.headers).startswith("https://"):
            parts.append("Secure")
        return "; ".join(parts)

    def expired_session_cookie(self) -> str:
        return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"

    def send_redirect(self, location: str, cookie_header: str | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        if cookie_header:
            self.send_header("Set-Cookie", cookie_header)
        self.end_headers()

    def is_public_get(self, path: str) -> bool:
        return path in {
            "/login",
            "/login.html",
            "/login.js",
            "/styles.css",
            "/api/health",
            "/api/auth/public",
            "/auth/keycloak",
            "/oauth2/callback",
            "/favicon.ico",
        }

    def ensure_authenticated(self, path: str) -> bool:
        if not auth_is_enabled():
            return True
        if self.current_user():
            return True
        if self.wants_json():
            self.send_json({"ok": False, "error": "Authentication required"}, status=401)
        else:
            next_path = safe_next_path(self.path)
            self.send_redirect(f"/login?next={quote(next_path)}")
        return False

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)

        if path == "/logout":
            delete_auth_session(self.current_session_id())
            self.send_redirect("/login", self.expired_session_cookie())
            return

        if path == "/login" or path == "/login.html":
            if auth_is_enabled() and self.current_user():
                self.send_redirect(safe_next_path(get_query_value(query, "next")))
                return
            self.path = "/login.html"
            super().do_GET()
            return

        if path == "/api/auth/public":
            settings = get_auth_settings(include_secret=False)
            self.send_json(
                {
                    "localEnabled": bool(settings.get("localEnabled")),
                    "localUserConfigured": bool(settings.get("localUserConfigured")),
                    "oidcEnabled": bool(settings.get("oidcEnabled")),
                    "authEnabled": auth_is_enabled(),
                }
            )
            return

        if path == "/api/auth/me":
            user = self.current_user()
            if not user:
                self.send_json({"ok": False, "error": "Authentication required"}, status=401)
                return
            self.send_json({"ok": True, "user": {key: value for key, value in user.items() if key != "id"}})
            return

        if path == "/auth/keycloak":
            try:
                settings = get_auth_settings()
                discovery = oidc_discovery(settings)
                state = secrets.token_urlsafe(32)
                nonce = secrets.token_urlsafe(32)
                verifier = secrets.token_urlsafe(64)
                save_oidc_login_state(state, nonce, verifier, get_query_value(query, "next") or "/")
                params = urlencode(
                    {
                        "response_type": "code",
                        "client_id": settings.get("clientId", ""),
                        "redirect_uri": oidc_redirect_uri(self.headers),
                        "scope": "openid email profile",
                        "state": state,
                        "nonce": nonce,
                        "code_challenge": code_challenge(verifier),
                        "code_challenge_method": "S256",
                    }
                )
                self.send_redirect(f"{discovery['authorization_endpoint']}?{params}")
            except RuntimeError as exc:
                self.send_redirect(f"/login?error={quote(str(exc))}")
            return

        if path == "/oauth2/callback":
            try:
                if get_query_value(query, "error"):
                    raise RuntimeError(get_query_value(query, "error_description") or get_query_value(query, "error"))
                login_state = pop_oidc_login_state(get_query_value(query, "state"))
                session_id, _user = exchange_oidc_code(get_query_value(query, "code"), login_state, self.headers)
                self.send_redirect(login_state["nextPath"], self.session_cookie(session_id))
            except RuntimeError as exc:
                self.send_redirect(f"/login?error={quote(str(exc))}")
            return

        if not self.is_public_get(path) and not self.ensure_authenticated(path):
            return

        if path == "/api/health":
            self.send_json({"ok": True, "emailConfigured": bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))})
            return
        if path == "/api/auth/settings":
            self.send_json({"ok": True, "settings": get_auth_settings(include_secret=False)})
            return
        if path == "/api/state":
            self.send_json(get_state())
            return
        if path == "/api/parts/providers":
            self.send_json({"providers": get_parts_providers()})
            return
        if path == "/api/vehicles/decode-vin":
            try:
                self.send_json(decode_vehicle_vin(query))
            except RuntimeError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=502)
            return
        if path == "/api/vehicles/models":
            try:
                self.send_json(get_vehicle_models(query))
            except RuntimeError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=502)
            return
        super().do_GET()

    def do_PUT(self):
        path = urlparse(self.path).path
        if not self.ensure_authenticated(path):
            return
        if path == "/api/auth/settings":
            data = self.read_json()
            self.send_json({"ok": True, "settings": update_auth_settings(data)})
            return
        if path != "/api/state":
            self.send_error(404)
            return

        data = self.read_json()
        if not isinstance(data, dict):
            self.send_error(400, "State must be an object")
            return

        write_state(data)
        self.send_json({"ok": True})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self.ensure_authenticated(path):
            return
        prefix = "/api/orders/"
        if not path.startswith(prefix):
            self.send_error(404)
            return

        order_id = unquote(path[len(prefix):])
        if not order_id:
            self.send_json({"ok": False, "error": "Order id is required"}, status=400)
            return

        if not delete_order(order_id):
            self.send_json({"ok": False, "error": "Order not found"}, status=404)
            return

        self.send_json({"ok": True})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/auth/login":
            data = self.read_json()
            try:
                session_id, user = local_login(data)
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=401)
                return
            self.send_json(
                {"ok": True, "user": user, "next": safe_next_path(data.get("next"))},
                headers={"Set-Cookie": self.session_cookie(session_id)},
            )
            return
        if not self.ensure_authenticated(path):
            return
        if path != "/api/email/estimate":
            if path == "/api/parts/search":
                data = self.read_json()
                self.send_json({"quotes": search_parts(data)})
                return
            self.send_error(404)
            return
        data = self.read_json()
        try:
            msg = build_estimate_email(data.get("orderId"), data.get("to"))
            send_email(msg)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        except RuntimeError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=503)
            return
        self.send_json({"ok": True})

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        try:
            return json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return {}

    def send_json(self, data: dict, status: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8787"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Auto Shop Manager listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()
