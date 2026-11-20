"""Checkout safety tests using an isolated SQLite database; no merchant traffic."""
from __future__ import annotations

import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path

from contracts import CheckoutResult, StockSnapshot
from store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.store.budget({"profile_id": 1, "budget": 50_000_000})

    def product(self, slug="phone", **overrides):
        data = {
            "profile_id": 1,
            "name": "Phone",
            "url": f"https://ibox.co.id/product/fixture-{slug}",
            "variant": "Blue / 256 GB",
            "action": "auto",
            "quantity": 1,
            "max_unit_price": 90_000,
            "max_total_price": 100_000,
            "priority": 1,
            "active": True,
        }
        data.update(overrides)
        product_id = self.store.save_product(data)
        return data, self.product_row(product_id)

    def product_row(self, product_id):
        return next(p for p in self.store.state()["products"] if p["id"] == product_id)

    def order_row(self, order_id):
        return next(o for o in self.store.state()["orders"] if o["id"] == order_id)

    def profile_row(self, profile_id=1):
        return next(p for p in self.store.state()["profiles"] if p["id"] == profile_id)

    def awaiting_payment(self, product, total=95_000):
        order = self.store.claim(product, product["max_unit_price"])
        self.assertIsNotNone(order)
        self.store.before_submit(order["id"], total)
        self.store.finish(order["id"], CheckoutResult(
            "awaiting_payment", order_ref="FIXTURE-" + order["id"],
            payment_url="https://ibox.co.id/payment/fixture-" + order["id"], total=total,
        ))
        return order

    def test_concurrent_claims_across_database_connections_create_one_order(self):
        _, product = self.product()
        stores = [Store(self.path) for _ in range(8)]
        barrier = threading.Barrier(len(stores))

        def claim(store):
            barrier.wait(timeout=10)
            return store.claim(product, 90_000)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(stores)) as pool:
            claims = list(pool.map(claim, stores))

        self.assertEqual(sum(order is not None for order in claims), 1)
        self.assertEqual(len(self.store.state()["orders"]), 1)
        self.assertEqual(self.profile_row()["committed"], 100_000)
        self.assertFalse(self.product_row(product["id"])["active"])

    def test_concurrent_different_products_cannot_exceed_profile_budget(self):
        self.store.budget({"profile_id": 1, "budget": 150_000})
        products = [self.product(slug)[1] for slug in ("phone-a", "phone-b")]
        stores = [Store(self.path), Store(self.path)]
        barrier = threading.Barrier(2)

        def claim(pair):
            store, product = pair
            barrier.wait(timeout=10)
            return store.claim(product, 90_000)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, zip(stores, products)))

        self.assertEqual(sum(order is not None for order in claims), 1)
        self.assertEqual(self.profile_row()["committed"], 100_000)
        self.assertLessEqual(self.profile_row()["committed"], 150_000)

    def test_paused_or_stale_snapshot_cannot_create_order(self):
        _, product = self.product()
        self.store.toggle(product["id"], False)
        self.assertIsNone(self.store.claim(product, 90_000))
        self.store.toggle(product["id"], True)
        self.assertIsNone(self.store.claim(product, 90_000))
        fresh = self.product_row(product["id"])
        self.assertIsNotNone(self.store.claim(fresh, 90_000))

    def test_price_over_unit_limit_cannot_create_order(self):
        _, product = self.product()
        self.assertIsNone(self.store.claim(product, 90_001))
        self.assertEqual(self.store.state()["orders"], [])

    def test_full_total_cap_is_reserved_before_cart_interaction(self):
        _, product = self.product()
        order = self.store.claim(product, 80_000)
        self.assertEqual(order["status"], "preparing")
        self.assertEqual(order["reserved_total"], 100_000)
        self.assertIsNone(order["total"])
        self.assertEqual(self.profile_row()["committed"], 100_000)
        with self.assertRaises(ValueError):
            self.store.budget({"profile_id": 1, "budget": 99_999})

    def test_pending_paid_and_unknown_orders_all_consume_budget(self):
        for status in ("awaiting_payment", "paid_manual", "unknown"):
            with self.subTest(status=status):
                profile_id = self.store.add_profile({"name": status, "budget": 200_000})
                _, product = self.product(
                    status, profile_id=profile_id,
                    max_unit_price=150_000, max_total_price=160_000,
                )
                order = self.store.claim(product, 140_000)
                self.store.before_submit(order["id"], 145_000)
                if status == "unknown":
                    self.store.checkout_error(order["id"], "Network disconnected after submit")
                else:
                    self.store.finish(order["id"], CheckoutResult(
                        "awaiting_payment", order_ref=status, total=145_000,
                    ))
                    if status == "paid_manual":
                        self.store.resolve(order["id"], {
                            "status": "paid_manual", "note": "Verified in merchant order history",
                        })
                held = 160_000 if status == "unknown" else 145_000
                self.assertEqual(self.profile_row(profile_id)["committed"], held)
                with self.assertRaises(ValueError):
                    self.store.budget({"profile_id": profile_id, "budget": held - 1})
                _, extra = self.product(
                    status + "-extra", profile_id=profile_id,
                    max_unit_price=55_000, max_total_price=60_000,
                )
                self.assertIsNone(self.store.claim(extra, 55_000))
                expected = "needs_action" if status == "unknown" else "budget_blocked"
                self.assertEqual(self.product_row(extra["id"])["status"], expected)

    def test_final_total_over_cap_is_rejected_before_submission(self):
        _, product = self.product()
        order = self.store.claim(product, 90_000)
        with self.assertRaisesRegex(ValueError, "melebihi limit"):
            self.store.before_submit(order["id"], 100_001)
        current = self.order_row(order["id"])
        self.assertEqual(current["status"], "preparing")
        self.assertIsNone(current["total"])
        self.assertEqual(current["reserved_total"], 100_000)

    def test_before_submit_persists_total_and_cannot_be_repeated(self):
        _, product = self.product()
        order = self.store.claim(product, 90_000)
        self.store.before_submit(order["id"], 95_000)
        current = self.order_row(order["id"])
        self.assertEqual(current["status"], "submitting")
        self.assertEqual(current["total"], 95_000)
        self.assertEqual(current["reserved_total"], 100_000)
        with self.assertRaises(ValueError):
            self.store.before_submit(order["id"], 95_000)

    def test_payment_page_requires_previously_persisted_submit(self):
        _, product = self.product()
        order = self.store.claim(product, 90_000)
        with self.assertRaises(ValueError):
            self.store.finish(order["id"], CheckoutResult("awaiting_payment"))
        self.assertEqual(self.order_row(order["id"])["status"], "preparing")

    def test_changed_post_submit_total_requires_reconciliation(self):
        _, product = self.product()
        order = self.store.claim(product, 90_000)
        self.store.before_submit(order["id"], 95_000)
        with self.assertRaisesRegex(ValueError, "berubah"):
            self.store.finish(order["id"], CheckoutResult("awaiting_payment", total=96_000))
        self.store.checkout_error(order["id"], "Merchant total changed")
        current = self.order_row(order["id"])
        self.assertEqual(current["status"], "unknown")
        self.assertEqual(self.profile_row()["committed"], 100_000)
        with self.assertRaises(ValueError):
            self.store.toggle(product["id"], True)

    def test_payment_page_is_pending_and_does_not_mark_order_paid(self):
        _, product = self.product()
        order = self.awaiting_payment(product)
        current = self.order_row(order["id"])
        self.assertEqual(current["status"], "awaiting_payment")
        self.assertNotEqual(current["status"], "paid_manual")
        self.assertEqual(current["payment_url"], "https://ibox.co.id/payment/fixture-" + order["id"])
        with self.assertRaises(ValueError):
            self.store.toggle(product["id"], True)

    def test_recovery_marks_interrupted_orders_unknown_without_retries(self):
        _, preparing_product = self.product("preparing")
        second_profile = self.store.add_profile({"name": "Other profile", "budget": 1_000_000})
        _, submitting_product = self.product("submitting", profile_id=second_profile)
        preparing = self.store.claim(preparing_product, 90_000)
        submitting = self.store.claim(submitting_product, 90_000)
        self.store.before_submit(submitting["id"], 95_000)
        restarted = Store(self.path)
        self.assertEqual(restarted.recover(), 2)
        self.assertEqual(restarted.recover(), 0)
        self.assertEqual(restarted.active_products(), [])
        self.assertEqual(self.profile_row()["committed"], 100_000)
        self.assertEqual(self.profile_row(second_profile)["committed"], 100_000)
        for order, product in ((preparing, preparing_product), (submitting, submitting_product)):
            with self.subTest(order_id=order["id"]):
                self.assertEqual(self.order_row(order["id"])["status"], "unknown")
                self.assertIsNone(restarted.claim(product, 90_000))
                with self.assertRaises(ValueError):
                    restarted.toggle(product["id"], True)

    def test_cancel_releases_budget_but_keeps_target_paused(self):
        _, product = self.product()
        order = self.awaiting_payment(product)
        self.store.resolve(order["id"], {
            "status": "cancelled_manual", "note": "Merchant confirmed order cancellation",
        })
        self.assertEqual(self.profile_row()["committed"], 0)
        current = self.product_row(product["id"])
        self.assertEqual(current["status"], "paused")
        self.assertFalse(current["active"])
        self.assertEqual(self.store.active_products(), [])
        self.assertIsNone(self.store.claim(current, 90_000))
        self.store.toggle(product["id"], True)
        self.assertIsNotNone(self.store.claim(self.product_row(product["id"]), 90_000))

    def test_order_reconciliation_requires_a_note_and_is_final(self):
        _, product = self.product()
        order = self.awaiting_payment(product)
        with self.assertRaises(ValueError):
            self.store.resolve(order["id"], {"status": "paid_manual", "note": ""})
        self.store.resolve(order["id"], {"status": "paid_manual", "note": "Checked with merchant"})
        with self.assertRaises(ValueError):
            self.store.resolve(order["id"], {"status": "cancelled_manual", "note": "Change"})

    def test_late_checkout_error_cannot_overwrite_a_reconciled_order(self):
        for final_status in ("paid_manual", "cancelled_manual"):
            with self.subTest(status=final_status):
                _, product = self.product(final_status)
                order = self.awaiting_payment(product)
                self.store.resolve(order["id"], {"status": final_status, "note": "Merchant verified"})
                self.store.checkout_error(order["id"], "Late worker error")
                self.assertEqual(self.order_row(order["id"])["status"], final_status)

    def test_identity_is_immutable_after_order_history_exists(self):
        data, product = self.product()
        order = self.awaiting_payment(product)
        self.store.resolve(order["id"], {
            "status": "cancelled_manual", "note": "Confirmed cancelled at merchant",
        })
        second_profile = self.store.add_profile({"name": "Second profile", "budget": 1_000_000})
        for key, value in (
            ("profile_id", second_profile),
            ("name", "Different phone"),
            ("url", "https://ibox.co.id/product/fixture-other-phone"),
            ("variant", "Red / 256 GB"),
        ):
            with self.subTest(field=key):
                with self.assertRaisesRegex(ValueError, "Identitas produk"):
                    self.store.save_product({**data, key: value}, product["id"])
                self.assertEqual(self.product_row(product["id"])[key], product[key])

    def test_increased_target_orders_only_quantity_not_previously_paid(self):
        data, product = self.product()
        order = self.awaiting_payment(product)
        self.store.resolve(order["id"], {"status": "paid_manual", "note": "Merchant confirmed paid"})
        with self.assertRaisesRegex(ValueError, "Target jumlah sudah terpenuhi"):
            self.store.toggle(product["id"], True)
        self.store.save_product({**data, "quantity": 3, "max_total_price": 300_000}, product["id"])
        additional = self.store.claim(self.product_row(product["id"]), 90_000)
        self.assertEqual(additional["quantity"], 2)

    def test_pending_orders_prevent_product_edit_or_deletion(self):
        data, product = self.product()
        self.awaiting_payment(product)
        with self.assertRaises(ValueError):
            self.store.save_product({**data, "name": "Other"}, product["id"])
        with self.assertRaises(ValueError):
            self.store.delete(product["id"])

    def test_duplicate_target_in_same_profile_is_rejected(self):
        data, _ = self.product()
        with self.assertRaises(ValueError):
            self.store.save_product(data)

    def test_new_live_store_is_empty_and_has_no_default_spend_authority(self):
        fresh = Store(Path(self.temp.name) / "new.sqlite3")
        state = fresh.state()
        self.assertEqual(fresh.mode, "live")
        self.assertEqual(state["products"], [])
        self.assertEqual(state["orders"], [])
        self.assertEqual(state["profiles"][0]["budget"], 0)
        self.assertEqual(state["profiles"][0]["committed"], 0)
        with self.assertRaises(ValueError):
            Store(Path(self.temp.name) / "unsupported.sqlite3", "demo")

    def test_only_actual_https_ibox_product_urls_are_accepted(self):
        for index, url in enumerate(("https://ibox.co.id/product/fixture-phone", "https://www.ibox.co.id/product/fixture-other")):
            with self.subTest(accepted=url):
                _, row = self.product(str(index), url=url)
                self.assertEqual(row["url"], url)
        for url in (
            "https://demo.invalid/product/fixture-phone", "http://ibox.co.id/product/fixture-phone",
            "https://ibox.com/product/fixture-phone", "https://ibox.co.id.evil.invalid/product/fixture-phone",
            "https://ibox.co.id/akun/dashboard", "https://ibox.co.id/product/", "https://ibox.co.id:8080/product/a",
            "https://user:pass@ibox.co.id/product/a", "https://ibox.co.id:invalid/product/a",
        ):
            with self.subTest(rejected=url):
                with self.assertRaises(ValueError):
                    self.product(url=url)

    def test_color_and_capacity_are_stored_independently(self):
        _, row = self.product(color="Night Sky", capacity="2 TB", action="assist")
        self.assertEqual(row["color"], "Night Sky")
        self.assertEqual(row["capacity"], "2 TB")
        self.assertEqual(row["variant"], "Night Sky / 2 TB")
        self.assertEqual(row["action"], "assist")

    def test_variant_edit_clears_observation_from_previous_variant(self):
        data, product = self.product(color="Blue", capacity="256 GB", action="notify")
        self.store.observation(product["id"], StockSnapshot(True, 80_000, "Phone", "Blue / 256 GB"),
                               "available", "Observed Blue", product["revision"])
        self.assertTrue(self.product_row(product["id"])["last_stock"])
        self.store.save_product({**data, "color": "White"}, product["id"])
        changed = self.product_row(product["id"])
        self.assertEqual(changed["color"], "White")
        self.assertEqual(changed["status"], "watching")
        self.assertIsNone(changed["last_stock"])
        self.assertIsNone(changed["last_price"])
        self.assertIsNone(changed["last_checked"])
        self.assertEqual(changed["note"], "")

    def test_unknown_product_action_is_rejected(self):
        with self.assertRaises(ValueError):
            self.product(action="unrecognized")

    def test_manual_preparation_survives_restart_and_can_be_reconciled(self):
        _, product = self.product(action="assist")
        order = self.store.claim(product, 90_000)
        self.store.finish_manual(order["id"], {"product_url": product["url"], "note": "Waiting for user checkout"})
        restarted = Store(self.path)
        self.assertEqual(restarted.recover(), 0)
        current = restarted.get_order(order["id"])
        self.assertEqual(current["status"], "manual_checkout")
        self.assertEqual(current["order_ref"], "")
        self.assertEqual(current["payment_url"], "")
        self.assertIsNone(current["total"])
        self.assertEqual(self.profile_row()["committed"], 100_000)
        with self.assertRaises(ValueError):
            self.store.toggle(product["id"], True)
        restarted.resolve(order["id"], {"status": "cancelled_manual", "note": "No merchant order was created"})
        self.assertEqual(self.profile_row()["committed"], 0)
        self.assertFalse(self.product_row(product["id"])["active"])


if __name__ == "__main__":
    unittest.main()
