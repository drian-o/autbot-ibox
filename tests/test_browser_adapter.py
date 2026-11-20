"""Uji adapter pada merchant lokal milik tes; tidak menghubungi iBox."""
from __future__ import annotations

import copy
import html
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_adapter import BrowserAdapter, parse_price_idr, validate_config
from contracts import AdapterError, NeedsAction


class PriceTests(unittest.TestCase):
    def test_indonesian_and_grouped_prices(self):
        for text, expected in [
            ("Rp 12.345.678", 12345678),
            ("Rp12.345.678,00", 12345678),
            ("IDR 12,345,678.00", 12345678),
            ("12500000", 12500000),
            ("Rp\u00a01.250.000", 1250000),
            ("1000,00", 1000),
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse_price_idr(text), expected)

    def test_ambiguous_or_multiple_prices_fail_closed(self):
        for text in ["Rp 100.000 Rp 90.000", "Mulai Rp 100.000", "-10", "0", "12.00.000", "1.200,50", "1e6", "NaN", "Rp 10.000 - 20.000"]:
            with self.subTest(text=text):
                self.assertRaises(AdapterError, parse_price_idr, text)

    def test_empty_configuration_is_not_ready(self):
        self.assertTrue(validate_config({}))
        self.assertTrue(validate_config(None))
        self.assertTrue(validate_config({"merchant": {"allowed_hosts": [{}]}}))

    def test_non_host_entries_and_unsupported_actions_are_rejected(self):
        config = {
            "merchant": {
                "allowed_hosts": ["https://ibox.co.id", "*.ibox.co.id"],
                "allowed_payment_hosts": [{}],
                "checkout_actions": [{"action": "evaluate", "script": "anything"}],
            }
        }
        messages = validate_config(config)
        self.assertTrue(any("allowed_hosts" in message for message in messages))
        self.assertTrue(any("allowed_payment_hosts" in message for message in messages))
        self.assertTrue(any("aksi hanya" in message for message in messages))


class MerchantHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def state(self):
        return self.server.fixture_state

    def send_html(self, content, status=200):
        data = ("<!doctype html><html><head><meta charset='utf-8'></head><body>" + content + "</body></html>").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path):
        self.send_response(303)
        self.send_header("Location", path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def item_html(self):
        item = self.state.get("cart")
        if not item:
            return ""
        variant = self.state.get("wrong_variant", item["variant"])
        quantity = self.state.get("wrong_quantity", item["quantity"])
        unit = self.state.get("unit_price", 1000000)
        row = (
            "<div class='item'>"
            "<span class='name'>Produk Demo</span>"
            f"<span class='variant'>{html.escape(variant)}</span>"
            f"<span class='quantity'>{quantity}</span>"
            f"<span class='unit-price'>Rp {unit}</span></div>"
        )
        return row * (2 if self.state.get("extra_item") else 1)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/login":
            self.send_html("<h1>Sesi tes lokal</h1>")
        elif path == "/product":
            if self.state.get("block_code"):
                self.send_html("<h1>Akses dibatasi</h1>", self.state["block_code"])
                return
            if self.state.get("challenge"):
                self.send_html("<div id='challenge'>Lakukan verifikasi</div>")
                return
            stock = "<div id='stock'>Tersedia</div>" if not self.state.get("out_of_stock") else "<div id='out'>Habis</div>"
            self.send_html(
                "<h1 id='name'>Produk Demo</h1><span id='price'>Rp 1.000.000</span>" + stock +
                "<form method='post' action='/add'>"
                "<select name='variant' id='variant'><option value='black'>Hitam 128 GB</option><option value='white'>Putih 128 GB</option></select>"
                "<input id='quantity' name='quantity' type='number' value='1' min='1' max='3'>"
                "<button id='add' type='submit'>Tambah keranjang</button></form>"
            )
        elif path == "/cart":
            rows = self.item_html()
            self.send_html(rows + ("<a id='checkout' href='/checkout'>Checkout</a>" if rows else "<div id='empty'>Kosong</div>"))
        elif path == "/checkout":
            quantity = self.state.get("cart", {}).get("quantity", 1)
            total = self.state.get("total", quantity * 1000000 + 10000)
            self.send_html(
                self.item_html() + f"<div id='total'>Rp {total}</div>"
                "<form method='post' action='/submit'><button id='submit' type='submit'>Buat pesanan</button></form>"
            )
        elif path == "/pay":
            number = parse_qs(urlsplit(self.path).query).get("ref", ["DEMO-OLD"])[0]
            self.send_html(f"<div id='order-ref'>{html.escape(number)}</div><div id='payment'>Instruksi pembayaran simulasi</div>")
        else:
            self.send_html("Tidak ditemukan", 404)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        payload = parse_qs(self.rfile.read(size).decode())
        if self.path == "/add":
            selected = "Hitam 128 GB" if payload.get("variant", [""])[0] == "black" else "Putih 128 GB"
            self.state["cart"] = {"quantity": int(payload["quantity"][0]), "variant": selected}
            self.redirect("/cart")
        elif self.path == "/submit":
            self.state["submits"] += 1
            if self.state.get("ambiguous_submit"):
                self.send_html("<h1>Pesanan sedang diproses</h1>")
            else:
                self.redirect(f"/pay?ref=DEMO-{self.state['submits']}")
        else:
            self.send_html("Tidak ditemukan", 404)


class BrowserFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import playwright.sync_api
        except ImportError:
            raise unittest.SkipTest("Playwright tidak terpasang; tes harga/config tetap berjalan.")
        cls.executable = shutil.which("chromium") or shutil.which("google-chrome")
        if cls.executable is None:
            with playwright.sync_api.sync_playwright() as manager:
                if Path(manager.chromium.executable_path).exists():
                    cls.executable = manager.chromium.executable_path
        if cls.executable is None:
            raise unittest.SkipTest("Chromium belum terpasang; jalankan python -m playwright install chromium.")
        # Beberapa runner membatasi socket proses Chromium. Lewati hanya karena
        # keterbatasan lingkungan yang dikenali; jangan menganggap alur lulus.
        try:
            with tempfile.TemporaryDirectory() as probe_dir:
                with playwright.sync_api.sync_playwright() as manager:
                    context = manager.chromium.launch_persistent_context(probe_dir, headless=True, executable_path=cls.executable)
                    context.close()
        except Exception as exc:
            reason = str(exc)
            if "Operation not permitted" in reason or "Executable doesn't exist" in reason or "error while loading shared libraries" in reason:
                raise unittest.SkipTest("Integrasi browser belum diuji: lingkungan tidak dapat menjalankan Chromium (socket/instalasi dibatasi).") from exc
            raise
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MerchantHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()
            cls.server_thread.join(timeout=2)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.server.fixture_state = {"submits": 0}
        selectors = {
            "login_required": "#login-required", "challenge": "#challenge",
            "product_name": "#name", "product_price": "#price", "in_stock": "#stock",
            "out_of_stock": "#out", "selected_variant": "#variant", "quantity_input": "#quantity",
            "add_to_cart": "#add", "cart_empty": "#empty", "cart_items": ".item",
            "cart_item_name": ".name", "cart_item_variant": ".variant", "cart_item_quantity": ".quantity",
            "cart_item_unit_price": ".unit-price", "checkout_items": ".item", "checkout_item_name": ".name",
            "checkout_item_variant": ".variant", "checkout_item_quantity": ".quantity", "checkout_item_unit_price": ".unit-price",
            "checkout_total": "#total", "submit_order": "#submit", "payment_ready": "#payment", "order_ref": "#order-ref",
        }
        self.config = {
            "allow_local_test_urls": True,
            "browser": {"headless": True, "executable_path": self.executable, "timeout_ms": 1500, "payment_timeout_ms": 1000},
            "merchant": {
                "allowed_hosts": ["127.0.0.1"], "allowed_payment_hosts": [],
                "login_url": self.origin + "/login", "cart_url": self.origin + "/cart", "selectors": selectors,
                "variants": {"Hitam 128 GB": [{"action": "select", "selector": "#variant", "value": "black"}]},
                "clear_cart_actions": [], "checkout_actions": [{"action": "click", "selector": "#checkout", "wait_for": "#total"}],
            },
        }
        self.product = {
            "id": 1, "profile_id": 1, "name": "Produk Demo", "url": self.origin + "/product",
            "variant": "Hitam 128 GB", "quantity": 1, "max_unit_price": 1000000, "max_total_price": 1050000,
            "priority": 1, "mode": "live",
        }
        self.adapter = BrowserAdapter(self.config, Path(self.directory.name))

    def tearDown(self):
        self.adapter.close()
        self.directory.cleanup()

    def test_full_flow_to_payment_has_single_pre_submit_checkpoint(self):
        self.assertEqual(validate_config(self.config), [])
        snapshot = self.adapter.inspect(self.product)
        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.unit_price, 1000000)
        callbacks = []

        def before_submit(total):
            self.assertEqual(self.server.fixture_state["submits"], 0)
            callbacks.append(total)

        result = self.adapter.checkout(self.product, snapshot, before_submit)
        self.assertEqual(result.status, "awaiting_payment")
        self.assertEqual(result.order_ref, "DEMO-1")
        self.assertEqual(result.total, 1010000)
        self.assertEqual(callbacks, [1010000])
        self.assertEqual(self.server.fixture_state["submits"], 1)

    def test_budget_overrun_stops_before_submit(self):
        self.server.fixture_state["total"] = 1100000
        snap = self.adapter.inspect(self.product)
        callbacks = []
        with self.assertRaises(NeedsAction):
            self.adapter.checkout(self.product, snap, callbacks.append)
        self.assertEqual(callbacks, [])
        self.assertEqual(self.server.fixture_state["submits"], 0)

    def test_wrong_variant_stops_before_submit(self):
        self.server.fixture_state["wrong_variant"] = "Putih 128 GB"
        snap = self.adapter.inspect(self.product)
        with self.assertRaises(NeedsAction):
            self.adapter.checkout(self.product, snap, lambda total: self.fail("Tidak boleh submit"))
        self.assertEqual(self.server.fixture_state["submits"], 0)

    def test_unit_price_drift_stops_before_submit(self):
        self.server.fixture_state["unit_price"] = 1020000
        snap = self.adapter.inspect(self.product)
        with self.assertRaises(NeedsAction):
            self.adapter.checkout(self.product, snap, lambda total: self.fail("Tidak boleh submit"))
        self.assertEqual(self.server.fixture_state["submits"], 0)

    def test_checkpoint_failure_does_not_submit(self):
        snap = self.adapter.inspect(self.product)

        def reject(total):
            raise RuntimeError("Anggaran direservasi pesanan lain")

        with self.assertRaises(RuntimeError):
            self.adapter.checkout(self.product, snap, reject)
        self.assertEqual(self.server.fixture_state["submits"], 0)

    def test_unknown_result_is_not_retried(self):
        self.server.fixture_state["ambiguous_submit"] = True
        snap = self.adapter.inspect(self.product)
        result = self.adapter.checkout(self.product, snap, lambda total: None)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(self.server.fixture_state["submits"], 1)

    def test_old_payment_tab_cannot_confirm_new_order(self):
        self.server.fixture_state["ambiguous_submit"] = True
        snap = self.adapter.inspect(self.product)
        old = self.adapter._sessions[1]["context"].new_page()
        old.goto(self.origin + "/pay?ref=DEMO-OLD")
        result = self.adapter.checkout(self.product, snap, lambda total: None)
        self.assertEqual(result.status, "unknown")
        self.assertNotEqual(result.order_ref, "DEMO-OLD")

    def test_stock_absent_does_not_trigger_checkout(self):
        self.server.fixture_state["out_of_stock"] = True
        snapshot = self.adapter.inspect(self.product)
        self.assertFalse(snapshot.available)
        self.assertIsNone(snapshot.unit_price)

    def test_http_403_pauses_instead_of_bypassing(self):
        self.server.fixture_state["block_code"] = 403
        with self.assertRaises(NeedsAction):
            self.adapter.inspect(self.product)
        self.assertEqual(self.server.fixture_state["submits"], 0)

    def test_challenge_pauses_for_user(self):
        self.server.fixture_state["challenge"] = True
        with self.assertRaises(NeedsAction):
            self.adapter.inspect(self.product)

    def test_submit_in_preparation_actions_is_rejected(self):
        config = copy.deepcopy(self.config)
        config["merchant"]["checkout_actions"] = [{"action": "click", "selector": "#submit"}]
        self.assertTrue(any("submit_order" in message for message in validate_config(config)))


if __name__ == "__main__":
    unittest.main()
