"""One serial worker owns the browser, checks stock and creates at most one order per target."""
from __future__ import annotations

import queue
import threading
import time
import uuid
from copy import deepcopy

from contracts import AdapterError, NeedsAction
from store import now


class Engine:
    def __init__(self, store, adapter, mode: str = "live", interval: int = 60):
        self.store, self.adapter, self.mode = store, adapter, mode
        self.interval = max(60, int(interval))
        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._cancel_cycle = threading.Event()
        self._wake = threading.Event()
        self._commands = queue.Queue(maxsize=10)
        self._thread = None
        self._running = False
        self._busy = False
        self._pending_check = False
        self._last_run = None
        self._last_error = ""
        self._next_run = 0.0
        self._connection = {"status": "not_checked", "message": "Belum membaca halaman produk iBox.", "last_checked": None}
        self._discovery = {"busy": False, "request_id": "", "result": None, "error": ""}

    def status(self) -> dict:
        with self._lock:
            return {"running": self._running, "busy": self._busy,
                    "last_run": self._last_run, "last_error": self._last_error, "interval": self.interval}

    def connection(self) -> dict:
        with self._lock:
            return dict(self._connection)

    def discovery(self) -> dict:
        with self._lock:
            return deepcopy(self._discovery)

    def _connected(self):
        with self._lock:
            self._connection = {"status": "connected", "message": "Halaman produk iBox berhasil dibaca. Status ini bukan verifikasi login atau pembayaran.", "last_checked": now()}

    def _queue(self, command, payload):
        try:
            self._commands.put_nowait((command, payload))
        except queue.Full:
            raise ValueError("Antrean browser penuh; tunggu proses yang sedang berjalan.") from None
        self._wake.set()

    def discover_product(self, product: dict) -> str:
        request_id = uuid.uuid4().hex
        with self._lock:
            if self._discovery["busy"]:
                raise ValueError("Pembacaan produk masih berjalan. Tunggu hasilnya di formulir.")
            self._queue("discover", dict(product, request_id=request_id))
            self._discovery = {"request_id": request_id, "url": product["url"], "profile_id": product["profile_id"], "busy": True, "result": None, "error": ""}
        return request_id

    def open_product(self, product: dict, resume=False):
        self._queue("resume" if resume else "product", dict(product))

    def start_thread(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="stockpilot-worker", daemon=True)
            self._thread.start()

    def start_monitor(self):
        with self._lock:
            self._running = True
            self._cancel_cycle.clear()
        self._wake.set()
        self.store.event("Pemantauan dimulai. Hanya produk aktif yang diperiksa.")

    def stop_monitor(self):
        with self._lock:
            self._running = False
            self._pending_check = False
            self._cancel_cycle.set()
        self.store.event("Pemantauan dihentikan. Checkout yang sudah berjalan diselesaikan sampai statusnya diketahui.")

    def request_check(self):
        with self._lock:
            if self._busy or self._pending_check:
                raise ValueError("Pemeriksaan masih berjalan atau sudah masuk antrean.")
            if self.mode == "live" and time.monotonic() < self._next_run:
                raise ValueError("Tunggu interval pemeriksaan berikutnya sebelum mengecek kembali.")
            self._pending_check = True
            self._cancel_cycle.clear()
        self._wake.set()

    def open_login(self, profile_id: int):
        self._queue("login", profile_id)

    def run_cycle(self):
        if not self._cycle_lock.acquire(blocking=False):
            return
        with self._lock:
            self._busy = True
            self._last_error = ""
        try:
            for product in self.store.active_products():
                if self._stop.is_set() or self._cancel_cycle.is_set():
                    break
                try:
                    snapshot = self.adapter.inspect(product)
                    self._connected()
                    if not snapshot.available:
                        self.store.observation(product["id"], snapshot, "out_of_stock", snapshot.note or "Stok belum tersedia.", product["revision"])
                        continue
                    if isinstance(snapshot.unit_price, bool) or not isinstance(snapshot.unit_price, int) or snapshot.unit_price <= 0:
                        raise NeedsAction("Harga tidak dapat dibaca secara pasti. Periksa pemetaan halaman.")
                    if snapshot.unit_price > product["max_unit_price"]:
                        self.store.observation(product["id"], snapshot, "price_above_limit", "Harga satuan di atas batas yang ditentukan.", product["revision"])
                        continue
                    self.store.observation(product["id"], snapshot, "available", snapshot.note or "Stok tersedia dan harga sesuai batas per unit.", product["revision"])
                    action = product.get("action", "notify")
                    if action == "notify":
                        if product["status"] != "available":
                            self.store.event(f"Stok tersedia sesuai batas harga: {product['name']} ({product['variant'] or 'tanpa varian'}).")
                        continue
                    if action == "auto" and not getattr(self.adapter, "checkout_ready", False):
                        raise NeedsAction("Checkout otomatis belum memiliki pemetaan yang lengkap. Pilih Pantau saja atau Siapkan di browser.")
                    if self._stop.is_set() or self._cancel_cycle.is_set():
                        break
                    order = self.store.claim(product, snapshot.unit_price)
                    if order is None:
                        continue
                    actual_product = dict(product, quantity=order["quantity"])
                    try:
                        if action == "assist":
                            prepared = self.adapter.prepare_purchase(actual_product, snapshot)
                            self.store.finish_manual(order["id"], prepared)
                            self.store.event(f"Produk siap di browser iBox: {product['name']}. Lanjutkan keranjang dan checkout di sana; belum ada pesanan terverifikasi.")
                        else:
                            result = self.adapter.checkout(actual_product, snapshot,
                                                           lambda total: self.store.before_submit(order["id"], total))
                            self.store.finish(order["id"], result)
                            label = "Instruksi pembayaran terbaca" if result.status == "awaiting_payment" else "Checkout perlu diperiksa"
                            self.store.event(f"{label}: {product['name']} ({order['quantity']} unit).")
                    except Exception as exc:
                        message = self._error(exc)
                        self.store.checkout_error(order["id"], message)
                        self._remember_error(message)
                        self.store.event(f"Checkout {product['name']} ditahan: {message}", "warning")
                except (NeedsAction, AdapterError) as exc:
                    message = self._error(exc)
                    self.store.problem(product["id"], message, pause=True, revision=product["revision"])
                    self._remember_error(message)
                    self.store.event(f"{product['name']}: {message}", "warning")
                except Exception as exc:
                    message = self._error(exc)
                    # Unexpected failures also stop the target; do not hammer a broken integration.
                    self.store.problem(product["id"], message, pause=True, revision=product["revision"])
                    self._remember_error(message)
                    self.store.event(f"Pemeriksaan {product['name']} dihentikan: {message}", "error")
        finally:
            with self._lock:
                self._busy = False
                self._last_run = now()
                self._next_run = time.monotonic() + self.interval
            self._cycle_lock.release()

    @staticmethod
    def _error(exc: Exception) -> str:
        # Browser exceptions can carry page content or URLs with tokens. Keep only the first line.
        first = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        return first[:400]

    def _remember_error(self, message):
        with self._lock:
            self._last_error = message
            self._connection = {"status": "needs_action", "message": message, "last_checked": now()}

    def _run_command(self, command, payload):
        with self._lock:
            self._busy = True
            if command == "discover":
                self._connection = {"status": "checking", "message": "Membaca halaman produk iBox…", "last_checked": self._connection["last_checked"]}
        try:
            if command == "login":
                self.store.event(self.adapter.open_login(payload))
            elif command == "discover":
                result = self.adapter.discover(payload)
                self._connected()
                with self._lock:
                    self._discovery.update(busy=False, result=result, error="")
            elif command in ("product", "resume"):
                result = (self.adapter.resume_purchase(payload) if command == "resume"
                          else self.adapter.open_product(payload))
                self.store.event(result.get("note", "Produk iBox dibuka di browser.") if isinstance(result, dict) else str(result))
        except Exception as exc:
            message = self._error(exc)
            self._remember_error(message)
            self.store.event("Browser iBox: " + message, "warning")
            if command == "discover":
                with self._lock:
                    self._discovery.update(busy=False, result=None, error=message)
        finally:
            with self._lock:
                self._busy = False

    def _loop(self):
        try:
            while not self._stop.is_set():
                try:
                    command, payload = self._commands.get_nowait()
                except queue.Empty:
                    command = None
                if command is not None:
                    self._run_command(command, payload)
                    continue
                with self._lock:
                    should_run = self._pending_check or (self._running and time.monotonic() >= self._next_run)
                    self._pending_check = False
                if should_run:
                    self.run_cycle()
                self._wake.wait(timeout=0.5)
                self._wake.clear()
        finally:
            try:
                self.adapter.close()
            except Exception:
                pass

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
