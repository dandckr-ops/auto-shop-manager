import json
import math
import os
import smtplib
import uuid
from email.message import EmailMessage
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import psycopg


APP_DIR = Path(__file__).resolve().parent
APP_DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/data"))
TAX_RATE = 0.08125


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
                status text not null default 'Estimate',
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
        seed_parts_providers(conn)
    migrate_blob_state()


def seed_parts_providers(conn) -> None:
    providers = [
        ("partstech", "PartsTech", "Aggregator for live supplier pricing and ordering"),
        ("nexpart", "Nexpart", "Multi-seller aftermarket catalog and ordering"),
        ("napa", "NAPA Pro", "Professional account integration placeholder"),
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
                provider_key == "manual",
                "mock-ready" if provider_key == "manual" else "needs credentials",
            ),
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


def get_state() -> dict:
    with connect() as conn:
        customers = conn.execute(
            "select id, name, phone, email, notes from customers order by name"
        ).fetchall()
        vehicles = conn.execute(
            "select id, customer_id, year, make, model, engine, vin from vehicles order by year desc, make, model"
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
                "status": row[3],
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
                    insert into vehicles (id, customer_id, year, make, model, engine, vin)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        vehicle.get("id") or str(uuid.uuid4()),
                        customer_id,
                        vehicle.get("year") or "",
                        vehicle.get("make") or "",
                        vehicle.get("model") or "",
                        vehicle.get("engine") or "",
                        vehicle.get("vin") or "",
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
                    order.get("status") or "Estimate",
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
        f"Here is the current {order.get('status', 'estimate').lower()} for {vehicle_label}.",
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
    msg["Subject"] = f"{order.get('status', 'Estimate')} for {vehicle_label}"
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

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json({"ok": True, "emailConfigured": bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))})
            return
        if path == "/api/state":
            self.send_json(get_state())
            return
        if path == "/api/parts/providers":
            self.send_json({"providers": get_parts_providers()})
            return
        super().do_GET()

    def do_PUT(self):
        path = urlparse(self.path).path
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

    def send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "8787"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Auto Shop Manager listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()
