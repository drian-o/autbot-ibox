"""SQLite state, reservations and idempotency for a local single-operator tool."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


PENDING = ("preparing", "manual_checkout", "submitting", "awaiting_payment", "needs_action", "unknown")
COMMITTED = (*PENDING, "paid_manual")
MAX_MONEY = 1_000_000_000_000


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def integer(value, label: str, minimum: int = 1, maximum: int = MAX_MONEY) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{label} harus bilangan bulat.")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} harus bilangan bulat.") from None
    if not minimum <= result <= maximum:
        raise ValueError(f"{label} harus antara {minimum:,} dan {maximum:,}.")
    return result


def text(value, label: str, maximum: int = 240, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} harus teks.")
    result = " ".join(value.strip().split())
    if (not result and not optional) or len(result) > maximum:
        raise ValueError(f"{label} wajib diisi, maksimal {maximum} karakter.")
    return result


def product_url(value) -> str:
    """Accept only public product pages on the actual Indonesian merchant."""
    raw = text(value, "URL produk", 2048)
    try:
        parsed = urlsplit(raw)
        valid = (parsed.scheme == "https" and parsed.hostname in {"ibox.co.id", "www.ibox.co.id"}
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and parsed.path.startswith("/product/") and len(parsed.path) > len("/product/"))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Gunakan link produk https://ibox.co.id/product/… dari situs iBox Indonesia.")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path, parsed.query, ""))


def product_data(data: dict, mode: str = "live") -> dict:
    if not isinstance(data, dict):
        raise ValueError("Data produk harus berupa objek JSON.")
    url = product_url(data.get("url", ""))
    color = text(data.get("color", ""), "Warna", 120, optional=True)
    capacity = text(data.get("capacity", ""), "Kapasitas", 120, optional=True)
    variant = " / ".join(value for value in (color, capacity) if value)
    if not variant:
        variant = text(data.get("variant", ""), "Varian", optional=True)
    action = data.get("action", "notify")
    if action not in ("notify", "assist", "auto"):
        raise ValueError("Aksi harus pantau saja, siapkan browser, atau checkout otomatis.")
    quantity = integer(data.get("quantity", 1), "Jumlah target", maximum=100)
    unit = integer(data.get("max_unit_price"), "Harga satuan maksimum")
    total = integer(data.get("max_total_price"), "Total maksimum")
    if total < quantity * unit:
        raise ValueError("Total maksimum harus mencakup jumlah × harga satuan maksimum; tambahkan ruang untuk ongkir/biaya.")
    active = data.get("active", False)
    if not isinstance(active, bool):
        raise ValueError("Status aktif harus true/false.")
    return {
        "profile_id": integer(data.get("profile_id"), "Profil"),
        "name": text(data.get("name", ""), "Nama produk"),
        "url": url,
        "variant": variant,
        "color": color,
        "capacity": capacity,
        "action": action,
        "quantity": quantity,
        "max_unit_price": unit,
        "max_total_price": total,
        "priority": integer(data.get("priority", 5), "Prioritas", maximum=99),
        "active": int(active),
        "status": "watching" if active else "paused",
        "mode": mode,
    }


class Store:
    def __init__(self, path: Path, mode: str = "live"):
        if mode != "live":
            raise ValueError("StockPilot iBox hanya memakai data situs asli.")
        self.path, self.mode = Path(path), mode
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, budget INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES profiles(id),
                name TEXT NOT NULL, url TEXT NOT NULL, variant TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '', capacity TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT 'notify',
                quantity INTEGER NOT NULL, max_unit_price INTEGER NOT NULL, max_total_price INTEGER NOT NULL,
                priority INTEGER NOT NULL DEFAULT 5, active INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, last_price INTEGER, last_stock INTEGER,
                last_checked TEXT, note TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS unique_target ON products(profile_id,url,variant,mode) WHERE deleted=0;
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES profiles(id),
                product_id INTEGER NOT NULL REFERENCES products(id), product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL, reserved_total INTEGER NOT NULL, total INTEGER,
                status TEXT NOT NULL, order_ref TEXT NOT NULL DEFAULT '', payment_url TEXT NOT NULL DEFAULT '',
                product_url TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_order ON orders(product_id)
              WHERE status IN ('preparing','manual_checkout','submitting','awaiting_payment','needs_action','unknown');
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
            );
            """)
            # Preserve existing live databases while new installations start with no spend authority.
            for table, column, declaration in (
                ("products", "color", "TEXT NOT NULL DEFAULT ''"),
                ("products", "capacity", "TEXT NOT NULL DEFAULT ''"),
                ("products", "action", "TEXT NOT NULL DEFAULT 'notify'"),
                ("orders", "product_url", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            db.execute("DROP INDEX IF EXISTS one_pending_order")
            db.execute("CREATE UNIQUE INDEX one_pending_order ON orders(product_id) WHERE status IN ('preparing','manual_checkout','submitting','awaiting_payment','needs_action','unknown')")
            db.execute("INSERT OR IGNORE INTO profiles(id,name,budget) VALUES(1,'Saya',0)")

    @contextmanager
    def db(self):
        with self.lock:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def event(self, message: str, level: str = "info"):
        with self.db() as db:
            db.execute("INSERT INTO events(created_at,level,message) VALUES(?,?,?)", (now(), level, str(message)[:1000]))
            db.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-500 FROM events)")

    @staticmethod
    def _committed(db, profile_id: int, mode: str) -> int:
        return db.execute(
            "SELECT COALESCE(SUM(reserved_total),0) FROM orders WHERE profile_id=? AND mode=? AND status != 'cancelled_manual'",
            (profile_id, mode),
        ).fetchone()[0]

    @staticmethod
    def _pending(db, product_id: int) -> bool:
        marks = ",".join("?" * len(PENDING))
        return bool(db.execute(f"SELECT 1 FROM orders WHERE product_id=? AND status IN ({marks})", (product_id, *PENDING)).fetchone())

    def state(self) -> dict:
        with self.db() as db:
            profiles = [dict(row) for row in db.execute("SELECT * FROM profiles ORDER BY id")]
            for profile in profiles:
                profile["committed"] = self._committed(db, profile["id"], self.mode)
            products = [dict(row) for row in db.execute("SELECT * FROM products WHERE deleted=0 AND mode=? ORDER BY priority,id", (self.mode,))]
            for product in products:
                product["active"] = bool(product["active"])
                if product["last_stock"] is not None:
                    product["last_stock"] = bool(product["last_stock"])
            orders = [dict(row) for row in db.execute("SELECT * FROM orders WHERE mode=? ORDER BY created_at DESC,rowid DESC LIMIT 200", (self.mode,))]
            events = [dict(row) for row in db.execute("SELECT created_at,level,message FROM events ORDER BY id DESC LIMIT 80")]
        return {"profiles": profiles, "products": products, "orders": orders, "events": events}

    def add_profile(self, data: dict) -> int:
        name = text(data.get("name", ""), "Nama profil", 60)
        budget = integer(data.get("budget"), "Anggaran profil", minimum=0)
        try:
            with self.db() as db:
                result = db.execute("INSERT INTO profiles(name,budget) VALUES(?,?)", (name, budget))
                return result.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("Nama profil sudah ada.") from None

    def budget(self, data: dict):
        profile_id = integer(data.get("profile_id"), "Profil")
        budget = integer(data.get("budget"), "Anggaran profil", minimum=0)
        with self.db() as db:
            if not db.execute("SELECT 1 FROM profiles WHERE id=?", (profile_id,)).fetchone():
                raise ValueError("Profil tidak ditemukan.")
            if budget < self._committed(db, profile_id, self.mode):
                raise ValueError("Anggaran tidak boleh di bawah total pesanan yang sudah dipesan atau ditahan.")
            db.execute("UPDATE profiles SET budget=? WHERE id=?", (budget, profile_id))

    def save_product(self, data: dict, product_id: int | None = None) -> int:
        values = product_data(data, self.mode)
        try:
            with self.db() as db:
                if not db.execute("SELECT 1 FROM profiles WHERE id=?", (values["profile_id"],)).fetchone():
                    raise ValueError("Profil tidak ditemukan.")
                if product_id is None:
                    columns = ",".join(values)
                    placeholders = ",".join("?" * len(values))
                    row = db.execute(f"INSERT INTO products({columns}) VALUES({placeholders})", tuple(values.values()))
                    return row.lastrowid
                existing = db.execute("SELECT * FROM products WHERE id=? AND deleted=0 AND mode=?", (product_id, self.mode)).fetchone()
                if not existing:
                    raise ValueError("Produk tidak ditemukan.")
                if self._pending(db, product_id):
                    raise ValueError("Produk memiliki pesanan aktif. Selesaikan atau rekonsiliasi pesanan lebih dulu.")
                history = db.execute("SELECT 1 FROM orders WHERE product_id=?", (product_id,)).fetchone()
                if history and any(values[k] != existing[k] for k in ("profile_id", "url", "variant", "name")):
                    raise ValueError("Identitas produk yang sudah memiliki riwayat pesanan tidak dapat diubah. Tambahkan produk baru.")
                assignments = ",".join(f"{key}=?" for key in values)
                changed_target = any(values[k] != existing[k] for k in ("profile_id", "url", "variant", "name", "color", "capacity"))
                reset = ",last_price=NULL,last_stock=NULL,last_checked=NULL" if changed_target else ""
                db.execute(f"UPDATE products SET {assignments},revision=revision+1,note=''{reset} WHERE id=?", (*values.values(), product_id))
                return product_id
        except sqlite3.IntegrityError:
            raise ValueError("Produk dan varian tersebut sudah ada di profil ini.") from None

    def toggle(self, product_id: int, active: bool):
        if not isinstance(active, bool):
            raise ValueError("Status aktif harus true/false.")
        with self.db() as db:
            row = db.execute("SELECT * FROM products WHERE id=? AND deleted=0 AND mode=?", (product_id, self.mode)).fetchone()
            if not row:
                raise ValueError("Produk tidak ditemukan.")
            if active and self._pending(db, product_id):
                raise ValueError("Masih ada pesanan aktif atau belum pasti untuk produk ini.")
            paid = db.execute("SELECT COALESCE(SUM(quantity),0) FROM orders WHERE product_id=? AND status='paid_manual'", (product_id,)).fetchone()[0]
            if active and paid >= row["quantity"]:
                raise ValueError("Target jumlah sudah terpenuhi. Naikkan jumlah target jika ingin memesan tambahan.")
            db.execute("UPDATE products SET active=?,status=?,revision=revision+1 WHERE id=?", (int(active), "watching" if active else "paused", product_id))

    def delete(self, product_id: int):
        with self.db() as db:
            if self._pending(db, product_id):
                raise ValueError("Produk masih memiliki pesanan aktif atau belum pasti.")
            result = db.execute("UPDATE products SET deleted=1,active=0,revision=revision+1 WHERE id=? AND mode=? AND deleted=0", (product_id, self.mode))
            if not result.rowcount:
                raise ValueError("Produk tidak ditemukan.")

    def active_products(self) -> list[dict]:
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM products WHERE mode=? AND active=1 AND deleted=0 ORDER BY priority,id", (self.mode,))]

    def get_product(self, product_id: int) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM products WHERE id=? AND mode=? AND deleted=0", (product_id, self.mode)).fetchone()
            if not row:
                raise ValueError("Produk tidak ditemukan.")
            return dict(row)

    def get_order(self, order_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM orders WHERE id=? AND mode=?", (order_id, self.mode)).fetchone()
            if not row:
                raise ValueError("Checkout tidak ditemukan.")
            return dict(row)

    def observation(self, product_id: int, snapshot, status: str, note: str = "", revision: int | None = None):
        with self.db() as db:
            extra = " AND revision=?" if revision is not None else ""
            args = (snapshot.unit_price, int(snapshot.available), now(), status, note[:1000], product_id)
            db.execute("UPDATE products SET last_price=?,last_stock=?,last_checked=?,status=?,note=? WHERE id=? AND active=1 AND deleted=0" + extra,
                       args + ((revision,) if revision is not None else ()))

    def problem(self, product_id: int, message: str, pause: bool = True, revision: int | None = None):
        with self.db() as db:
            extra = " AND revision=?" if revision is not None else ""
            args = (message[:1000], now(), product_id) + ((revision,) if revision is not None else ())
            if pause:
                db.execute("UPDATE products SET active=0,status='needs_action',note=?,last_checked=?,last_stock=NULL,last_price=NULL WHERE id=?" + extra, args)
            else:
                db.execute("UPDATE products SET status='error',note=?,last_checked=?,last_stock=NULL,last_price=NULL WHERE id=?" + extra, args)

    def claim(self, product: dict, unit_price: int) -> dict | None:
        """Reserve the entire configured cap before interacting with a cart."""
        with self.db() as db:
            current = db.execute("SELECT * FROM products WHERE id=?", (product["id"],)).fetchone()
            if not current or not current["active"] or current["deleted"] or current["revision"] != product["revision"]:
                return None
            if self._pending(db, product["id"]):
                return None
            if db.execute("SELECT 1 FROM orders WHERE profile_id=? AND mode=? AND status IN ('preparing','manual_checkout','submitting','needs_action','unknown')",
                          (current["profile_id"], self.mode)).fetchone():
                db.execute("UPDATE products SET active=0,status='needs_action',note=? WHERE id=?",
                           ("Profil memiliki checkout yang perlu diperiksa. Rekonsiliasi pesanan lalu aktifkan kembali produk ini.", product["id"]))
                return None
            paid = db.execute("SELECT COALESCE(SUM(quantity),0) FROM orders WHERE product_id=? AND status='paid_manual'", (product["id"],)).fetchone()[0]
            remaining = current["quantity"] - paid
            if remaining <= 0:
                db.execute("UPDATE products SET active=0,status='completed' WHERE id=?", (product["id"],))
                return None
            if not isinstance(unit_price, int) or unit_price <= 0 or unit_price > current["max_unit_price"]:
                return None
            if unit_price * remaining > current["max_total_price"]:
                return None
            budget = db.execute("SELECT budget FROM profiles WHERE id=?", (current["profile_id"],)).fetchone()[0]
            reserved = self._committed(db, current["profile_id"], self.mode)
            if reserved + current["max_total_price"] > budget:
                db.execute("UPDATE products SET status='budget_blocked',note=? WHERE id=?", ("Anggaran tersisa belum cukup untuk batas total pesanan ini.", product["id"]))
                return None
            order_id, stamp = uuid.uuid4().hex, now()
            db.execute("""INSERT INTO orders(id,profile_id,product_id,product_name,quantity,reserved_total,status,mode,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,'preparing',?,?,?)""",
                       (order_id, current["profile_id"], current["id"], current["name"], remaining, current["max_total_price"], self.mode, stamp, stamp))
            db.execute("UPDATE products SET active=0,status='preparing',note=? WHERE id=?", ("Persiapan checkout; belum ada bukti pesanan tercatat di iBox.", current["id"]))
            return dict(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())

    def finish_manual(self, order_id: str, prepared: dict):
        """A prepared browser is a local reservation, never a merchant invoice."""
        url = product_url(prepared.get("product_url", ""))
        note = str(prepared.get("note", ""))[:1000]
        with self.db() as db:
            order = db.execute("SELECT * FROM orders WHERE id=? AND mode=?", (order_id, self.mode)).fetchone()
            if not order or order["status"] != "preparing":
                raise ValueError("Persiapan checkout sudah diproses.")
            db.execute("UPDATE orders SET status='manual_checkout',product_url=?,note=?,updated_at=? WHERE id=?", (url, note, now(), order_id))
            db.execute("UPDATE products SET active=0,status='manual_checkout',note=? WHERE id=?", (note, order["product_id"]))

    def before_submit(self, order_id: str, total: int):
        total = integer(total, "Total checkout")
        with self.db() as db:
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["status"] != "preparing":
                raise ValueError("Pesanan sudah diproses; pengiriman ulang dihentikan.")
            if total > order["reserved_total"]:
                raise ValueError("Total checkout melebihi limit yang sudah ditetapkan.")
            profile = db.execute("SELECT * FROM profiles WHERE id=?", (order["profile_id"],)).fetchone()
            if self._committed(db, order["profile_id"], self.mode) - order["reserved_total"] + total > profile["budget"]:
                raise ValueError("Total checkout melebihi anggaran profil.")
            # Persist intent BEFORE the external submit. A crash afterward requires reconciliation.
            db.execute("UPDATE orders SET status='submitting',total=?,updated_at=? WHERE id=?", (total, now(), order_id))

    def finish(self, order_id: str, result):
        if result.status not in ("awaiting_payment", "needs_action", "unknown"):
            raise ValueError("Status adapter tidak dikenal.")
        with self.db() as db:
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["status"] not in ("preparing", "submitting"):
                raise ValueError("Transisi status pesanan tidak valid.")
            if result.status == "awaiting_payment" and order["status"] != "submitting":
                raise ValueError("Adapter melaporkan pembayaran tanpa pencatatan pengiriman pesanan.")
            if result.total is not None and result.total != order["total"]:
                raise ValueError("Total sesudah checkout berubah; periksa pesanan di merchant.")
            payment = result.payment_url or ""
            if payment:
                split = urlsplit(payment)
                if split.scheme != "https" or not split.hostname or split.username or split.password:
                    raise ValueError("Alamat pembayaran tidak valid.")
            db.execute("UPDATE orders SET status=?,order_ref=?,payment_url=?,note=?,updated_at=? WHERE id=?",
                       (result.status, result.order_ref[:200], payment[:2048], result.note[:1000], now(), order_id))
            if result.status == "awaiting_payment":
                db.execute("UPDATE orders SET reserved_total=total WHERE id=?", (order_id,))
            product_status = "ordered" if result.status == "awaiting_payment" else "needs_action"
            db.execute("UPDATE products SET status=?,note=? WHERE id=?", (product_status, result.note[:1000], order["product_id"]))

    def checkout_error(self, order_id: str, message: str):
        with self.db() as db:
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order or order["status"] not in ("preparing", "submitting"):
                return
            status = "unknown" if order["status"] in ("submitting", "unknown") else "needs_action"
            db.execute("UPDATE orders SET status=?,note=?,updated_at=? WHERE id=?", (status, message[:1000], now(), order_id))
            db.execute("UPDATE products SET active=0,status='needs_action',note=? WHERE id=?", (message[:1000], order["product_id"]))

    def resolve(self, order_id: str, data: dict):
        status = data.get("status")
        if status not in ("paid_manual", "cancelled_manual"):
            raise ValueError("Pilih lunas atau dibatalkan setelah memeriksa pesanan di merchant.")
        note = text(data.get("note", ""), "Catatan pemeriksaan", 500)
        with self.db() as db:
            order = db.execute("SELECT * FROM orders WHERE id=? AND mode=?", (order_id, self.mode)).fetchone()
            if not order or order["status"] not in ("manual_checkout", "awaiting_payment", "needs_action", "unknown"):
                raise ValueError("Pesanan belum dapat direkonsiliasi atau sudah selesai.")
            db.execute("UPDATE orders SET status=?,note=?,updated_at=? WHERE id=?", (status, "Dikonfirmasi pengguna: " + note, now(), order_id))
            # Cancelled orders stay paused until the user deliberately re-arms the target.
            product_status = "completed" if status == "paid_manual" else "paused"
            db.execute("UPDATE products SET active=0,status=?,note=? WHERE id=?", (product_status, "Status diperbarui pengguna. Aktifkan lagi hanya bila masih diperlukan.", order["product_id"]))
        self.event("Status pesanan diperbarui pengguna: " + status)

    def recover(self):
        with self.db() as db:
            affected = list(db.execute("SELECT product_id FROM orders WHERE status IN ('preparing','submitting')"))
            db.execute("UPDATE orders SET status='unknown',note=?,updated_at=? WHERE status IN ('preparing','submitting')",
                       ("Aplikasi berhenti saat checkout. Periksa riwayat merchant sebelum melakukan tindakan lain.", now()))
            for row in affected:
                db.execute("UPDATE products SET active=0,status='needs_action',note=? WHERE id=?", ("Checkout terputus; periksa pesanan sebelum mengulang.", row["product_id"]))
        return len(affected)

    def order_by_ref(self, reference: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM orders WHERE order_ref=? AND mode=?", (reference, self.mode)).fetchone()
            return dict(row) if row else None

