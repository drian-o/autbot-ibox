"""Live iBox product reading and a browser handoff using observed public DOM.

Product selectors were observed on ibox.co.id on 20 November 2026. No private
endpoint, browser-storage token, payment selector, or order receipt is guessed.
All browser operations belong to the engine's one worker thread.
"""
from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from browser_adapter import (
    BrowserAdapter, MAX_AMOUNT, _valid_url, normalize_text, parse_price_idr,
    validate_config as validate_checkout_config,
)
from contracts import AdapterError, CheckoutResult, NeedsAction, StockSnapshot


IBOX_HOSTS = {"ibox.co.id", "www.ibox.co.id"}
PRODUCT_SELECTORS = {
    "name": "h1#mcis-pdp-product-title",
    "price": '[data-testid="qa-pdp-price"]',
    "quantity": 'input[data-testid="qa-pdp-inputqty"]',
    "button": "button#mcis-add-to-cart",
    "color": 'input[name="Warna"][data-testid="qa-pdp-configurable-1"]',
    "capacity": 'input[name="Kapasitas"][data-testid="qa-pdp-configurable-3"]',
}


def default_config() -> dict:
    """Return a fresh, working public-product configuration, without credentials."""
    return {
        "browser": {"headless": False, "timeout_ms": 20000,
                    "payment_timeout_ms": 30000, "channel": "", "executable_path": ""},
        "checkout": {"enabled": False},
        "merchant": {
            "allowed_hosts": ["ibox.co.id", "www.ibox.co.id"],
            "allowed_payment_hosts": [], "login_url": "https://ibox.co.id/",
            "cart_url": "",
            "selectors": {
                "product_name": PRODUCT_SELECTORS["name"],
                "product_price": PRODUCT_SELECTORS["price"],
                "quantity_input": PRODUCT_SELECTORS["quantity"],
                "add_to_cart": PRODUCT_SELECTORS["button"],
                "in_stock": "button#mcis-add-to-cart:not([disabled])",
                "out_of_stock": "button#mcis-add-to-cart[disabled]",
                "login_required": "", "challenge": "", "selected_variant": "",
                "cart_empty": "", "cart_items": "", "cart_item_name": "",
                "cart_item_variant": "", "cart_item_quantity": "", "cart_item_unit_price": "",
                "checkout_items": "", "checkout_item_name": "", "checkout_item_variant": "",
                "checkout_item_quantity": "", "checkout_item_unit_price": "",
                "checkout_total": "", "submit_order": "", "payment_ready": "", "order_ref": "",
            },
            "variants": {}, "clear_cart_actions": [], "checkout_actions": [],
        },
    }


def merge_config(overrides: dict | None = None) -> dict:
    """Keep the observed PDP defaults when only browser settings are customized."""
    result = default_config()
    if overrides is None:
        return result
    if not isinstance(overrides, dict):
        raise ValueError("Akar config.json harus berupa objek JSON.")

    def merge(target, changes):
        for key, value in changes.items():
            if isinstance(target.get(key), dict) and isinstance(value, dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    merge(result, overrides)
    # An older, empty merchant template must not disable the built-in public
    # iBox connection. Nonempty conflicting values still fail validation.
    merchant = result.get("merchant")
    if isinstance(merchant, dict):
        defaults = default_config()["merchant"]
        if merchant.get("allowed_hosts") in (None, []):
            merchant["allowed_hosts"] = defaults["allowed_hosts"]
        if merchant.get("login_url") in (None, ""):
            merchant["login_url"] = defaults["login_url"]
        selectors = merchant.get("selectors")
        if isinstance(selectors, dict):
            for key in ("product_name", "product_price", "quantity_input", "add_to_cart", "in_stock", "out_of_stock"):
                if selectors.get(key) in (None, ""):
                    selectors[key] = defaults["selectors"][key]
    return result


def validate_live_config(config: dict) -> list[str]:
    """Validate read/login settings independently of optional checkout mapping."""
    if not isinstance(config, dict):
        return ["Konfigurasi harus berupa objek JSON."]
    merchant, browser = config.get("merchant"), config.get("browser")
    if not isinstance(merchant, dict) or not isinstance(browser, dict):
        return ["merchant dan browser harus berupa objek JSON."]
    problems = []
    if not isinstance(merchant.get("selectors", {}), dict):
        problems.append("merchant.selectors harus berupa objek JSON.")
    hosts = merchant.get("allowed_hosts")
    if not isinstance(hosts, list) or not hosts or any(not isinstance(h, str) or h not in IBOX_HOSTS for h in hosts):
        problems.append("Koneksi iBox hanya menerima host ibox.co.id dan www.ibox.co.id.")
    if not _valid_url(merchant.get("login_url", ""), IBOX_HOSTS):
        problems.append("login_url harus berupa URL HTTPS iBox.")
    for key in ("timeout_ms", "payment_timeout_ms"):
        value = browser.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1000 <= value <= 120000:
            problems.append(f"browser.{key} harus berupa integer 1000 sampai 120000.")
    if browser.get("headless") is not False:
        problems.append("Gunakan browser.headless=false agar login dan checkout dapat dilanjutkan di browser.")
    if browser.get("channel") not in ("", "chromium", "chrome", "msedge"):
        problems.append("browser.channel hanya kosong, chromium, chrome, atau msedge.")
    if not isinstance(browser.get("executable_path", ""), str):
        problems.append("browser.executable_path harus berupa teks.")
    if not isinstance(config.get("checkout", {}), dict):
        problems.append("checkout harus berupa objek JSON.")
    elif not isinstance(config.get("checkout", {}).get("enabled", False), bool):
        problems.append("checkout.enabled harus true atau false.")
    if config.get("allow_local_test_urls"):
        problems.append("Adapter iBox live tidak menerima URL fixture lokal.")
    return problems


def _same_option(left: str, right: str, field: str) -> bool:
    left, right = normalize_text(left), normalize_text(right)
    if field == "capacity":
        left, right = left.replace(" ", ""), right.replace(" ", "")
    return left == right


def variant_label(color: str, capacity: str) -> str:
    return " / ".join(part for part in (color.strip(), capacity.strip()) if part)


def _selected(options: list[dict], field: str) -> str:
    selected = [item["value"] for item in options if item["selected"]]
    if options and len(selected) != 1:
        raise NeedsAction(f"Pilihan {field} pada halaman belum dapat dipastikan.")
    return selected[0] if selected else ""


def _validate_configurables(state: dict) -> None:
    """Do not mistake a changed selector layout for a product with no variants."""
    if state.get("unmapped_color_count", 0) or state.get("unmapped_capacity_count", 0):
        raise NeedsAction("Susunan pilihan warna/kapasitas produk ini belum didukung. Selector iBox perlu diperbarui sebelum pemantauan.")
    unsupported = state.get("unsupported_configurables", [])
    if unsupported:
        labels = ", ".join(str(value) for value in unsupported)[:150]
        raise NeedsAction(f"Pilihan konfigurasi produk belum didukung: {labels}. Tidak ada varian bawaan yang dipilih sebagai pengganti.")
    if len(state.get("model_options", [])) > 1:
        raise NeedsAction("Produk ini memiliki beberapa pilihan model yang belum didukung. Gunakan halaman SKU dengan satu model.")


class IBoxAdapter(BrowserAdapter):
    """Actual iBox PDP adapter; automatic checkout is an explicit advanced option."""

    def __init__(self, config: dict | None, data_dir: Path):
        config = merge_config(config)
        problems = validate_live_config(config)
        if problems:
            raise AdapterError("Konfigurasi iBox tidak valid: " + "; ".join(problems))
        super().__init__(config, data_dir)

    @property
    def checkout_issues(self) -> list[str]:
        issues = []
        if self.config.get("checkout", {}).get("enabled") is not True:
            issues.append("Checkout otomatis belum diaktifkan; login dan pemantauan tetap tersedia.")
        issues.extend(validate_checkout_config(self.config))
        for key in ("cart_item_variant", "checkout_item_variant"):
            if not self.selectors.get(key):
                issues.append(f"Isi merchant.selectors.{key} untuk memverifikasi warna dan kapasitas.")
        return issues

    @property
    def checkout_ready(self) -> bool:
        return not self.checkout_issues

    def _route(self, session: dict, route: Any) -> None:
        # Handoff tabs are controlled by the operator, who may choose a payment
        # provider. Automated tabs retain the exact-host navigation guard.
        try:
            if route.request.frame.page in session.get("manual_pages", set()):
                route.continue_()
                return
        except Exception:
            pass
        super()._route(session, route)

    def _response(self, session: dict, response: Any) -> None:
        try:
            if response.request.frame.page in session.get("manual_pages", set()):
                return
        except Exception:
            pass
        super()._response(session, response)

    def _session(self, profile_id: int) -> dict:
        session = super()._session(profile_id)
        session.setdefault("manual_pages", set())
        if session["page"].is_closed():
            try:
                session["page"] = session["context"].new_page()
            except Exception:
                # Closing the last window can close its persistent context.
                # Reopen that same local profile instead of keeping a dead tab.
                self._sessions.pop(profile_id, None)
                try:
                    session["context"].close()
                except Exception:
                    pass
                session = super()._session(profile_id)
                session.setdefault("manual_pages", set())
        return session

    def _manual_page(self, session: dict, role: str = "login"):
        page = session.get(role + "_page")
        if page is not None and not page.is_closed():
            return page
        page = session["context"].new_page()
        session["manual_pages"].add(page)
        session[role + "_page"] = page
        return page

    @staticmethod
    def _product_key(product: dict):
        return product.get("id") or (product["url"], product.get("color", ""), product.get("capacity", ""))

    def _handoff_page(self, session: dict, product: dict):
        pages = session.setdefault("handoff_pages", {})
        key = self._product_key(product)
        page = pages.get(key)
        if page is None or page.is_closed():
            page = session["context"].new_page()
            session["manual_pages"].add(page)
            pages[key] = page
        return page

    @contextmanager
    def _using_page(self, session: dict, page):
        original = session["page"]
        session["page"] = page
        try:
            yield
        finally:
            session["page"] = original

    def _product_url(self, url: str) -> str:
        if not _valid_url(url, IBOX_HOSTS) or not urlsplit(url).path.startswith("/product/"):
            raise AdapterError("Gunakan link produk HTTPS dari ibox.co.id/product/….")
        return url

    def _close_public_popup(self, page):
        # The public marketing popup exposes this accessible button label.
        close = page.get_by_role("button", name="Close Popup", exact=True)
        if close.count() == 1 and close.is_visible():
            close.click(timeout=self.timeout)

    def _read_dom(self, page) -> dict:
        """Read only visible PDP text/attributes, never scripts or browser storage."""
        return page.evaluate("""(selectors) => {
            const all = key => [...document.querySelectorAll(selectors[key])];
            const options = key => all(key).map(el => {
                const parent = el.parentElement;
                const selected = el.dataset.selected === 'true' || parent?.dataset.selected === 'true';
                const interactive = !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                return {value: el.value || '', selected,
                    selectable: interactive,
                    available: interactive && parent?.dataset.disabled !== 'true'};
            });
            const names = all('name'), prices = all('price'), buttons = all('button');
            const desktop = el => !(el.getAttribute('data-testid') || '').includes('_mobile');
            const colors = all('color'), capacities = all('capacity');
            const configured = [...document.querySelectorAll('[data-testid^="qa-pdp-configurable-"]')].filter(desktop);
            const unmatched = (name, captured) => [...document.querySelectorAll('[name="' + name + '"]')]
                .filter(desktop).filter(el => !captured.includes(el)).length;
            return {
                name_count: names.length, price_count: prices.length, button_count: buttons.length,
                name: names[0]?.textContent?.trim() || '',
                price_text: prices[0]?.textContent?.trim() || '',
                button_text: buttons[0]?.textContent?.trim() || '',
                button_disabled: buttons[0]?.disabled === true || buttons[0]?.getAttribute('aria-disabled') === 'true',
                colors: options('color'), capacities: options('capacity'),
                unmapped_color_count: unmatched('Warna', colors),
                unmapped_capacity_count: unmatched('Kapasitas', capacities),
                unsupported_configurables: [...new Set(configured
                    .filter(el => !el.matches('input') || !['Warna', 'Kapasitas', 'Model'].includes(el.getAttribute('name')))
                    .map(el => el.getAttribute('name') || 'kontrol tanpa nama'))],
                model_options: [...document.querySelectorAll('input[name="Model"]')].filter(desktop).map(el => el.value || '')
            };
        }""", PRODUCT_SELECTORS)

    def _stable_state(self, session: dict) -> dict:
        page = session["page"]
        self._one(page, PRODUCT_SELECTORS["name"], "nama produk iBox")
        self._one(page, PRODUCT_SELECTORS["price"], "harga produk iBox")
        self._one(page, PRODUCT_SELECTORS["button"], "tombol pembelian iBox")
        deadline = time.monotonic() + self.timeout / 1000
        previous = None
        while time.monotonic() < deadline:
            self._guard(session)
            current = self._read_dom(page)
            if any(current[key] != 1 for key in ("name_count", "price_count", "button_count")):
                raise NeedsAction("Struktur halaman iBox berubah: nama, harga, atau tombol tidak cocok tepat satu kali.")
            current["url"] = self._product_url(page.url)
            if current == previous:
                _validate_configurables(current)
                current["selected_color"] = _selected(current["colors"], "warna")
                current["selected_capacity"] = _selected(current["capacities"], "kapasitas")
                return current
            previous = current
            page.wait_for_timeout(200)
        raise NeedsAction("Halaman produk masih berubah. Periksa halaman iBox sebelum mencoba kembali.")

    def _choose(self, session: dict, requested: str, field: str, required: bool) -> tuple[dict, str]:
        state = self._stable_state(session)
        key = "colors" if field == "color" else "capacities"
        label = "warna" if field == "color" else "kapasitas"
        options = state[key]
        if not requested:
            if options and required:
                raise NeedsAction(f"Pilih {label} dari hasil Baca produk sebelum mengaktifkan incaran.")
            return state, ""
        matches = [item for item in options if _same_option(item["value"], requested, field)]
        if len(matches) != 1:
            raise NeedsAction(f"Pilihan {label} {requested!r} tidak ditemukan tepat satu kali di produk ini.")
        option = matches[0]
        if option["selected"]:
            return state, ""
        if not option["selectable"]:
            return state, f"Pilihan {label} {option['value']} belum tersedia; varian lain tidak digunakan."
        controls = session["page"].locator(PRODUCT_SELECTORS[field])
        found = [controls.nth(i) for i in range(controls.count())
                 if _same_option(controls.nth(i).input_value(), requested, field)]
        if len(found) != 1:
            raise NeedsAction(f"Kontrol {label} pada halaman berubah. Muat ulang data produk.")
        # No force click: native/ARIA disabled controls and overlays are honored.
        try:
            found[0].click(timeout=self.timeout)
        except Exception as exc:
            raise NeedsAction(f"Pilihan {label} belum dapat diklik. Periksa pop-up atau verifikasi di browser iBox.") from exc
        deadline = time.monotonic() + self.timeout / 1000
        while time.monotonic() < deadline:
            state = self._stable_state(session)
            if _same_option(state["selected_" + field], option["value"], field):
                return state, ""
            session["page"].wait_for_timeout(150)
        raise NeedsAction(f"iBox belum mengonfirmasi pilihan {label}; proses dihentikan.")

    def _load_target(self, session: dict, product: dict, *, required: bool) -> tuple[dict, str]:
        self._goto(session, self._product_url(product.get("url", "")))
        self._close_public_popup(session["page"])
        # Capacity can change which colors exist; choose it before the color.
        state, unavailable = self._choose(session, str(product.get("capacity", "")).strip(), "capacity", required)
        if not unavailable:
            state, unavailable = self._choose(session, str(product.get("color", "")).strip(), "color", required)
        name = str(product.get("name", "")).strip()
        if name and normalize_text(state["name"]) != normalize_text(name):
            raise NeedsAction("Nama produk iBox berbeda dari incaran. Gunakan Baca produk untuk mengisi nama yang sesuai.")
        if not unavailable:
            for field in ("color", "capacity"):
                requested = str(product.get(field, "")).strip()
                if requested and not _same_option(requested, state["selected_" + field], field):
                    raise NeedsAction("iBox mengubah pilihan varian setelah pemilihan. Incaran tidak dilanjutkan.")
        return state, unavailable

    @staticmethod
    def _snapshot(state: dict, product: dict, unavailable: str = "") -> StockSnapshot:
        _validate_configurables(state)
        target_variant = variant_label(str(product.get("color", "")), str(product.get("capacity", "")))
        if unavailable:
            return StockSnapshot(False, None, state["name"], target_variant, unavailable)
        amount = parse_price_idr(state["price_text"])
        if state["button_disabled"]:
            if "stok habis" not in normalize_text(state["button_text"]):
                raise NeedsAction("Tombol pembelian belum aktif tetapi iBox tidak menampilkan Stok habis. Periksa lokasi atau persyaratan pembelian di browser.")
            return StockSnapshot(False, amount, state["name"], target_variant, "iBox menampilkan Stok habis untuk varian yang dipilih.")
        if not state["button_text"]:
            raise NeedsAction("Label tombol pembelian iBox belum dapat diverifikasi.")
        selected_options = [item for key in ("colors", "capacities")
                            for item in state[key] if item["selected"]]
        if any(not item["available"] for item in selected_options):
            raise NeedsAction("Penanda stok iBox bertentangan: tombol pembelian aktif tetapi varian terpilih dinyatakan tidak tersedia. Periksa langsung pada browser.")
        return StockSnapshot(True, amount, state["name"], target_variant,
                             "Harga dan varian dibaca dari halaman iBox; ketersediaan akhir diperiksa lagi saat checkout.")

    def discover(self, product: dict) -> dict:
        session = self._session(int(product["profile_id"]))
        session["payment_phase"], session["block"] = False, ""
        state, unavailable = self._load_target(session, dict(product, name=""), required=False)
        snapshot = self._snapshot(state, product, unavailable)
        return {
            "name": state["name"], "url": state["url"], "price": snapshot.unit_price,
            "available": snapshot.available, "colors": state["colors"], "capacities": state["capacities"],
            "selected_color": state["selected_color"], "selected_capacity": state["selected_capacity"],
            "note": snapshot.note,
        }

    def _open_product(self, session: dict, product: dict) -> StockSnapshot:
        state, unavailable = self._load_target(session, product, required=True)
        return self._snapshot(state, product, unavailable)

    def inspect(self, product: dict) -> StockSnapshot:
        session = self._session(int(product["profile_id"]))
        session["payment_phase"], session["block"] = False, ""
        return self._open_product(session, product)

    def open_login(self, profile_id: int) -> str:
        session = self._session(profile_id)
        page = self._manual_page(session)
        if page.url == "about:blank":
            with self._using_page(session, page):
                self._goto(session, self.merchant["login_url"], guard=False)
        page.bring_to_front()
        return "Browser iBox dibuka. Masuk melalui tombol akun di situs iBox; sesi tersimpan untuk profil ini."

    def prepare_purchase(self, product: dict, snapshot: StockSnapshot | None = None) -> dict:
        session = self._session(int(product["profile_id"]))
        page = self._handoff_page(session, product)
        session["payment_phase"], session["block"] = False, ""
        quantity = product.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 100:
            raise AdapterError("Jumlah target harus berupa integer 1 sampai 100.")
        with self._using_page(session, page):
            latest = self._open_product(session, product)
            if not latest.available or latest.unit_price is None:
                raise NeedsAction("Varian belum tersedia untuk dibeli. Tidak ada barang ditambahkan ke keranjang.")
            for key in ("max_unit_price", "max_total_price"):
                value = product.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_AMOUNT:
                    raise AdapterError("Batas belanja harus berupa Rupiah positif.")
            if latest.unit_price > product["max_unit_price"] or latest.unit_price * quantity > product["max_total_price"]:
                raise NeedsAction("Harga terbaru atau subtotal melampaui batas belanja incaran.")
            count = self._one(page, PRODUCT_SELECTORS["quantity"], "jumlah produk iBox")
            try:
                count.fill(str(quantity), timeout=self.timeout)
                count.press("Tab", timeout=self.timeout)
            except Exception as exc:
                raise NeedsAction("Jumlah produk belum dapat diisi; periksa pembatasan jumlah di halaman iBox.") from exc
            if count.input_value().strip() != str(quantity):
                raise NeedsAction("iBox mengubah jumlah produk. Tidak ada barang ditambahkan ke keranjang.")
            state = self._stable_state(session)
            for field in ("color", "capacity"):
                requested = str(product.get(field, "")).strip()
                if requested and not _same_option(requested, state["selected_" + field], field):
                    raise NeedsAction("iBox mengubah varian saat jumlah diisi; periksa pilihan pada halaman.")
            if normalize_text(state["name"]) != normalize_text(product["name"]):
                raise NeedsAction("Nama produk berubah saat persiapan; checkout belum dilanjutkan.")
            latest = self._snapshot(state, product)
            if not latest.available or latest.unit_price is None:
                raise NeedsAction("Stok berubah saat jumlah diisi; checkout belum dilanjutkan.")
            if latest.unit_price > product["max_unit_price"] or latest.unit_price * quantity > product["max_total_price"]:
                raise NeedsAction("Harga berubah saat persiapan dan melewati batas belanja.")
            self._guard(session)
            page.bring_to_front()
            return {
                "product_url": page.url, "name": latest.name, "variant": latest.variant,
                "quantity": quantity, "unit_price": latest.unit_price,
                "note": "Produk dan jumlah disiapkan di browser iBox. Lanjutkan keranjang, alamat, ongkir, dan pembayaran di situs. Belum ada pesanan dibuat oleh StockPilot; periksa total akhir sendiri.",
            }

    def open_product(self, product: dict) -> dict:
        """Reopen a target without adding anything or creating another session."""
        session = self._session(int(product["profile_id"]))
        page = self._manual_page(session, "preview")
        session["payment_phase"], session["block"] = False, ""
        with self._using_page(session, page):
            state, unavailable = self._load_target(session, product, required=True)
        page.bring_to_front()
        return {"product_url": state["url"], "note": unavailable or "Varian produk dibuka tanpa menambah barang atau mengirim pesanan."}

    def resume_purchase(self, product: dict) -> dict:
        """Focus a handoff tab without navigating away from an ongoing checkout."""
        session = self._session(int(product["profile_id"]))
        page = session.get("handoff_pages", {}).get(self._product_key(product))
        if page is not None and not page.is_closed() and page.url != "about:blank":
            page.bring_to_front()
            return {"product_url": product["url"], "note": "Jendela checkout yang sedang terbuka ditampilkan kembali; halaman tidak dimuat ulang."}
        if product.get("_resume_status"):
            page = self._manual_page(session, "review")
            if page.url == "about:blank":
                with self._using_page(session, page):
                    self._goto(session, self.merchant["login_url"], guard=False)
            page.bring_to_front()
            return {"product_url": product["url"], "note": "Tab checkout sebelumnya tidak tersambung setelah ditutup atau aplikasi dimulai ulang. Periksa keranjang dan riwayat pesanan melalui akun iBox sebelum membuat pesanan lain."}
        return self.open_product(product)

    def checkout(self, product: dict, snapshot: StockSnapshot, before_submit) -> CheckoutResult:
        if not self.checkout_ready:
            raise NeedsAction("Checkout otomatis belum siap. Gunakan Siapkan di browser, atau lengkapi pemetaan checkout yang telah diperiksa.")
        session = self._session(int(product["profile_id"]))
        monitor_page = session["page"]
        earlier_pages = set(session["context"].pages)
        checkout_page = session["context"].new_page()
        session["page"] = checkout_page
        result = None
        try:
            result = super().checkout(product, snapshot, before_submit)
            return result
        finally:
            # A completed, uncertain, or interrupted checkout must never become
            # the next monitoring tab. Keep it available for manual inspection.
            candidates = [page for page in session["context"].pages
                          if page not in earlier_pages and not page.is_closed()]
            session["manual_pages"].update(candidates)
            focus = checkout_page if not checkout_page.is_closed() else None
            if result and result.payment_url:
                focus = next((page for page in candidates if page.url == result.payment_url), focus)
            if focus is not None:
                session.setdefault("handoff_pages", {})[self._product_key(product)] = focus
            session["page"] = monitor_page
