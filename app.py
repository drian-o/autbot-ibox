#!/usr/bin/env python3
"""StockPilot: a local Python wishlist, stock monitor and checkout workstation."""
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from contracts import NeedsAction
from engine import Engine
from store import Store, integer, product_url


ROOT = Path(__file__).resolve().parent


class DisabledAdapter:
    def __init__(self, message):
        self.message = message

    def inspect(self, product):
        raise NeedsAction(self.message)

    def checkout(self, *args):
        raise NeedsAction(self.message)

    def open_login(self, profile):
        raise NeedsAction(self.message)

    def close(self):
        pass

    checkout_ready = False
    checkout_issues = []


class Application:
    def __init__(self, store, engine, mode="live", ready=True, message=""):
        self.store, self.engine, self.mode = store, engine, mode
        self.ready, self.message = ready, message
        self.csrf = secrets.token_urlsafe(32)

    def state(self):
        checkout = self.ready and bool(getattr(self.engine.adapter, "checkout_ready", False))
        return dict(self.store.state(), mode="live", version="2.1.0", csrf=self.csrf,
                    monitor=self.engine.status(), adapter_ready=self.ready, login_ready=self.ready,
                    adapter_message=self.message, connection=self.engine.connection(), discovery=self.engine.discovery(),
                    capabilities={"login": self.ready, "inspect": self.ready, "checkout": checkout,
                                  "checkout_mode": "automatic" if checkout else "assisted"},
                    checkout_message=("Pemetaan checkout otomatis dikonfigurasi; hasil tetap harus diverifikasi di iBox."
                                      if checkout else "Pemantauan dan persiapan produk memakai situs iBox asli. Keranjang, pengiriman, dan pembayaran dilanjutkan pengguna di browser."))

    def _require_ready(self):
        if not self.ready:
            raise ValueError(self.message or "Koneksi browser belum siap. Jalankan install_windows.bat.")

    def _validate_action(self, data):
        if data.get("action") == "auto" and not getattr(self.engine.adapter, "checkout_ready", False):
            raise ValueError("Pemetaan checkout otomatis belum lengkap. Pilih Pantau saja atau Siapkan di browser.")

    def _profile(self, value):
        profile_id = integer(value, "Profil")
        if profile_id not in {p["id"] for p in self.store.state()["profiles"]}:
            raise ValueError("Profil tidak ditemukan.")
        return profile_id

    def mutate(self, path: str, data: dict):
        if path == "/api/profiles":
            return {"id": self.store.add_profile(data)}
        if path == "/api/budget":
            self.store.budget(data)
        elif path == "/api/products/discover":
            self._require_ready()
            payload = {"url": product_url(data.get("url", "")), "profile_id": self._profile(data.get("profile_id"))}
            return {"request_id": self.engine.discover_product(payload)}
        elif path == "/api/products":
            self._validate_action(data)
            return {"id": self.store.save_product(data)}
        elif match := re.fullmatch(r"/api/products/(\d+)/open", path):
            self._require_ready()
            self.engine.open_product(self.store.get_product(int(match[1])))
        elif match := re.fullmatch(r"/api/products/(\d+)(/(delete|toggle))?", path):
            product_id = int(match[1])
            if match[3] == "delete":
                self.store.delete(product_id)
            elif match[3] == "toggle":
                self.store.toggle(product_id, data.get("active"))
            else:
                self._validate_action(data)
                self.store.save_product(data, product_id)
        elif match := re.fullmatch(r"/api/orders/([0-9a-f]{32})/open", path):
            self._require_ready()
            order = self.store.get_order(match[1])
            if order["status"] in ("paid_manual", "cancelled_manual", "preparing", "submitting"):
                raise ValueError("Checkout selesai atau masih diproses. Periksa status sebelum membuka kembali.")
            product = self.store.get_product(order["product_id"])
            self.engine.open_product(dict(product, quantity=order["quantity"], _resume_status=order["status"]), resume=True)
        elif match := re.fullmatch(r"/api/orders/([0-9a-f]{32})/resolve", path):
            self.store.resolve(match[1], data)
        elif path == "/api/monitor/stop":
            self.engine.stop_monitor()
        elif path in ("/api/monitor/start", "/api/monitor/check", "/api/login"):
            self._require_ready()
            if path.endswith("start"):
                self.engine.start_monitor()
            elif path.endswith("check"):
                self.engine.request_check()
            else:
                self.engine.open_login(self._profile(data.get("profile_id")))
        else:
            raise ValueError("Aksi tidak ditemukan.")
        return {}


def make_handler(application: Application):
    class Handler(BaseHTTPRequestHandler):
        server_version = "StockPilot"

        def log_message(self, _format, *args):
            # Avoid logging product URLs, payment links or browser session tokens.
            pass

        def _trusted(self) -> bool:
            port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host", "") not in hosts:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in {"http://" + host for host in hosts}

        def _send(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data):
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not self._trusted():
                self._json(403, {"error": "Akses hanya dari dashboard lokal."})
                return
            path = urlsplit(self.path).path
            if path == "/api/state":
                self._json(200, application.state())
                return
            static = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
            if path in static:
                file = ROOT / "static" / static[path]
                if file.is_file():
                    mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    self._send(200, file.read_bytes(), mime + "; charset=utf-8")
                else:
                    self._json(404, {"error": "Berkas dashboard tidak ditemukan."})
                return
            self._json(404, {"error": "Halaman tidak ditemukan."})

        def do_POST(self):
            token = self.headers.get("X-CSRF-Token", "")
            if not self._trusted() or not token.isascii() or not secrets.compare_digest(token, application.csrf):
                self._json(403, {"error": "Sesi dashboard tidak valid. Muat ulang halaman."})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json(415, {"error": "Gunakan JSON."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("Ukuran permintaan tidak valid.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Permintaan harus berupa objek JSON.")
                result = application.mutate(urlsplit(self.path).path, data)
                self._json(200, {"ok": True, **result})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)[:500]})
            except Exception:
                self._json(500, {"error": "Aksi gagal diproses. Periksa status dashboard sebelum mengulang."})

    return Handler


class InstanceLock:
    """OS-held lock prevents two local servers from owning one browser/database."""
    def __init__(self, path: Path):
        self.handle = open(path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                self.handle.seek(0)
                if not self.handle.read(1):
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise ValueError("Data ini sedang dipakai instance StockPilot lain. Tutup instance tersebut lebih dulu.") from None

    def close(self):
        self.handle.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="StockPilot iBox — pantau stok asli dan siapkan pembelian di browser.")
    parser.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, help="Konfigurasi browser/checkout lanjutan (opsional).")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Port harus antara 1024 dan 65535.")
    mode = "live"
    data_dir = args.data_dir.expanduser().resolve() / "ibox_live_v2"
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        instance = InstanceLock(data_dir / "app.lock")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    ready = True
    try:
        from ibox_adapter import IBoxAdapter, default_config, merge_config, validate_live_config
        config = default_config()
        if args.config:
            custom = json.loads(args.config.read_text(encoding="utf-8-sig"))
            if not isinstance(custom, dict):
                raise ValueError("Akar konfigurasi harus berupa object JSON.")
            config = merge_config(custom)
        problems = validate_live_config(config)
        if problems:
            raise ValueError("; ".join(problems[:4]))
        adapter = IBoxAdapter(config, data_dir)
        message = "iBox asli: tambah link produk lalu klik Baca produk iBox. Login dilakukan langsung di browser iBox."
    except (OSError, ValueError, ImportError, TypeError) as exc:
        ready = False
        message = "Browser iBox belum siap: " + str(exc).splitlines()[0][:400]
        adapter = DisabledAdapter(message)
    store = Store(data_dir / "stockpilot.sqlite3", mode)
    recovered = store.recover()
    if recovered:
        store.event(f"{recovered} checkout terputus perlu diperiksa di riwayat merchant.", "warning")
    engine = Engine(store, adapter, mode, args.interval)
    application = Application(store, engine, mode, ready, message)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(application))
    except OSError as exc:
        instance.close()
        print(f"Tidak dapat membuka port {args.port}: {exc}. Coba --port 8778.", file=sys.stderr)
        return 1
    engine.start_thread()
    address = f"http://127.0.0.1:{args.port}"
    print(f"StockPilot iBox v2.1 [LIVE - BUKAN DEMO]  {address}", flush=True)
    print(message, flush=True)
    print("Batas belanja dalam Rupiah penuh. Ctrl+C untuk menutup.", flush=True)
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nMenutup StockPilot…", flush=True)
    finally:
        engine.shutdown()
        server.server_close()
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
