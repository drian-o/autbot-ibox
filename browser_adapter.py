"""Adapter browser yang harus dipetakan ke halaman merchant yang sebenarnya.

Tidak ada endpoint, selector iBox, pengelakan CAPTCHA, atau pembayaran kartu
yang ditebak. Gunakan satu thread untuk seluruh umur instance ini. Sesi login
tersimpan lokal dalam profil Chromium terpisah untuk tiap profil aplikasi.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from contracts import AdapterError, CheckoutResult, NeedsAction, StockSnapshot


MAX_AMOUNT = 1_000_000_000_000
CHECKOUT_SELECTORS = (
    "product_name", "product_price", "in_stock", "quantity_input",
    "add_to_cart", "cart_empty", "cart_items", "cart_item_name",
    "cart_item_quantity", "cart_item_unit_price", "checkout_items", "checkout_item_name",
    "checkout_item_quantity", "checkout_item_unit_price", "checkout_total", "submit_order",
    "payment_ready", "order_ref",
)
ACTION_TYPES = {"click", "fill", "select", "goto"}


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def parse_price_idr(text: str) -> int:
    """Parse satu nilai Rupiah, bukan teks yang berisi beberapa harga.

    Rp 12.500.000,00 dan IDR 12,500,000.00 diterima. Pecahan nonnol,
    kisaran harga, harga coret bersama harga promo, serta angka negatif ditolak.
    """
    cleaned = unicodedata.normalize("NFKC", str(text)).strip()
    match = re.fullmatch(r"(?:(?:Rp\.?|IDR)\s*)?([0-9][0-9.,\s]*)", cleaned, re.I)
    if not match:
        raise AdapterError("Selector harga harus berisi tepat satu nilai Rupiah.")
    number = re.sub(r"\s+", "", match.group(1))
    if "." in number and "," in number:
        decimal = "." if number.rfind(".") > number.rfind(",") else ","
        grouping = "," if decimal == "." else "."
        whole, fraction = number.rsplit(decimal, 1)
        if fraction != "00" or decimal in whole:
            raise AdapterError("Harga pecahan atau format harga ambigu tidak didukung.")
        if not re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(grouping) + r"[0-9]{3})+", whole):
            raise AdapterError("Pemisah ribuan pada harga tidak valid.")
        digits = whole.replace(grouping, "")
    elif "." in number or "," in number:
        separator = "." if "." in number else ","
        parts = number.split(separator)
        if len(parts) == 2 and parts[-1] == "00":
            digits = parts[0]
        elif 1 <= len(parts[0]) <= 3 and all(len(part) == 3 and part.isdigit() for part in parts[1:]):
            digits = "".join(parts)
        else:
            raise AdapterError("Pemisah ribuan pada harga tidak valid.")
    else:
        digits = number
    if not digits.isdigit():
        raise AdapterError("Nilai harga tidak valid.")
    value = int(digits)
    if value <= 0 or value > MAX_AMOUNT:
        raise AdapterError("Harga harus positif dan berada dalam rentang yang didukung.")
    return value


def _hosts(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    result: set[str] = set()
    for raw in value:
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            continue
        name = raw.lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name):
            continue
        if ".." in name or "." not in name and name != "localhost":
            continue
        result.add(name)
    return result


def _valid_url(url: str, hosts: set[str], allow_local: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.hostname not in hosts:
            return False
        if parsed.scheme == "https":
            return parsed.port in (None, 443)
        return bool(
            allow_local and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port is not None
        )
    except (TypeError, ValueError):
        return False


def validate_config(config: dict) -> list[str]:
    """Kembalikan kekurangan konfigurasi tanpa membuka browser atau jaringan."""
    if not isinstance(config, dict):
        return ["Konfigurasi harus berupa object JSON."]
    errors: list[str] = []
    merchant = config.get("merchant", {}) if isinstance(config, dict) else {}
    browser = config.get("browser", {}) if isinstance(config, dict) else {}
    if not isinstance(merchant, dict) or not isinstance(browser, dict):
        return ["merchant dan browser harus berupa object JSON."]
    raw_hosts = merchant.get("allowed_hosts", [])
    hosts = _hosts(raw_hosts)
    if not hosts or not isinstance(raw_hosts, list) or any(not isinstance(item, str) for item in raw_hosts) or len(hosts) != len(raw_hosts):
        errors.append("Isi merchant.allowed_hosts dengan hostname persis, tanpa https/path/wildcard.")
    payment_hosts = merchant.get("allowed_payment_hosts", [])
    if not isinstance(payment_hosts, list) or any(not isinstance(item, str) for item in payment_hosts) or len(_hosts(payment_hosts)) != len(payment_hosts):
        errors.append("merchant.allowed_payment_hosts harus berisi hostname persis.")
    allow_local = config.get("allow_local_test_urls") is True
    for field in ("login_url", "cart_url"):
        if not _valid_url(merchant.get(field, ""), hosts, allow_local):
            errors.append(f"Isi merchant.{field} dengan URL dari allowed_hosts.")
    selectors = merchant.get("selectors", {})
    if not isinstance(selectors, dict):
        selectors = {}
    for key in CHECKOUT_SELECTORS:
        if not isinstance(selectors.get(key), str) or not selectors[key].strip():
            errors.append(f"Isi merchant.selectors.{key} dari halaman merchant.")
    variants = merchant.get("variants", {})
    if not isinstance(variants, dict):
        errors.append("merchant.variants harus berupa object nama varian ke daftar aksi.")
        variants = {}
    if variants:
        for key in ("selected_variant", "cart_item_variant", "checkout_item_variant"):
            if not selectors.get(key):
                errors.append(f"Isi merchant.selectors.{key} untuk memverifikasi varian.")
    action_lists = {
        "clear_cart_actions": merchant.get("clear_cart_actions", []),
        "checkout_actions": merchant.get("checkout_actions", []),
        **{f"variants.{name}": actions for name, actions in variants.items()},
    }
    for label, actions in action_lists.items():
        if not isinstance(actions, list):
            errors.append(f"merchant.{label} harus berupa daftar aksi.")
            continue
        for index, action in enumerate(actions):
            prefix = f"merchant.{label}[{index}]"
            if not isinstance(action, dict) or action.get("action") not in ACTION_TYPES:
                errors.append(f"{prefix}: aksi hanya click, fill, select, atau goto.")
                continue
            kind = action["action"]
            if kind == "goto":
                if not _valid_url(action.get("url", ""), hosts, allow_local):
                    errors.append(f"{prefix}: URL harus berada di allowed_hosts.")
            elif not isinstance(action.get("selector"), str) or not action["selector"].strip():
                errors.append(f"{prefix}: selector wajib diisi.")
            elif action["selector"] == selectors.get("submit_order"):
                errors.append(f"{prefix}: submit_order harus dilakukan hanya oleh adapter setelah validasi.")
            if kind in {"fill", "select"} and not isinstance(action.get("value"), str):
                errors.append(f"{prefix}: value harus berupa string.")
    for key in ("timeout_ms", "payment_timeout_ms"):
        if key in browser and (isinstance(browser[key], bool) or not isinstance(browser[key], int) or not 1000 <= browser[key] <= 120000):
            errors.append(f"browser.{key} harus berupa integer 1000 sampai 120000.")
    if "headless" in browser and not isinstance(browser["headless"], bool):
        errors.append("browser.headless harus berupa boolean.")
    if browser.get("channel") not in (None, "", "chrome", "msedge", "chromium"):
        errors.append("browser.channel hanya chrome, msedge, chromium, atau kosong.")
    return errors


class BrowserAdapter:
    """Checkout satu produk per pesanan; tidak mengulang submit otomatis."""

    def __init__(self, config: dict, data_dir: Path):
        self.config = config
        self.merchant = config.get("merchant", {})
        self.browser = config.get("browser", {})
        self.selectors = self.merchant.get("selectors", {})
        self.allowed_hosts = _hosts(self.merchant.get("allowed_hosts", []))
        self.payment_hosts = _hosts(self.merchant.get("allowed_payment_hosts", []))
        self.allow_local = config.get("allow_local_test_urls") is True
        self.data_dir = Path(data_dir)
        self.timeout = self.browser.get("timeout_ms", 15000)
        self.payment_timeout = self.browser.get("payment_timeout_ms", 30000)
        self._owner: int | None = None
        self._playwright: Any = None
        self._sessions: dict[int, dict] = {}

    def validate_config(self) -> list[str]:
        return validate_config(self.config)

    def _assert_thread(self) -> None:
        thread = threading.get_ident()
        if self._owner is None:
            self._owner = thread
        elif self._owner != thread:
            raise AdapterError("BrowserAdapter harus dipanggil dari satu worker thread yang sama.")

    def _require_config(self) -> None:
        errors = self.validate_config()
        if errors:
            raise AdapterError("Konfigurasi merchant belum lengkap: " + "; ".join(errors[:4]))

    def _session(self, profile_id: int) -> dict:
        self._assert_thread()
        if isinstance(profile_id, bool) or not isinstance(profile_id, int) or profile_id <= 0:
            raise AdapterError("ID profil tidak valid.")
        if profile_id in self._sessions:
            return self._sessions[profile_id]
        try:
            if self._playwright is None:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
            profile_dir = self.data_dir / "browser_profiles" / f"profile_{profile_id}"
            profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            kwargs: dict[str, Any] = {
                "headless": self.browser.get("headless", False),
                "viewport": {"width": 1360, "height": 900},
                "accept_downloads": False,
            }
            if self.browser.get("channel"):
                kwargs["channel"] = self.browser["channel"]
            if self.browser.get("executable_path"):
                kwargs["executable_path"] = str(self.browser["executable_path"])
            # On Windows prefer an existing Edge/Chrome installation when the
            # operator has not explicitly selected a Playwright browser.
            # This avoids downloading Chromium Headless Shell (and ENOSPC).
            if "channel" not in kwargs and "executable_path" not in kwargs:
                from browser_detection import find_preferred_browser
                existing_browser = find_preferred_browser()
                if existing_browser:
                    kwargs["channel"] = existing_browser
            context = self._playwright.chromium.launch_persistent_context(str(profile_dir), **kwargs)
            context.set_default_timeout(self.timeout)
            session = {"context": context, "page": None, "payment_phase": False, "block": ""}
            context.route("**/*", lambda route: self._route(session, route))
            context.on("response", lambda response: self._response(session, response))
            page = context.pages[0] if context.pages else context.new_page()
            session["page"] = page
            self._sessions[profile_id] = session
            return session
        except ImportError as exc:
            raise AdapterError("Pasang browser: pip install -r requirements.txt lalu python -m playwright install chromium.") from exc
        except Exception as exc:
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError("Chromium tidak dapat dibuka. Periksa instalasi Playwright dan tutup profil yang sedang dipakai proses lain.") from exc

    def _route(self, session: dict, route: Any) -> None:
        try:
            request = route.request
            if request.is_navigation_request() and request.frame.parent_frame is None:
                hosts = self.allowed_hosts | (self.payment_hosts if session["payment_phase"] else set())
                if not _valid_url(request.url, hosts, self.allow_local):
                    session["block"] = "Navigasi menuju host di luar konfigurasi dihentikan. Periksa halaman secara manual."
                    route.abort()
                    return
            route.continue_()
        except Exception:
            session["block"] = "Pemeriksaan navigasi browser gagal; proses dihentikan."
            try:
                route.abort()
            except Exception:
                pass

    def _response(self, session: dict, response: Any) -> None:
        try:
            if (
                response.status in (401, 403, 429)
                and urlsplit(response.url).hostname in self.allowed_hosts | self.payment_hosts
                and response.request.resource_type in ("document", "xhr", "fetch")
            ):
                session["block"] = f"Merchant mengembalikan HTTP {response.status}; selesaikan akses secara manual."
        except Exception:
            session["block"] = "Status respons merchant tidak dapat diverifikasi."

    def _visible(self, page: Any, selector: str) -> bool:
        if not selector:
            return False
        locator = page.locator(selector)
        return locator.count() > 0 and locator.first.is_visible()

    def _guard(self, session: dict, page: Any | None = None) -> None:
        if session["block"]:
            raise NeedsAction(session["block"])
        page = page or session["page"]
        if page.is_closed():
            raise NeedsAction("Jendela merchant ditutup; buka sesi login lagi.")
        hosts = self.allowed_hosts | (self.payment_hosts if session["payment_phase"] else set())
        if page.url != "about:blank" and not _valid_url(page.url, hosts, self.allow_local):
            raise NeedsAction("Halaman sekarang berada di luar host yang dikonfigurasi.")
        if self._visible(page, self.selectors.get("challenge", "")):
            raise NeedsAction("Verifikasi CAPTCHA/antibot terlihat. Selesaikan langsung pada browser.")
        if self._visible(page, self.selectors.get("login_required", "")):
            raise NeedsAction("Sesi login membutuhkan tindakan. Login langsung pada browser merchant.")

    def _goto(self, session: dict, url: str, *, guard: bool = True) -> Any:
        if not _valid_url(url, self.allowed_hosts, self.allow_local):
            raise AdapterError("URL produk/checkout harus menggunakan host yang diizinkan.")
        page = session["page"]
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
        except Exception as exc:
            if session["block"]:
                raise NeedsAction(session["block"]) from exc
            raise AdapterError("Halaman merchant gagal dimuat; proses belum dapat dilanjutkan.") from exc
        if guard:
            self._guard(session)
        return page

    def _one(self, scope: Any, selector: str, label: str) -> Any:
        if not selector:
            raise AdapterError(f"Selector {label} belum diisi.")
        locator = scope.locator(selector)
        try:
            locator.wait_for(state="visible", timeout=self.timeout)
            if locator.count() != 1:
                raise NeedsAction(f"Elemen {label} harus cocok tepat satu kali.")
            return locator
        except NeedsAction:
            raise
        except Exception as exc:
            raise NeedsAction(f"Elemen {label} belum dapat diverifikasi. Periksa selector dan halaman merchant.") from exc

    def _text(self, locator: Any) -> str:
        try:
            checked = locator.locator("option:checked")
            if checked.count() == 1:
                return checked.inner_text().strip()
            return locator.input_value().strip()
        except Exception:
            return locator.inner_text().strip()

    def _actions(self, session: dict, actions: list[dict], product: dict) -> None:
        for action in actions:
            self._guard(session)
            kind = action["action"]
            if kind == "goto":
                self._goto(session, action["url"])
                continue
            page = session["page"]
            selector = action["selector"]
            if selector == self.selectors["submit_order"]:
                raise AdapterError("Aksi persiapan tidak boleh mengirim pesanan.")
            locator = self._one(page, selector, "aksi persiapan")
            value = str(action.get("value", "")).replace("{quantity}", str(product["quantity"]))
            value = value.replace("{variant}", str(product.get("variant", "")))
            if kind == "click":
                locator.click(timeout=self.timeout)
            elif kind == "fill":
                locator.fill(value, timeout=self.timeout)
                locator.press("Tab", timeout=self.timeout)
            elif kind == "select":
                locator.select_option(value=value, timeout=self.timeout)
            else:
                raise AdapterError("Aksi konfigurasi tidak dikenal.")
            if action.get("wait_for"):
                self._one(page, action["wait_for"], "hasil aksi")
            self._guard(session)

    def _open_product(self, session: dict, product: dict) -> StockSnapshot:
        page = self._goto(session, product["url"])
        variant = str(product.get("variant", "")).strip()
        if variant:
            actions = self.merchant.get("variants", {}).get(variant)
            if not isinstance(actions, list) or not actions:
                raise NeedsAction("Varian ini belum dipetakan di merchant.variants.")
            self._actions(session, actions, product)
            selected = self._text(self._one(page, self.selectors.get("selected_variant", ""), "varian terpilih"))
            if normalize_text(selected) != normalize_text(variant):
                raise NeedsAction("Varian pada halaman tidak sama dengan varian wishlist.")
        name = self._text(self._one(page, self.selectors["product_name"], "nama produk"))
        if normalize_text(name) != normalize_text(product["name"]):
            raise NeedsAction("Nama produk halaman berbeda dari nama produk wishlist.")
        if self._visible(page, self.selectors.get("out_of_stock", "")):
            return StockSnapshot(False, None, name=name, variant=variant, note="Stok belum tersedia.")
        stock = page.locator(self.selectors["in_stock"])
        available = stock.count() == 1 and stock.is_visible() and stock.is_enabled()
        if not available:
            return StockSnapshot(False, None, name=name, variant=variant, note="Tanda stok tersedia belum terverifikasi.")
        amount = parse_price_idr(self._text(self._one(page, self.selectors["product_price"], "harga produk")))
        self._guard(session)
        return StockSnapshot(True, amount, name=name, variant=variant, note="Stok dan harga terverifikasi pada halaman produk.")

    def inspect(self, product: dict) -> StockSnapshot:
        self._require_config()
        session = self._session(int(product["profile_id"]))
        session["payment_phase"] = False
        session["block"] = ""
        return self._open_product(session, product)

    def open_login(self, profile_id: int) -> str:
        url = self.merchant.get("login_url", "")
        if not _valid_url(url, self.allowed_hosts, self.allow_local):
            raise AdapterError("Isi login_url dan allowed_hosts sebelum membuka sesi login.")
        session = self._session(profile_id)
        session["payment_phase"] = False
        session["block"] = ""
        if session["page"].is_closed():
            session["page"] = session["context"].new_page()
        page = self._goto(session, url, guard=False)
        if session["block"]:
            raise NeedsAction(session["block"])
        page.bring_to_front()
        return "Browser merchant dibuka. Login dan siapkan alamat/metode pembayaran langsung di sana."

    def _empty_cart(self, session: dict, product: dict) -> None:
        page = self._goto(session, self.merchant["cart_url"])
        cart_items = page.locator(self.selectors["cart_items"])
        if cart_items.count() > 0:
            actions = self.merchant.get("clear_cart_actions", [])
            if not actions:
                raise NeedsAction("Keranjang belum kosong. Kosongkan manual sebelum mengaktifkan produk.")
            self._actions(session, actions, product)
        self._one(page, self.selectors["cart_empty"], "keranjang kosong")
        if page.locator(self.selectors["cart_items"]).count() != 0:
            raise NeedsAction("Keranjang masih berisi barang; checkout dihentikan.")

    def _verify_items(self, session: dict, product: dict, prefix: str) -> None:
        page = session["page"]
        rows = page.locator(self.selectors[f"{prefix}_items"])
        self._one(page, self.selectors[f"{prefix}_items"], f"item {prefix}")
        if rows.count() != 1:
            raise NeedsAction("Checkout harus berisi tepat satu item wishlist.")
        row = rows.first
        name = self._text(self._one(row, self.selectors[f"{prefix}_item_name"], f"nama item {prefix}"))
        quantity = self._text(self._one(row, self.selectors[f"{prefix}_item_quantity"], f"jumlah item {prefix}"))
        if normalize_text(name) != normalize_text(product["name"]):
            raise NeedsAction("Nama barang dalam keranjang/checkout tidak sesuai wishlist.")
        if not re.fullmatch(r"[1-9][0-9]*", quantity) or int(quantity) != int(product["quantity"]):
            raise NeedsAction("Jumlah barang dalam keranjang/checkout tidak sesuai target.")
        unit_price = parse_price_idr(self._text(self._one(row, self.selectors[f"{prefix}_item_unit_price"], f"harga satuan item {prefix}")))
        if unit_price > product["max_unit_price"]:
            raise NeedsAction("Harga satuan pada keranjang/checkout melewati batas wishlist.")
        variant = str(product.get("variant", "")).strip()
        if variant:
            actual = self._text(self._one(row, self.selectors.get(f"{prefix}_item_variant", ""), f"varian item {prefix}"))
            if normalize_text(actual) != normalize_text(variant):
                raise NeedsAction("Varian barang dalam keranjang/checkout tidak sesuai wishlist.")
        self._guard(session)

    def checkout(self, product: dict, snapshot: StockSnapshot, before_submit: Callable[[int], None]) -> CheckoutResult:
        self._require_config()
        session = self._session(int(product["profile_id"]))
        session["payment_phase"] = False
        session["block"] = ""
        quantity = product.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 100:
            raise AdapterError("Jumlah barang harus berupa integer 1 sampai 100.")
        for key in ("max_unit_price", "max_total_price"):
            if isinstance(product.get(key), bool) or not isinstance(product.get(key), int) or not 0 < product[key] <= MAX_AMOUNT:
                raise AdapterError("Batas harga satuan dan total wajib berupa Rupiah positif.")
        self._empty_cart(session, product)
        latest = self._open_product(session, product)
        if not latest.available or latest.unit_price is None:
            raise NeedsAction("Stok berubah sebelum checkout. Periksa dan aktifkan ulang bila diperlukan.")
        if latest.unit_price > product["max_unit_price"]:
            raise NeedsAction("Harga satuan terbaru melewati batas wishlist.")
        if latest.unit_price * quantity > product["max_total_price"]:
            raise NeedsAction("Subtotal produk sudah melewati batas total wishlist.")
        page = session["page"]
        quantity_input = self._one(page, self.selectors["quantity_input"], "jumlah produk")
        quantity_input.fill(str(quantity), timeout=self.timeout)
        quantity_input.press("Tab", timeout=self.timeout)
        if self._text(quantity_input) != str(quantity):
            raise NeedsAction("Jumlah produk tidak dapat diatur; periksa batas pembelian merchant.")
        self._guard(session)
        self._one(page, self.selectors["add_to_cart"], "tambah keranjang").click(timeout=self.timeout)
        self._guard(session)
        self._goto(session, self.merchant["cart_url"])
        self._verify_items(session, product, "cart")
        self._actions(session, self.merchant.get("checkout_actions", []), product)
        self._verify_items(session, product, "checkout")
        total = parse_price_idr(self._text(self._one(page, self.selectors["checkout_total"], "total akhir checkout")))
        if total > product["max_total_price"]:
            raise NeedsAction("Total akhir termasuk ongkir/biaya melewati batas wishlist.")
        submit = self._one(page, self.selectors["submit_order"], "kirim pesanan")
        if not submit.is_enabled():
            raise NeedsAction("Tombol pesanan belum aktif; lengkapi checkout pada browser.")
        if self._visible(page, self.selectors["payment_ready"]):
            raise NeedsAction("Penanda pembayaran sudah terlihat sebelum submit; perbaiki pemetaan selector agar konfirmasi tidak keliru.")
        self._guard(session)
        earlier_pages = set(session["context"].pages)
        # Checkpoint transaksional milik engine. Jika gagal, jangan klik submit.
        before_submit(total)
        session["payment_phase"] = True
        try:
            submit.click(timeout=self.timeout)
            return self._await_payment(session, total, earlier_pages)
        except Exception:
            return CheckoutResult(
                "unknown", total=total,
                note="Hasil pengiriman pesanan belum pasti. Periksa riwayat merchant sebelum mencoba lagi; submit tidak diulang.",
            )

    def _await_payment(self, session: dict, total: int, earlier_pages: set) -> CheckoutResult:
        deadline = time.monotonic() + self.payment_timeout / 1000
        order_ref = ""
        while time.monotonic() < deadline:
            if session["block"]:
                return CheckoutResult("unknown", total=total, note="Pesanan sudah dikirim tetapi merchant meminta pemeriksaan akses. Periksa status pesanan manual.")
            pages = list(session["context"].pages)
            ready_page = None
            for page in reversed(pages):
                if page is not session["page"] and page in earlier_pages:
                    continue
                if page.is_closed() or page.url == "about:blank":
                    continue
                self._guard(session, page)
                ref_locator = page.locator(self.selectors["order_ref"])
                if ref_locator.count() == 1 and ref_locator.is_visible():
                    candidate = self._text(ref_locator)
                    if candidate and len(candidate) <= 200:
                        order_ref = candidate
                if self._visible(page, self.selectors["payment_ready"]):
                    ready_page = page
            if ready_page is not None and order_ref:
                ready_page.bring_to_front()
                return CheckoutResult(
                    "awaiting_payment", order_ref=order_ref, payment_url=ready_page.url,
                    total=total, note="Pesanan tercatat dan instruksi pembayaran tersedia. Status ini belum berarti lunas.",
                )
            session["page"].wait_for_timeout(200)
        return CheckoutResult(
            "unknown", order_ref=order_ref, total=total,
            note="Konfirmasi pembayaran belum terverifikasi setelah submit. Periksa riwayat merchant; jangan membuat pesanan kedua.",
        )

    def close(self) -> None:
        self._assert_thread()
        for session in self._sessions.values():
            try:
                session["context"].close()
            except Exception:
                pass
        self._sessions.clear()
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
