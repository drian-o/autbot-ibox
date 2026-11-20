"""Exercise the worker against offline adapters, including uncertain checkout results."""
from __future__ import annotations

import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path

from contracts import AdapterError, CheckoutResult, NeedsAction, StockSnapshot
from engine import Engine
from store import Store


class FakeAdapter:
    """Deterministic test double. Never launches a browser or contacts a merchant."""
    checkout_ready = True

    def __init__(self, snapshot=None, total=95_000, behavior="normal"):
        self.snapshot = snapshot or StockSnapshot(True, 90_000, "Phone", "Blue")
        self.total = total
        self.behavior = behavior
        self.inspections = []
        self.checkouts = []
        self.preparations = []
        self.logins = []
        self.openings = []
        self.resumes = []
        self.discovery_error = None
        self.submit_actions = 0
        self.inspect_hook = None
        self.submit_hook = None

    def inspect(self, product):
        self.inspections.append(product["id"])
        if self.inspect_hook:
            self.inspect_hook(product)
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot

    def checkout(self, product, snapshot, before_submit):
        self.checkouts.append(dict(product))
        if self.behavior == "fail_before_submit":
            raise NeedsAction("Login or cart needs review")
        if self.behavior != "missing_callback":
            before_submit(self.total)
            self.submit_actions += 1
        if self.submit_hook:
            self.submit_hook(product)
        if self.behavior == "timeout_after_submit":
            raise TimeoutError("Merchant response was lost")
        total = self.total + 1 if self.behavior == "changed_total" else self.total
        return CheckoutResult(
            "awaiting_payment", "FIXTURE-" + str(product["id"]),
            "https://ibox.co.id/payment/fixture-" + str(product["id"]), total,
            "Unit-test receipt; no merchant traffic",
        )

    def prepare_purchase(self, product, snapshot):
        self.preparations.append(dict(product))
        return {"product_url": product["url"], "note": "Test preparation; no merchant order created"}

    def discover(self, product):
        if self.discovery_error:
            raise self.discovery_error
        return {"name": "Phone", "url": product["url"], "price": 90_000, "available": True,
                "selected_color": "Blue", "selected_capacity": "256 GB", "colors": [], "capacities": []}

    def open_product(self, product):
        self.openings.append(dict(product))
        return {"note": "Test product opened"}

    def resume_purchase(self, product):
        self.resumes.append(dict(product))
        return {"note": "Existing test tab resumed"}

    def open_login(self, profile_id):
        self.logins.append(profile_id)
        return "Test login opening requested"

    def close(self):
        pass


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.store.budget({"profile_id": 1, "budget": 50_000_000})

    def product(self, slug="phone", **overrides):
        data = {
            "profile_id": 1, "name": "Phone", "url": f"https://ibox.co.id/product/fixture-{slug}",
            "variant": "Blue", "action": "auto", "quantity": 1, "max_unit_price": 90_000,
            "max_total_price": 100_000, "priority": 1, "active": True,
        }
        data.update(overrides)
        return self.store.save_product(data)

    def product_row(self, product_id):
        return next(p for p in self.store.state()["products"] if p["id"] == product_id)

    def test_success_stops_at_pending_payment_and_never_orders_target_twice(self):
        product_id = self.product()
        adapter = FakeAdapter()
        engine = Engine(self.store, adapter)

        def verify_intent_persisted(_product):
            order = self.store.state()["orders"][0]
            self.assertEqual(order["status"], "submitting")
            self.assertEqual(order["total"], 95_000)

        adapter.submit_hook = verify_intent_persisted
        engine.run_cycle()
        engine.run_cycle()
        state = self.store.state()
        self.assertEqual(len(state["orders"]), 1)
        self.assertEqual(state["orders"][0]["status"], "awaiting_payment")
        self.assertEqual(state["orders"][0]["total"], 95_000)
        self.assertEqual(state["profiles"][0]["committed"], 95_000)
        self.assertFalse(self.product_row(product_id)["active"])
        self.assertEqual(adapter.submit_actions, 1)

    def test_unavailable_and_overpriced_products_remain_watched_without_orders(self):
        for slug, snapshot, expected_status in (
            ("soldout", StockSnapshot(False, 80_000), "out_of_stock"),
            ("expensive", StockSnapshot(True, 90_001), "price_above_limit"),
        ):
            with self.subTest(slug=slug):
                product_id = self.product(slug)
                adapter = FakeAdapter(snapshot=snapshot)
                Engine(self.store, adapter).run_cycle()
                self.assertEqual(self.product_row(product_id)["status"], expected_status)
                self.assertTrue(self.product_row(product_id)["active"])
                self.assertEqual(adapter.checkouts, [])
                self.assertEqual(self.store.state()["orders"], [])
                self.store.toggle(product_id, False)

    def test_unreadable_prices_pause_without_creating_orders(self):
        for index, price in enumerate((None, True, 0, -1, 90_000.5, "90000")):
            with self.subTest(price=price):
                product_id = self.product("invalid-" + str(index))
                adapter = FakeAdapter(snapshot=StockSnapshot(True, price))
                Engine(self.store, adapter).run_cycle()
                self.assertFalse(self.product_row(product_id)["active"])
                self.assertEqual(self.product_row(product_id)["status"], "needs_action")
                self.assertEqual(adapter.checkouts, [])
                self.assertEqual(self.store.state()["orders"], [])

    def test_inspection_error_pauses_target_before_any_reservation(self):
        product_id = self.product()
        adapter = FakeAdapter(snapshot=AdapterError("Stock selector is missing"))
        Engine(self.store, adapter).run_cycle()
        self.assertEqual(self.product_row(product_id)["status"], "needs_action")
        self.assertFalse(self.product_row(product_id)["active"])
        self.assertEqual(self.store.state()["orders"], [])
        self.assertEqual(adapter.checkouts, [])

    def test_final_checkout_total_over_limit_prevents_submit(self):
        product_id = self.product()
        adapter = FakeAdapter(total=100_001)
        Engine(self.store, adapter).run_cycle()
        order = self.store.state()["orders"][0]
        self.assertEqual(order["status"], "needs_action")
        self.assertIsNone(order["total"])
        self.assertEqual(adapter.submit_actions, 0)
        self.assertFalse(self.product_row(product_id)["active"])

    def test_failure_before_submit_is_held_without_retry(self):
        self.product()
        adapter = FakeAdapter(behavior="fail_before_submit")
        engine = Engine(self.store, adapter)
        engine.run_cycle()
        engine.run_cycle()
        self.assertEqual(len(adapter.checkouts), 1)
        self.assertEqual(adapter.submit_actions, 0)
        self.assertEqual(self.store.state()["orders"][0]["status"], "needs_action")
        self.assertEqual(self.store.state()["profiles"][0]["committed"], 100_000)

    def test_lost_submit_response_becomes_unknown_and_blocks_same_profile(self):
        self.product("first")
        adapter = FakeAdapter(behavior="timeout_after_submit")
        engine = Engine(self.store, adapter)
        engine.run_cycle()
        second_id = self.product("second")
        engine.run_cycle()
        engine.run_cycle()
        self.assertEqual(adapter.submit_actions, 1)
        self.assertEqual(len(adapter.checkouts), 1)
        self.assertEqual(len(self.store.state()["orders"]), 1)
        self.assertEqual(self.store.state()["orders"][0]["status"], "unknown")
        self.assertEqual(self.store.state()["profiles"][0]["committed"], 100_000)
        self.assertFalse(self.product_row(second_id)["active"])

    def test_changed_total_after_submit_requires_reconciliation(self):
        self.product()
        adapter = FakeAdapter(behavior="changed_total")
        engine = Engine(self.store, adapter)
        engine.run_cycle()
        engine.run_cycle()
        self.assertEqual(adapter.submit_actions, 1)
        self.assertEqual(self.store.state()["orders"][0]["status"], "unknown")
        self.assertEqual(self.store.state()["profiles"][0]["committed"], 100_000)

    def test_adapter_cannot_report_payment_without_submit_callback(self):
        self.product()
        adapter = FakeAdapter(behavior="missing_callback")
        Engine(self.store, adapter).run_cycle()
        self.assertEqual(self.store.state()["orders"][0]["status"], "needs_action")
        self.assertEqual(adapter.submit_actions, 0)

    def test_priority_and_remaining_budget_decide_which_product_is_ordered(self):
        self.store.budget({"profile_id": 1, "budget": 150_000})
        low = self.product("low-priority", priority=9)
        high = self.product("high-priority", priority=1)
        adapter = FakeAdapter()
        Engine(self.store, adapter).run_cycle()
        self.assertEqual([p["id"] for p in adapter.checkouts], [high])
        self.assertEqual(self.product_row(low)["status"], "budget_blocked")
        self.assertEqual(self.store.state()["profiles"][0]["committed"], 95_000)

    def test_pause_while_stock_is_read_prevents_stale_order(self):
        product_id = self.product()
        adapter = FakeAdapter()
        adapter.inspect_hook = lambda _product: self.store.toggle(product_id, False)
        Engine(self.store, adapter).run_cycle()
        self.assertEqual(adapter.checkouts, [])
        self.assertEqual(self.store.state()["orders"], [])
        self.assertFalse(self.product_row(product_id)["active"])

    def test_overlapping_cycles_cannot_duplicate_inspection_or_checkout(self):
        self.product()
        adapter = FakeAdapter()
        started, release = threading.Event(), threading.Event()

        def blocking_inspection(_product):
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test worker was not released")

        adapter.inspect_hook = blocking_inspection
        engine = Engine(self.store, adapter)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(engine.run_cycle)
            try:
                self.assertTrue(started.wait(timeout=5))
                engine.run_cycle()
                self.assertEqual(len(adapter.inspections), 1)
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(adapter.submit_actions, 1)
        self.assertEqual(len(self.store.state()["orders"]), 1)

    def test_stop_during_inspection_prevents_new_checkout(self):
        self.product()
        adapter = FakeAdapter()
        engine = Engine(self.store, adapter)
        adapter.inspect_hook = lambda _product: engine.stop_monitor()
        engine.start_monitor()
        engine.run_cycle()
        self.assertEqual(adapter.checkouts, [])
        self.assertEqual(self.store.state()["orders"], [])
        self.assertFalse(engine.status()["running"])

    def test_stop_during_checkout_finishes_current_order_and_skips_next(self):
        self.product("first")
        self.product("second")
        adapter = FakeAdapter()
        engine = Engine(self.store, adapter)
        adapter.submit_hook = lambda _product: engine.stop_monitor()
        engine.start_monitor()
        engine.run_cycle()
        self.assertEqual(adapter.submit_actions, 1)
        self.assertEqual(len(self.store.state()["orders"]), 1)
        self.assertEqual(self.store.state()["orders"][0]["status"], "awaiting_payment")

    def test_notify_does_not_reserve_or_open_checkout_even_with_zero_budget(self):
        self.store.budget({"profile_id": 1, "budget": 0})
        product_id = self.product(action="notify")
        adapter = FakeAdapter()
        engine = Engine(self.store, adapter)
        engine.run_cycle()
        engine.run_cycle()
        state = self.store.state()
        self.assertEqual(self.product_row(product_id)["status"], "available")
        self.assertTrue(self.product_row(product_id)["active"])
        self.assertEqual(state["orders"], [])
        self.assertEqual(state["profiles"][0]["committed"], 0)
        self.assertEqual(adapter.checkouts, [])
        self.assertEqual(adapter.preparations, [])
        self.assertEqual(engine.connection()["status"], "connected")

    def test_assist_keeps_local_reservation_without_claiming_merchant_order(self):
        first = self.product("first", action="assist")
        second = self.product("second", action="assist")
        adapter = FakeAdapter()
        adapter.checkout_ready = False
        engine = Engine(self.store, adapter)
        engine.run_cycle()
        engine.run_cycle()
        state = self.store.state()
        self.assertEqual(len(state["orders"]), 1)
        order = state["orders"][0]
        self.assertEqual(order["status"], "manual_checkout")
        self.assertEqual(order["order_ref"], "")
        self.assertEqual(order["payment_url"], "")
        self.assertIsNone(order["total"])
        self.assertEqual(order["product_url"], self.product_row(first)["url"])
        self.assertEqual(state["profiles"][0]["committed"], 100_000)
        self.assertFalse(self.product_row(first)["active"])
        self.assertFalse(self.product_row(second)["active"])
        self.assertEqual(self.product_row(second)["status"], "needs_action")
        self.assertEqual(len(adapter.preparations), 1)
        self.assertEqual(adapter.submit_actions, 0)

    def test_auto_without_checkout_mapping_pauses_without_reserving(self):
        product_id = self.product()
        adapter = FakeAdapter()
        adapter.checkout_ready = False
        Engine(self.store, adapter).run_cycle()
        self.assertEqual(self.product_row(product_id)["status"], "needs_action")
        self.assertFalse(self.product_row(product_id)["active"])
        self.assertEqual(self.store.state()["orders"], [])
        self.assertEqual(adapter.submit_actions, 0)

    def test_discovery_is_queued_read_only_and_errors_clear_previous_result(self):
        adapter = FakeAdapter()
        adapter.checkout_ready = False
        engine = Engine(self.store, adapter)
        product = {"url": "https://ibox.co.id/product/fixture-discovery", "profile_id": 1}
        request_id = engine.discover_product(product)
        pending = engine.discovery()
        self.assertTrue(pending["busy"])
        self.assertEqual(pending["request_id"], request_id)
        self.assertIsNone(pending["result"])
        with self.assertRaises(ValueError):
            engine.discover_product(product)
        engine._run_command(*engine._commands.get_nowait())
        first = engine.discovery()
        self.assertFalse(first["busy"])
        self.assertEqual(first["result"]["name"], "Phone")
        # Consumers cannot mutate the worker's cached result through state snapshots.
        first["result"]["name"] = "Changed externally"
        self.assertEqual(engine.discovery()["result"]["name"], "Phone")
        adapter.discovery_error = NeedsAction("Product selectors changed")
        newer = engine.discover_product(product)
        self.assertNotEqual(newer, request_id)
        self.assertIsNone(engine.discovery()["result"])
        engine._run_command(*engine._commands.get_nowait())
        failed = engine.discovery()
        self.assertEqual(failed["request_id"], newer)
        self.assertFalse(failed["busy"])
        self.assertIsNone(failed["result"])
        self.assertEqual(failed["error"], "Product selectors changed")
        self.assertEqual(engine.connection()["status"], "needs_action")
        self.assertEqual(self.store.state()["products"], [])
        self.assertEqual(self.store.state()["orders"], [])

    def test_login_and_resume_are_separate_from_checkout_submission(self):
        product_id = self.product(action="notify", active=False)
        product = self.product_row(product_id)
        adapter = FakeAdapter()
        adapter.checkout_ready = False
        engine = Engine(self.store, adapter)
        engine.open_login(1)
        engine.open_product(product)
        engine.open_product(product, resume=True)
        for _ in range(3):
            engine._run_command(*engine._commands.get_nowait())
        self.assertEqual(adapter.logins, [1])
        self.assertEqual(len(adapter.openings), 1)
        self.assertEqual(len(adapter.resumes), 1)
        self.assertEqual(adapter.checkouts, [])
        self.assertEqual(adapter.preparations, [])
        self.assertEqual(self.store.state()["orders"], [])
        self.assertEqual(engine.connection()["status"], "not_checked")

    def test_stale_inspection_error_does_not_pause_newly_edited_target(self):
        product_id = self.product(color="Blue", capacity="256 GB", action="notify")
        original_revision = self.product_row(product_id)["revision"]
        adapter = FakeAdapter(snapshot=NeedsAction("Old Blue variant read failed"))
        def edit_during_inspection(_product):
            current = self.product_row(product_id)
            self.store.save_product({**current, "color": "White", "active": True}, product_id)
        adapter.inspect_hook = edit_during_inspection
        Engine(self.store, adapter).run_cycle()
        current = self.product_row(product_id)
        self.assertGreater(current["revision"], original_revision)
        self.assertEqual(current["color"], "White")
        self.assertTrue(current["active"])
        self.assertEqual(current["status"], "watching")
        self.assertEqual(current["note"], "")
        self.assertIsNone(current["last_checked"])
        self.assertEqual(self.store.state()["orders"], [])

    def test_minimum_live_poll_interval_is_sixty_seconds(self):
        self.assertEqual(Engine(self.store, FakeAdapter(), interval=1).interval, 60)


if __name__ == "__main__":
    unittest.main()
