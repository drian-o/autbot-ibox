"""Live adapter logic tests; browser/network effects are replaced explicitly.

These cases verify variant/price and handoff behavior. They are not evidence of
a completed merchant checkout or a successful real browser launch.
"""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts import AdapterError, NeedsAction, CheckoutResult
from browser_adapter import BrowserAdapter
from ibox_adapter import (
    IBoxAdapter, PRODUCT_SELECTORS, _selected, _validate_configurables, default_config, merge_config,
    validate_live_config,
)


def state():
    return {
        "name": "iPhone Duo", "price_text": "Rp 74.999.000", "button_text": "Stok habis",
        "button_disabled": True, "url": "https://ibox.co.id/product/iphone-duo-s9600006714",
        "colors": [
            {"value": "Star White", "selected": True, "selectable": True, "available": True},
            {"value": "Night Sky", "selected": False, "selectable": False, "available": False},
        ],
        "capacities": [
            {"value": "256 GB", "selected": True, "selectable": True, "available": False},
            {"value": "2 TB", "selected": False, "selectable": True, "available": False},
        ],
        "selected_color": "Star White", "selected_capacity": "256 GB",
    }


class Option:
    def __init__(self, page, field, index):
        self.page, self.field, self.index = page, field, index

    def input_value(self):
        return self.page.state[self.field][self.index]["value"]

    def click(self, **kwargs):
        option = self.page.state[self.field][self.index]
        if not option["selectable"]:
            raise AssertionError("Cannot click a disabled option")
        self.page.clicks.append(option["value"])
        for row in self.page.state[self.field]:
            row["selected"] = row is option
        self.page.state["selected_color" if self.field == "colors" else "selected_capacity"] = option["value"]


class Options:
    def __init__(self, page, field):
        self.page, self.field = page, field

    def count(self):
        return len(self.page.state[self.field])

    def nth(self, index):
        return Option(self.page, self.field, index)


class Page:
    def __init__(self, url="https://ibox.co.id/product/iphone-duo"):
        self.state, self.url = state(), url
        self.clicks, self.navigations, self.front_count = [], [], 0
        self.closed = False
        self.quantity = "1"
        self.quantity_limit = None

    def locator(self, selector):
        for kind, field in (("color", "colors"), ("capacity", "capacities")):
            if selector == PRODUCT_SELECTORS[kind]:
                return Options(self, field)
        raise AssertionError("Unknown selector " + selector)

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, _):
        pass

    def bring_to_front(self):
        self.front_count += 1


class Context:
    def __init__(self, pages):
        self.pages = list(pages)

    def new_page(self):
        page = Page("about:blank")
        self.pages.append(page)
        return page


class FakeAdapter(IBoxAdapter):
    def __init__(self, directory):
        super().__init__(default_config(), Path(directory))
        self.page = Page()
        self.manual = Page("https://ibox.co.id/current-checkout")
        self.login = Page("https://ibox.co.id/")
        self.preview = Page()
        self.session = {
            "page": self.page, "manual_pages": {self.manual, self.login, self.preview},
            "login_page": self.login, "preview_page": self.preview,
            "context": Context([self.page, self.manual, self.login, self.preview]),
            "block": "", "payment_phase": False,
        }

    def _session(self, _):
        return self.session

    def _stable_state(self, session):
        return copy.deepcopy(session["page"].state)

    def _goto(self, session, url, **kwargs):
        session["page"].url = url
        session["page"].navigations.append(url)
        return session["page"]

    def _close_public_popup(self, _):
        pass

    def _one(self, page, selector, label):
        if selector == PRODUCT_SELECTORS["quantity"]:
            return Quantity(page)
        raise AssertionError("Unexpected locator " + selector)

    def _guard(self, session):
        pass


class Quantity:
    def __init__(self, page):
        self.page = page

    def fill(self, value, **kwargs):
        self.page.quantity = value

    def press(self, key, **kwargs):
        if self.page.quantity_limit is not None:
            self.page.quantity = str(min(int(self.page.quantity), self.page.quantity_limit))

    def input_value(self):
        return self.page.quantity


class IBoxAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.adapter = FakeAdapter(self.directory.name)
        self.product = {
            "profile_id": 1, "url": "https://ibox.co.id/product/iphone-duo",
            "name": "iPhone Duo", "color": "Star White", "capacity": "2 TB",
            "quantity": 1, "max_unit_price": 75000000, "max_total_price": 76000000,
        }
        self.adapter.session["handoff_pages"] = {self.adapter._product_key(self.product): self.adapter.manual}

    def tearDown(self):
        self.directory.cleanup()

    def test_reading_configuration_does_not_require_checkout_mapping(self):
        self.assertEqual(validate_live_config(default_config()), [])
        self.assertFalse(self.adapter.checkout_ready)
        changed = merge_config({"browser": {"channel": "msedge"}})
        self.assertEqual(changed["merchant"]["selectors"]["product_price"], PRODUCT_SELECTORS["price"])
        self.assertEqual(validate_live_config(changed), [])
        old = merge_config({"merchant": {"allowed_hosts": [], "login_url": "", "selectors": {"product_price": ""}}})
        self.assertEqual(validate_live_config(old), [])
        self.assertEqual(old["merchant"]["selectors"]["product_price"], PRODUCT_SELECTORS["price"])

    def test_other_hosts_and_invisible_browser_are_not_live_ready(self):
        for edit in ({"merchant": {"allowed_hosts": ["ibox.com"]}}, {"browser": {"headless": True}},
                     {"allow_local_test_urls": True}, {"merchant": {"allowed_hosts": [{}]}},
                     {"merchant": {"selectors": None}}, {"merchant": {"selectors": []}}):
            self.assertTrue(validate_live_config(merge_config(edit)))
        for url in ("https://ibox.com/product/a", "https://ibox.co.id.evil.test/product/a", "http://ibox.co.id/product/a",
                    "https://me:secret@ibox.co.id/product/a", "https://ibox.co.id/akun/dashboard"):
            self.assertRaises(AdapterError, self.adapter._product_url, url)

    def test_selected_color_is_never_replaced_with_available_color(self):
        product = dict(self.product, color="Night Sky")
        snapshot = self.adapter.inspect(product)
        self.assertFalse(snapshot.available)
        self.assertIsNone(snapshot.unit_price)
        self.assertIn("Night Sky", snapshot.note)
        self.assertEqual(self.adapter.page.clicks, ["2 TB"])
        self.assertEqual(self.adapter.page.state["selected_color"], "Star White")

    def test_capacity_selection_uses_normal_click_and_verifies_state(self):
        snapshot = self.adapter.inspect(dict(self.product, capacity="2tb"))
        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.unit_price, 74999000)
        self.assertEqual(self.adapter.page.clicks, ["2 TB"])

    def test_unsupported_variant_is_a_mapping_error_not_stock_available(self):
        with self.assertRaises(NeedsAction):
            self.adapter.inspect(dict(self.product, color="Made up color"))

    def test_omitted_variant_needs_selection_for_monitoring(self):
        with self.assertRaises(NeedsAction):
            self.adapter.inspect(dict(self.product, capacity=""))
        result = self.adapter.discover({"profile_id": 1, "url": self.product["url"]})
        self.assertEqual(result["name"], "iPhone Duo")
        self.assertEqual(result["selected_color"], "Star White")
        self.assertEqual(result["capacities"][1]["value"], "2 TB")

    def test_wrong_product_name_pauses(self):
        self.assertRaises(NeedsAction, self.adapter.inspect, dict(self.product, name="iphoneduo"))

    def test_disabled_button_without_stock_marker_does_not_claim_sold_out(self):
        raw = state()
        raw["button_text"] = "Pilih lokasi"
        self.assertRaises(NeedsAction, self.adapter._snapshot, raw, self.product)

    def test_price_parser_does_not_merge_installment_and_full_price(self):
        raw = state()
        raw["price_text"] = "Rp 74.999.000 Cicilan Rp 3.124.958"
        self.assertRaises(AdapterError, self.adapter._snapshot, raw, self.product)

    def test_ambiguous_selected_variants_fail_closed(self):
        options = state()["colors"]
        options[1]["selected"] = True
        self.assertRaises(NeedsAction, _selected, options, "warna")

    def test_login_and_resume_do_not_navigate_an_existing_checkout(self):
        original = self.adapter.manual.url
        self.adapter.open_login(1)
        result = self.adapter.resume_purchase(self.product)
        self.assertEqual(self.adapter.manual.url, original)
        self.assertEqual(self.adapter.manual.navigations, [])
        self.assertEqual(self.adapter.manual.front_count, 1)
        self.assertEqual(self.adapter.login.front_count, 1)
        self.assertNotIn("payment_url", result)

    def test_product_reading_never_navigates_the_manual_checkout_tab(self):
        self.adapter.inspect(self.product)
        self.assertEqual(self.adapter.manual.navigations, [])
        self.assertEqual(self.adapter.page.navigations, [self.product["url"]])

    def test_unconfigured_checkout_does_not_submit(self):
        snapshot = self.adapter.inspect(self.product)
        callbacks = []
        self.assertRaises(NeedsAction, self.adapter.checkout, self.product, snapshot, callbacks.append)
        self.assertEqual(callbacks, [])

    def test_handoff_prepares_quantity_without_claiming_merchant_order(self):
        self.adapter.manual.state.update(button_disabled=False, button_text="Tambah keranjang")
        for option in self.adapter.manual.state["capacities"]:
            option["available"] = True
        result = self.adapter.prepare_purchase(self.product)
        self.assertEqual(result["quantity"], 1)
        self.assertEqual(result["unit_price"], 74999000)
        self.assertNotIn("order_ref", result)
        self.assertNotIn("payment_url", result)
        self.assertEqual(self.adapter.manual.clicks, ["2 TB"])
        self.assertEqual(self.adapter.page.navigations, [])
        self.assertIs(self.adapter.session["page"], self.adapter.page)

    def test_handoff_rechecks_current_price_against_limit(self):
        self.adapter.manual.state.update(button_disabled=False, button_text="Tambah keranjang", price_text="Rp 76.000.000")
        for option in self.adapter.manual.state["capacities"]:
            option["available"] = True
        self.assertRaises(NeedsAction, self.adapter.prepare_purchase, self.product)

    def test_handoff_stops_when_merchant_reduces_quantity(self):
        self.adapter.manual.state.update(button_disabled=False, button_text="Tambah keranjang")
        for option in self.adapter.manual.state["capacities"]:
            option["available"] = True
        self.adapter.manual.quantity_limit = 1
        product = dict(self.product, quantity=2, max_total_price=152000000)
        self.assertRaises(NeedsAction, self.adapter.prepare_purchase, product)

    def test_changed_configurable_ids_cannot_be_treated_as_no_variants(self):
        for key in ("unmapped_color_count", "unmapped_capacity_count"):
            with self.subTest(key=key):
                raw = state()
                raw[key] = 1
                self.assertRaises(NeedsAction, self.adapter._snapshot, raw, self.product)

    def test_additional_configurable_category_stops_product_reading(self):
        raw = state()
        raw["unsupported_configurables"] = ["Ukuran"]
        self.assertRaises(NeedsAction, self.adapter._snapshot, raw, self.product)

    def test_one_model_is_supported_but_multiple_models_are_not(self):
        raw = state()
        raw["model_options"] = ["iPhone Duo"]
        _validate_configurables(raw)
        raw["model_options"].append("iPhone Duo Pro")
        self.assertRaises(NeedsAction, _validate_configurables, raw)

    def test_unavailable_selected_capacity_and_enabled_buy_button_is_unknown(self):
        raw = state()
        raw.update(button_disabled=False, button_text="Tambah keranjang")
        self.assertRaises(NeedsAction, self.adapter._snapshot, raw, self.product)

    def test_disabled_selected_color_and_enabled_buy_button_is_unknown(self):
        raw = state()
        raw.update(button_disabled=False, button_text="Tambah keranjang")
        for option in raw["capacities"]:
            option["available"] = True
        raw["colors"][0].update(available=False, selectable=False)
        self.assertRaises(NeedsAction, self.adapter._snapshot, raw, self.product)

    def test_unavailable_selected_variant_with_sold_out_button_is_valid(self):
        raw = state()
        raw["model_options"] = ["iPhone Duo"]
        raw["colors"][0].update(available=False, selectable=False)
        snapshot = self.adapter._snapshot(raw, self.product)
        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.unit_price, 74999000)

    def test_deliberate_product_preview_does_not_replace_manual_checkout(self):
        original = self.adapter.manual.url
        self.adapter.open_product(self.product)
        self.assertEqual(self.adapter.manual.url, original)
        self.assertEqual(self.adapter.manual.navigations, [])
        self.assertEqual(self.adapter.preview.navigations, [self.product["url"]])

    def test_lost_pending_checkout_opens_history_review_instead_of_product(self):
        self.adapter.manual.closed = True
        result = self.adapter.resume_purchase(dict(self.product, _resume_status="unknown"))
        self.assertIn("riwayat", result["note"])
        review = self.adapter.session["review_page"]
        self.assertEqual(review.navigations, ["https://ibox.co.id/"])
        self.assertEqual(self.adapter.preview.navigations, [])

    def test_auto_checkout_result_or_failure_preserves_separate_handoff_page(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                def checkout(_self, product, snapshot, before_submit):
                    session = _self._session(product["profile_id"])
                    session["page"].url = "https://ibox.co.id/checkout-in-progress"
                    if failure:
                        raise NeedsAction("Fixture interrupted checkout")
                    return CheckoutResult("unknown", note="Fixture ambiguous result")

                with patch.object(IBoxAdapter, "checkout_ready", new_callable=PropertyMock, return_value=True):
                    with patch.object(BrowserAdapter, "checkout", new=checkout):
                        if failure:
                            self.assertRaises(NeedsAction, self.adapter.checkout, self.product, None, lambda total: None)
                        else:
                            self.adapter.checkout(self.product, None, lambda total: None)
                handoff = self.adapter.session["handoff_pages"][self.adapter._product_key(self.product)]
                self.assertIs(self.adapter.session["page"], self.adapter.page)
                self.assertIsNot(handoff, self.adapter.page)
                self.assertIn(handoff, self.adapter.session["manual_pages"])
                self.adapter.inspect(self.product)
                self.assertEqual(handoff.url, "https://ibox.co.id/checkout-in-progress")


if __name__ == "__main__":
    unittest.main()
