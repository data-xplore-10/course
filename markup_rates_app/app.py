"""Customer markup rates — a small, dependency-free example app.

Each customer has a default markup percentage, plus optional per-category
overrides. A quote endpoint resolves the effective rate for a customer and
product category and applies it to a unit cost.

Run:   python app.py            (serves http://localhost:8000)
Test:  python -m unittest test_app.py
"""

import json
import os
import re
import sqlite3
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_DB = Path(__file__).parent / "markup_rates.db"

CATEGORIES = ["Hardware", "Software", "Services", "Shipping"]
MIN_MARKUP = Decimal("0")
MAX_MARKUP = Decimal("500")
CENT = Decimal("0.01")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    default_markup TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS category_rates (
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    category    TEXT NOT NULL,
    markup      TEXT NOT NULL,
    PRIMARY KEY (customer_id, category)
);
"""

SEED = [
    ("Acme Corp", "25", {"Hardware": "18", "Services": "40"}),
    ("Globex", "30", {"Shipping": "5"}),
    ("Initech", "20", {}),
]


class ValidationError(Exception):
    pass


class NotFound(Exception):
    pass


# --------------------------------------------------------------------------
# Data layer
# --------------------------------------------------------------------------


class Store:
    def __init__(self, path=DEFAULT_DB, seed=True):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        if seed and not self.conn.execute("SELECT 1 FROM customers").fetchone():
            for name, default, overrides in SEED:
                cid = self.create_customer(name, default)["id"]
                for category, markup in overrides.items():
                    self.set_category_rate(cid, category, markup)

    # -- customers ---------------------------------------------------------

    def list_customers(self):
        rows = self.conn.execute("SELECT * FROM customers ORDER BY name").fetchall()
        return [self._customer_dict(r) for r in rows]

    def get_customer(self, customer_id):
        row = self.conn.execute(
            "SELECT * FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"Customer {customer_id} not found")
        return self._customer_dict(row)

    def create_customer(self, name, default_markup):
        name = _clean_name(name)
        markup = parse_markup(default_markup)
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO customers (name, default_markup) VALUES (?, ?)",
                    (name, str(markup)),
                )
        except sqlite3.IntegrityError:
            raise ValidationError(f"A customer named '{name}' already exists")
        return self.get_customer(cur.lastrowid)

    def update_customer(self, customer_id, name=None, default_markup=None):
        current = self.get_customer(customer_id)
        name = _clean_name(name) if name is not None else current["name"]
        markup = (
            parse_markup(default_markup)
            if default_markup is not None
            else Decimal(current["default_markup"])
        )
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE customers SET name = ?, default_markup = ? WHERE id = ?",
                    (name, str(markup), customer_id),
                )
        except sqlite3.IntegrityError:
            raise ValidationError(f"A customer named '{name}' already exists")
        return self.get_customer(customer_id)

    def delete_customer(self, customer_id):
        self.get_customer(customer_id)
        with self.conn:
            self.conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))

    # -- category overrides ------------------------------------------------

    def set_category_rate(self, customer_id, category, markup):
        self.get_customer(customer_id)
        category = _check_category(category)
        markup = parse_markup(markup)
        with self.conn:
            self.conn.execute(
                "INSERT INTO category_rates (customer_id, category, markup) "
                "VALUES (?, ?, ?) ON CONFLICT (customer_id, category) "
                "DO UPDATE SET markup = excluded.markup",
                (customer_id, category, str(markup)),
            )
        return self.get_customer(customer_id)

    def clear_category_rate(self, customer_id, category):
        self.get_customer(customer_id)
        category = _check_category(category)
        with self.conn:
            self.conn.execute(
                "DELETE FROM category_rates WHERE customer_id = ? AND category = ?",
                (customer_id, category),
            )
        return self.get_customer(customer_id)

    # -- pricing -----------------------------------------------------------

    def quote(self, customer_id, category, cost):
        customer = self.get_customer(customer_id)
        category = _check_category(category)
        try:
            cost = Decimal(str(cost))
        except InvalidOperation:
            raise ValidationError("Cost must be a number")
        if not cost.is_finite() or cost < 0:
            raise ValidationError("Cost must be zero or greater")

        override = customer["overrides"].get(category)
        markup = Decimal(override if override is not None else customer["default_markup"])
        price = (cost * (1 + markup / 100)).quantize(CENT, ROUND_HALF_UP)
        return {
            "customer": customer["name"],
            "category": category,
            "cost": str(cost.quantize(CENT, ROUND_HALF_UP)),
            "markup": str(markup),
            "source": "category override" if override is not None else "customer default",
            "price": str(price),
            "margin": str((price - cost).quantize(CENT, ROUND_HALF_UP)),
        }

    # -- helpers -----------------------------------------------------------

    def _customer_dict(self, row):
        overrides = {
            r["category"]: r["markup"]
            for r in self.conn.execute(
                "SELECT category, markup FROM category_rates WHERE customer_id = ?",
                (row["id"],),
            )
        }
        return {
            "id": row["id"],
            "name": row["name"],
            "default_markup": row["default_markup"],
            "overrides": overrides,
        }


def parse_markup(value):
    try:
        markup = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise ValidationError("Markup must be a number")
    if not markup.is_finite():
        raise ValidationError("Markup must be a number")
    if not MIN_MARKUP <= markup <= MAX_MARKUP:
        raise ValidationError(f"Markup must be between {MIN_MARKUP}% and {MAX_MARKUP}%")
    # Store at most two decimal places, normalised (e.g. "25.50" -> "25.5").
    markup = markup.quantize(CENT, ROUND_HALF_UP).normalize()
    return markup.quantize(Decimal(1)) if markup == markup.to_integral() else markup


def _clean_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("Customer name is required")
    return name.strip()


def _check_category(category):
    if category not in CATEGORIES:
        raise ValidationError(f"Unknown category '{category}'")
    return category


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

ROUTES = [
    ("GET", r"/api/categories", "categories"),
    ("GET", r"/api/customers", "list_customers"),
    ("POST", r"/api/customers", "create_customer"),
    ("PUT", r"/api/customers/(\d+)", "update_customer"),
    ("DELETE", r"/api/customers/(\d+)", "delete_customer"),
    ("PUT", r"/api/customers/(\d+)/rates/([^/]+)", "set_rate"),
    ("DELETE", r"/api/customers/(\d+)/rates/([^/]+)", "clear_rate"),
    ("GET", r"/api/quote", "quote"),
]


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def log_message(self, fmt, *args):
            if os.environ.get("QUIET") != "1":
                super().log_message(fmt, *args)

        # -- routing -------------------------------------------------------

        def _dispatch(self, method):
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send_file(STATIC_DIR / "index.html")
            for route_method, pattern, action in ROUTES:
                match = re.fullmatch(pattern, url.path)
                if match and route_method == method:
                    try:
                        status, body = getattr(self, action)(
                            *match.groups(), query=parse_qs(url.query)
                        )
                    except ValidationError as e:
                        status, body = 400, {"error": str(e)}
                    except NotFound as e:
                        status, body = 404, {"error": str(e)}
                    return self._send_json(status, body)
            self._send_json(404, {"error": "Not found"})

        # -- actions -------------------------------------------------------

        def categories(self, query):
            return 200, CATEGORIES

        def list_customers(self, query):
            return 200, store.list_customers()

        def create_customer(self, query):
            data = self._json_body()
            return 201, store.create_customer(data.get("name"), data.get("default_markup"))

        def update_customer(self, cid, query):
            data = self._json_body()
            return 200, store.update_customer(
                int(cid), data.get("name"), data.get("default_markup")
            )

        def delete_customer(self, cid, query):
            store.delete_customer(int(cid))
            return 200, {"deleted": int(cid)}

        def set_rate(self, cid, category, query):
            return 200, store.set_category_rate(
                int(cid), unquote(category), self._json_body().get("markup")
            )

        def clear_rate(self, cid, category, query):
            return 200, store.clear_category_rate(int(cid), unquote(category))

        def quote(self, query):
            def arg(key):
                if key not in query:
                    raise ValidationError(f"Missing query parameter '{key}'")
                return query[key][0]

            try:
                cid = int(arg("customer_id"))
            except ValueError:
                raise ValidationError("customer_id must be an integer")
            return 200, store.quote(cid, arg("category"), arg("cost"))

        # -- I/O helpers ---------------------------------------------------

        def _json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                raise ValidationError("Request body must be valid JSON")
            if not isinstance(data, dict):
                raise ValidationError("Request body must be a JSON object")
            return data

        def _send_json(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_file(self, path):
            payload = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main():
    port = int(os.environ.get("PORT", 8000))
    store = Store(os.environ.get("MARKUP_DB", DEFAULT_DB))
    # Single-threaded on purpose: one SQLite connection, no locking needed.
    server = HTTPServer(("0.0.0.0", port), make_handler(store))
    print(f"Markup rates app running on http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
