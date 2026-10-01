import json
import threading
import unittest
from decimal import Decimal
from http.server import HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Store, ValidationError, make_handler, parse_markup


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", seed=False)
        self.cid = self.store.create_customer("Acme", "25")["id"]

    def test_quote_uses_default_when_no_override(self):
        q = self.store.quote(self.cid, "Hardware", "100")
        self.assertEqual(q["price"], "125.00")
        self.assertEqual(q["source"], "customer default")

    def test_quote_uses_category_override(self):
        self.store.set_category_rate(self.cid, "Hardware", "10")
        q = self.store.quote(self.cid, "Hardware", "80")
        self.assertEqual((q["markup"], q["price"], q["margin"]), ("10", "88.00", "8.00"))
        self.assertEqual(q["source"], "category override")
        # Other categories still use the default.
        self.assertEqual(self.store.quote(self.cid, "Software", "80")["price"], "100.00")

    def test_clearing_override_falls_back_to_default(self):
        self.store.set_category_rate(self.cid, "Services", "50")
        self.store.clear_category_rate(self.cid, "Services")
        self.assertEqual(self.store.get_customer(self.cid)["overrides"], {})

    def test_price_rounds_half_up_to_cents(self):
        self.store.update_customer(self.cid, default_markup="12.5")
        self.assertEqual(self.store.quote(self.cid, "Software", "0.99")["price"], "1.11")

    def test_markup_validation(self):
        self.assertEqual(parse_markup("25.50"), Decimal("25.5"))
        self.assertEqual(str(parse_markup("30.00")), "30")
        for bad in ["-1", "501", "abc", "", None, "nan", "inf"]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                parse_markup(bad)

    def test_duplicate_name_rejected(self):
        with self.assertRaises(ValidationError):
            self.store.create_customer("Acme", "10")

    def test_unknown_category_rejected(self):
        with self.assertRaises(ValidationError):
            self.store.set_category_rate(self.cid, "Food", "10")

    def test_delete_cascades_overrides(self):
        self.store.set_category_rate(self.cid, "Hardware", "10")
        self.store.delete_customer(self.cid)
        rows = self.store.conn.execute("SELECT * FROM category_rates").fetchall()
        self.assertEqual(rows, [])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), make_handler(Store(":memory:")))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req) as res:
                return res.status, json.loads(res.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_end_to_end(self):
        status, cust = self.call("POST", "/api/customers", {"name": "Wayne", "default_markup": "40"})
        self.assertEqual(status, 201)
        status, cust = self.call("PUT", f"/api/customers/{cust['id']}/rates/Shipping", {"markup": "0"})
        self.assertEqual((status, cust["overrides"]), (200, {"Shipping": "0"}))
        status, q = self.call("GET", f"/api/quote?customer_id={cust['id']}&category=Shipping&cost=12")
        self.assertEqual((status, q["price"]), (200, "12.00"))
        status, q = self.call("GET", f"/api/quote?customer_id={cust['id']}&category=Hardware&cost=12")
        self.assertEqual(q["price"], "16.80")

    def test_errors(self):
        self.assertEqual(self.call("POST", "/api/customers", {"name": "X", "default_markup": "-5"})[0], 400)
        self.assertEqual(self.call("PUT", "/api/customers/9999", {"default_markup": "5"})[0], 404)
        self.assertEqual(self.call("GET", "/api/quote?customer_id=1")[0], 400)
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)

    def test_seed_data_present(self):
        status, customers = self.call("GET", "/api/customers")
        self.assertIn("Acme Corp", [c["name"] for c in customers])


if __name__ == "__main__":
    unittest.main()
