# Pemetaan tambahan untuk checkout iBox

**Tidak perlu mengedit konfigurasi untuk membaca produk, memilih warna/kapasitas, atau membuka iBox/login.** Adapter iBox bawaan menangani halaman produk. Dokumen ini ditujukan untuk pengembangan checkout otomatis yang memerlukan keranjang, alamat/pengiriman, ringkasan pembayaran dan hasil pesanan nyata.

Checkout otomatis sampai nomor pesanan/pembayaran belum diverifikasi pada akun iBox. Nilai yang belum diketahui tetap kosong. Kemampuan membaca stok tetap bekerja terpisah dari kelengkapan pemetaan checkout.

## Konfigurasi opsional

Salin template hanya bila ingin mengubah pengaturan browser atau mengembangkan checkout:

```bat
copy config.example.json config.json
.venv\Scripts\python.exe app.py --config config.json
```

Pengaturan dimuat saat startup; tutup dan mulai ulang aplikasi setelah mengubah file. `checkout.enabled` bawaan adalah `false`. Mengubahnya menjadi `true` saja belum membuat pemetaan lengkap atau benar.

Biarkan `browser.headless` bernilai `false` agar login dan langkah manual terlihat. `timeout_ms` mengatur waktu tunggu elemen, sedangkan `payment_timeout_ms` mengatur konfirmasi setelah submit. Interval monitor diatur melalui `--interval`.

`merchant.allowed_hosts` memuat hostname persis iBox. `allowed_payment_hosts` hanya diisi dengan host pembayaran yang benar-benar terlihat pada alur merchant; bukan wildcard. Jangan memperluasnya untuk melewati penolakan navigasi yang belum diperiksa. `login_url` bawaan membuka beranda iBox agar pengguna dapat login sendiri. `cart_url` perlu dibuktikan dari alur keranjang asli sebelum checkout otomatis dipakai.

## Bagian halaman produk yang sudah diamati

Selector berikut berasal dari pemeriksaan DOM publik produk iBox. Situs dapat mengubahnya sewaktu-waktu; adapter harus menghentikan pembacaan bila elemen tidak cocok.

| Elemen | Selector/keadaan |
| --- | --- |
| Nama | `h1#mcis-pdp-product-title` |
| Harga satuan | `[data-testid="qa-pdp-price"]` |
| Input jumlah | `input[data-testid="qa-pdp-inputqty"]` |
| Tombol keranjang | `button#mcis-add-to-cart`; atribut `disabled` menunjukkan tombol tidak aktif pada halaman yang diperiksa. |
| Warna desktop | `input[name="Warna"][data-testid="qa-pdp-configurable-1"]` |
| Warna terpilih | `data-selected="true"` pada input warna. |
| Warna tidak tersedia | `aria-disabled="true"` pada input; tidak boleh dipaksa dipilih. |
| Kapasitas desktop | `input[name="Kapasitas"][data-testid="qa-pdp-configurable-3"]` |
| Kapasitas terpilih | `data-selected="true"` pada wadah induknya. |

Halaman juga memiliki kontrol mobile. Jangan memakai selector yang mencocokkan kontrol desktop dan mobile sekaligus. Pemilihan varian dapat mengganti URL/SKU; temukan ulang kontrol dan verifikasi warna serta kapasitas setelah navigasi.

Harga produk dibaca dari elemen harga utama, bukan harga cicilan atau harga tampilan varian lain. Kondisi tombol dan varian tetap perlu diperiksa. Tidak tersedianya pilihan yang diminta tidak menjadi izin untuk beralih otomatis ke warna atau kapasitas lain.

## Pemetaan keranjang dan checkout

Gunakan Developer Tools atau [Playwright codegen](https://playwright.dev/python/docs/codegen-intro) pada browser lokal sendiri untuk memeriksa locator. Jangan memasukkan rekaman yang berisi password/OTP/token ke proyek atau membagikan profil browser.

Tabel ini menjelaskan kunci adapter umum. Selector harus diperiksa pada halaman dan tahap yang tepat, bukan diisi dengan nama elemen perkiraan.

| Kunci `merchant.selectors` | Yang harus dapat diverifikasi |
| --- | --- |
| `login_required` | Penanda bahwa login perlu diselesaikan. |
| `challenge` | Penanda verifikasi/CAPTCHA yang memerlukan pengguna. |
| `cart_empty` | Keranjang benar-benar kosong. |
| `cart_items` | Wadah satu baris barang. |
| `cart_item_name` | Nama dalam baris keranjang. |
| `cart_item_variant` | Warna/kapasitas tepat untuk barang itu. |
| `cart_item_quantity` | Jumlah unit dalam baris, berupa angka. |
| `cart_item_unit_price` | Harga satuan efektif dalam baris. |
| `checkout_items` | Wadah satu baris ringkasan checkout. |
| `checkout_item_name` | Nama pada ringkasan checkout. |
| `checkout_item_variant` | Warna/kapasitas yang akan dipesan. |
| `checkout_item_quantity` | Jumlah final. |
| `checkout_item_unit_price` | Harga satuan final. |
| `checkout_total` | Total akhir termasuk pengiriman dan biaya. |
| `submit_order` | Tombol yang benar-benar membuat pesanan. |
| `payment_ready` | Penanda instruksi pembayaran sesudah pesanan tercatat. |
| `order_ref` | Nomor/reference pesanan baru. |

Selector `cart_item_*` relatif terhadap satu `cart_items`; `checkout_item_*` relatif terhadap satu `checkout_items`. Adapter umum memproses satu produk/varian per pesanan. Jika halaman memakai bundel, beberapa baris, iframe atau teks varian yang berbeda, sesuaikan kodenya dan uji hasilnya sebelum dipakai.

Keranjang diperiksa kosong sebelum penambahan barang. `clear_cart_actions: []` mempertahankan isi keranjang yang sudah ada dan meminta penanganan manual. Jangan menambah aksi pengosongan kecuali penghapusan isi keranjang memang diinginkan.

Nama, warna/kapasitas, jumlah, harga satuan, dan total akhir harus cocok. Pembacaan teks yang ambigu harus menghentikan proses. Sekadar mendeteksi tombol checkout atau halaman pembayaran tidak cukup untuk menyatakan pesanan telah tercatat.

## Aksi persiapan

`checkout_actions` hanya berisi langkah sebelum tombol pembuat pesanan: membuka checkout, memilih alamat tersimpan, kurir, dan metode pembayaran. Aksi yang didukung adapter umum:

| `action` | Properti |
| --- | --- |
| `click` | `selector` |
| `fill` | `selector`, `value` |
| `select` | `selector`, `value` untuk nilai option |
| `goto` | `url` pada host merchant yang diizinkan |

`wait_for` dapat menunggu selector tahap berikutnya. Nilai `{quantity}` dan `{variant}` dapat digunakan oleh adapter umum sebagai pengganti data wishlist. Varian iBox dalam aplikasi disimpan sebagai `color` dan `capacity`; pembacaan/pemilihan produk memakai adapter khusus, sehingga satu teks gabungan tidak boleh dianggap membuktikan semua kontrol sudah tepat.

**Tombol pembuat pesanan hanya ditempatkan pada `selectors.submit_order`.** Jangan menaruh tombol itu, alias selectornya, atau tombol yang langsung melakukan pembayaran di aksi persiapan. Engine menyimpan checkpoint sebelum satu kali submit. Hasil yang tidak pasti memerlukan rekonsiliasi dan tidak diulang otomatis.

Jangan memasukkan password, OTP, PIN, detail kartu atau token browser ke JSON. Langkah verifikasi sensitif dilanjutkan langsung di alur merchant.

## Memeriksa konfigurasi

Pemeriksaan struktur tanpa membuka browser:

```bat
.venv\Scripts\python.exe -c "import json; from browser_adapter import validate_config; c=json.load(open('config.json',encoding='utf-8')); print('\n'.join(validate_config(c)) or 'Struktur konfigurasi lengkap; kecocokan halaman masih perlu diperiksa.')"
```

Validasi struktur tidak membuktikan kebenaran pemetaan. Periksa bahwa setiap selector menunjuk tepat satu elemen, jumlah dan varian tetap benar setelah navigasi, total mencakup biaya, serta nomor pesanan hanya muncul setelah pesanan baru tercatat.

Uji parser/engine/API secara lokal:

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Suite fixture memeriksa perilaku kode tanpa membuat pesanan merchant. Itu bukan bukti bahwa alur checkout iBox yang memakai akun, alamat, dan metode pembayaran tertentu sudah terintegrasi.

## Hasil checkout dan pemulihan

`awaiting_payment` memerlukan nomor pesanan baru dan penanda pembayaran yang sesuai. Tab pembayaran lama tidak boleh digunakan sebagai bukti. Status tersebut belum berarti lunas; konfirmasi pengguna dicatat sebagai `paid_manual`.

Jika browser atau server terputus sesudah persiapan/pengiriman pesanan, periksa riwayat iBox. Pembaruan status di dashboard hanya rekonsiliasi lokal dan tidak membatalkan transaksi di merchant. Jangan melepas anggaran atau mencoba ulang sebelum hasil tindakan sebelumnya diketahui.
