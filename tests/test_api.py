"""HTTP and queue contracts using test doubles; no browser or merchant traffic."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer

from app import Application, make_handler
from contracts import NeedsAction, StockSnapshot
from engine import Engine
from store import Store


class ReadOnlyTestAdapter:
    checkout_ready = False

    def __init__(self):
        self.logins = []
        self.openings = []
        self.resumes = []
        self.preparations = []
        self.discoveries = []
        self.discovery_error = None
        self.discovery_started = threading.Event()
        self.discovery_release = threading.Event()
        self.discovery_release.set()

    def inspect(self, product):
        return StockSnapshot(True, 1_000_000, product["name"], product["variant"])

    def discover(self, product):
        self.discoveries.append(dict(product))
        self.discovery_started.set()
        if not self.discovery_release.wait(timeout=3):
            raise NeedsAction("Test discovery release timed out")
        if self.discovery_error:
            raise self.discovery_error
        return {"name": "Fixture phone", "url": product["url"], "price": 1_000_000,
                "available": True, "selected_color": "Blue", "selected_capacity": "256 GB",
                "colors": [{"value": "Blue", "selected": True, "available": True}],
                "capacities": [{"value": "256 GB", "selected": True, "available": True}]}

    def open_login(self, profile_id):
        self.logins.append(profile_id)
        return "Test login opening requested"

    def open_product(self, product):
        self.openings.append(dict(product))
        return {"note": "Test product opened"}

    def resume_purchase(self, product):
        self.resumes.append(dict(product))
        return {"note": "Existing test preparation resumed"}

    def prepare_purchase(self, product, snapshot):
        self.preparations.append(dict(product))
        return {"product_url": product["url"], "note": "Test browser preparation; no merchant order"}

    def checkout(self, *args):
        raise AssertionError("Automatic checkout is unavailable in this test adapter")

    def close(self):
        pass


class APITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "test.sqlite3")
        self.adapter = ReadOnlyTestAdapter()
        self.engine = Engine(self.store, self.adapter)
        self.app = Application(self.store, self.engine)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.engine.start_thread()

    def tearDown(self):
        self.adapter.discovery_release.set()
        self.engine.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, path, data=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        supplied = {}
        if data is not None:
            supplied = {"Content-Type": "application/json", "X-CSRF-Token": self.app.csrf}
        supplied.update(headers or {})
        client.request("POST" if data is not None else "GET", path,
                       json.dumps(data) if data is not None else None, supplied)
        result = client.getresponse()
        output = result.status, result.read()
        client.close()
        return output

    def state(self):
        status, body = self.request("/api/state")
        self.assertEqual(status, 200)
        return json.loads(body)

    def wait_state(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = self.state()
            if predicate(state):
                return state
            time.sleep(.01)
        self.fail("Worker did not finish within local test deadline")

    def product(self, **overrides):
        values = {"profile_id": 1, "name": "Fixture phone", "url": "https://ibox.co.id/product/fixture-phone",
                  "color": "Blue", "capacity": "256 GB", "quantity": 1, "max_unit_price": 1_500_000,
                  "max_total_price": 1_600_000, "priority": 1, "active": False, "action": "notify"}
        values.update(overrides)
        return values

    def add_product(self, **overrides):
        code, raw = self.request("/api/products", self.product(**overrides))
        self.assertEqual(code, 200, raw)
        return json.loads(raw)["id"]

    def test_dashboard_assets_and_empty_live_state(self):
        for path in ("/", "/styles.css", "/app.js"):
            with self.subTest(path=path):
                status, content = self.request(path)
                self.assertEqual(status, 200)
                self.assertTrue(content)
        state = self.state()
        self.assertEqual(state["mode"], "live")
        self.assertEqual(state["products"], [])
        self.assertEqual(state["orders"], [])
        self.assertEqual(state["profiles"][0]["budget"], 0)
        self.assertFalse(state["monitor"]["running"])
        self.assertFalse(state["capabilities"]["checkout"])
        self.assertTrue(state["capabilities"]["inspect"])
        self.assertTrue(state["login_ready"])
        self.assertEqual(state["connection"]["status"], "not_checked")
        self.assertEqual(self.request("/../../app.py")[0], 404)
        self.assertEqual(self.request("/demo/pay/FAKE")[0], 404)

    def test_cross_origin_and_invalid_csrf_rejected(self):
        for headers in ({"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": "ÿ"},
                        {"Origin": "https://untrusted.invalid"}, {"Host": "untrusted.invalid"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                code, body = self.request("/api/monitor/check", {}, headers)
                self.assertEqual(code, 403)
                self.assertIn("error", json.loads(body))
        self.assertEqual(self.state()["orders"], [])

    def test_profile_product_validation_and_crud(self):
        code, raw = self.request("/api/profiles", {"name": "Pribadi", "budget": 5_000_000})
        self.assertEqual(code, 200)
        profile_id = json.loads(raw)["id"]
        product = self.product(profile_id=profile_id)
        product_id = self.add_product(profile_id=profile_id)
        self.assertEqual(self.request("/api/products", product)[0], 400)
        self.assertEqual(self.request("/api/products", dict(product, quantity=101))[0], 400)
        self.assertEqual(self.request("/api/products", [product])[0], 400)
        self.assertEqual(self.request(f"/api/products/{product_id}/toggle", {"active": True})[0], 200)
        self.assertEqual(self.request(f"/api/products/{product_id}", dict(product, color="White"))[0], 200)
        saved = next(row for row in self.state()["products"] if row["id"] == product_id)
        self.assertEqual(saved["color"], "White")
        self.assertEqual(saved["capacity"], "256 GB")
        self.assertEqual(self.request(f"/api/products/{product_id}/delete", {})[0], 200)
        self.assertNotIn(product_id, [row["id"] for row in self.state()["products"]])

    def test_real_product_url_required_and_automatic_checkout_gated(self):
        for url in ("https://demo.invalid/item", "http://ibox.co.id/product/item", "https://ibox.com/product/item",
                    "https://example.com/product/item", "https://ibox.co.id/akun/dashboard"):
            with self.subTest(url=url):
                self.assertEqual(self.request("/api/products", self.product(url=url))[0], 400)
                self.assertEqual(self.request("/api/products/discover", {"url": url, "profile_id": 1})[0], 400)
        self.assertEqual(self.request("/api/products", self.product(action="auto"))[0], 400)
        self.assertEqual(self.state()["products"], [])
        self.assertEqual(self.state()["orders"], [])

    def test_login_works_without_checkout_mapping_and_does_not_claim_connection(self):
        self.assertFalse(self.state()["capabilities"]["checkout"])
        self.assertEqual(self.request("/api/login", {"profile_id": 1})[0], 200)
        state = self.wait_state(lambda state: self.adapter.logins == [1] and not state["monitor"]["busy"])
        self.assertEqual(state["connection"]["status"], "not_checked")
        self.assertEqual(state["orders"], [])
        self.assertEqual(self.request("/api/login", {"profile_id": 999})[0], 400)

    def test_discovery_is_async_read_only_and_does_not_set_budget(self):
        self.adapter.discovery_release.clear()
        payload = {"url": "https://ibox.co.id/product/fixture-discover", "profile_id": 1}
        code, raw = self.request("/api/products/discover", payload)
        self.assertEqual(code, 200)
        request_id = json.loads(raw)["request_id"]
        self.assertTrue(self.adapter.discovery_started.wait(timeout=2))
        state = self.state()
        self.assertEqual(state["discovery"]["request_id"], request_id)
        self.assertTrue(state["discovery"]["busy"])
        self.assertIsNone(state["discovery"]["result"])
        self.assertEqual(self.request("/api/products/discover", payload)[0], 400)
        self.adapter.discovery_release.set()
        state = self.wait_state(lambda state: not state["discovery"]["busy"])
        self.assertEqual(state["discovery"]["result"]["price"], 1_000_000)
        self.assertEqual(state["discovery"]["profile_id"], 1)
        self.assertEqual(state["discovery"]["url"], payload["url"])
        self.assertEqual(state["connection"]["status"], "connected")
        self.assertEqual(state["products"], [])
        self.assertEqual(state["orders"], [])
        self.assertEqual(state["profiles"][0]["budget"], 0)
        self.assertEqual(state["profiles"][0]["committed"], 0)

    def test_discovery_failure_exposes_error_without_fabricated_result(self):
        payload = {"url": "https://ibox.co.id/product/fixture-discover", "profile_id": 1}
        self.assertEqual(self.request("/api/products/discover", payload)[0], 200)
        previous = self.wait_state(lambda state: state["discovery"]["result"] is not None)
        self.adapter.discovery_error = NeedsAction("HTTP 403 requires user action")
        code, raw = self.request("/api/products/discover", payload)
        self.assertEqual(code, 200)
        request_id = json.loads(raw)["request_id"]
        self.assertNotEqual(request_id, previous["discovery"]["request_id"])
        failed = self.wait_state(lambda state: state["discovery"]["request_id"] == request_id and not state["discovery"]["busy"])
        self.assertIsNone(failed["discovery"]["result"])
        self.assertIn("HTTP 403", failed["discovery"]["error"])
        self.assertEqual(failed["connection"]["status"], "needs_action")
        self.assertEqual(failed["orders"], [])

    def test_notify_check_reports_stock_without_reservation_at_zero_budget(self):
        product_id = self.add_product(active=True)
        self.assertEqual(self.request("/api/monitor/check", {})[0], 200)
        state = self.wait_state(lambda state: any(row["id"] == product_id and row["last_checked"] for row in state["products"]))
        self.assertEqual(state["products"][0]["status"], "available")
        self.assertEqual(state["orders"], [])
        self.assertEqual(state["profiles"][0]["committed"], 0)
        # Manual button presses respect the same live polling interval.
        self.assertEqual(self.request("/api/monitor/check", {})[0], 400)

    def test_assisted_checkout_resumes_existing_flow_and_reconciles_locally(self):
        self.assertEqual(self.request("/api/budget", {"profile_id": 1, "budget": 2_000_000})[0], 200)
        product_id = self.add_product(action="assist", active=True)
        self.assertEqual(self.request("/api/monitor/check", {})[0], 200)
        state = self.wait_state(lambda state: len(state["orders"]) == 1 and state["orders"][0]["status"] == "manual_checkout")
        order = state["orders"][0]
        self.assertEqual(order["order_ref"], "")
        self.assertEqual(order["payment_url"], "")
        self.assertIsNone(order["total"])
        self.assertFalse(state["products"][0]["active"])
        self.assertEqual(self.request(f"/api/orders/{order['id']}/open", {})[0], 200)
        self.wait_state(lambda state: len(self.adapter.resumes) == 1 and not state["monitor"]["busy"])
        self.assertEqual(self.adapter.openings, [])
        self.assertEqual(self.adapter.resumes[0]["id"], product_id)
        self.assertEqual(len(self.adapter.preparations), 1)
        self.assertEqual(self.request(f"/api/orders/{order['id']}/resolve", {
            "status": "cancelled_manual", "note": "Checked: no merchant order created"})[0], 200)
        state = self.state()
        self.assertEqual(state["orders"][0]["status"], "cancelled_manual")
        self.assertEqual(state["profiles"][0]["committed"], 0)
        self.assertFalse(state["products"][0]["active"])
        self.assertEqual(self.request(f"/api/orders/{order['id']}/open", {})[0], 400)

    def test_open_product_uses_separate_command_without_creating_checkout(self):
        product_id = self.add_product()
        self.assertEqual(self.request(f"/api/products/{product_id}/open", {})[0], 200)
        state = self.wait_state(lambda state: len(self.adapter.openings) == 1 and not state["monitor"]["busy"])
        self.assertEqual(self.adapter.openings[0]["id"], product_id)
        self.assertEqual(self.adapter.resumes, [])
        self.assertEqual(self.adapter.preparations, [])
        self.assertEqual(state["orders"], [])
        self.assertEqual(self.request("/api/products/999/open", {})[0], 400)


if __name__ == "__main__":
    unittest.main()
