# StockPilot iBox — Python

Dashboard lokal untuk membaca **harga dan stok asli di `https://ibox.co.id`**, menyimpan daftar incaran, memilih warna/kapasitas, dan membuka produk yang memenuhi batas belanja di browser iBox. Versi ini mulai langsung dengan koneksi iBox dan daftar kosong.

**Pembacaan produk dan browser iBox sudah memiliki pemetaan bawaan. Checkout otomatis sampai nomor pesanan/pembayaran belum diverifikasi pada akun iBox; paket tidak mengklaim pesanan dibuat hanya karena halaman produk terbuka.** Login dan pembacaan stok dapat dipakai tanpa mengisi selector checkout.

## Mulai di Windows

1. Tutup StockPilot lama: tekan **Ctrl+C** di terminalnya.
2. Ekstrak paket baru ke folder baru, misalnya `D:\StockPilot_iBox`. Jangan jalankan ZIP tanpa mengekstraknya.
3. Buka folder yang memuat `app.py`, lalu klik dua kali **`install_windows.bat`**. Tunggu pemasangan dependency dan Chromium selesai. Internet diperlukan untuk langkah ini.
4. Klik dua kali **`start_windows.bat`**. Dashboard akan terbuka di **http://127.0.0.1:8777**. Biarkan terminal dan browser iBox tetap terbuka saat dipakai.
5. Di dashboard, klik **Buka iBox / login**. Masuk melalui situs iBox bila diperlukan. Password dan kode verifikasi diisi langsung di browser iBox.
6. Tambahkan incaran, tempel tautan produk, lalu klik **Baca produk iBox**. Hasil pembacaan mengisi nama dan pilihan varian dari halaman asli.
7. Pilih warna dan kapasitas yang diinginkan, isi jumlah serta batas harga, lalu simpan. Aktifkan produk dan pilih **Cek sekali** atau **Mulai pantau**.

Untuk instalasi yang sebelumnya menampilkan `py is not recognized`, tidak perlu memakai perintah `py`. Installer mencari Python yang sudah ada, termasuk lokasi instalasi Python 3.13 per pengguna, lalu memakai Python di `.venv`.

Jika Python belum ditemukan, installer menampilkan perintah instalasi yang dapat dijalankan di Command Prompt. Python **3.10 atau lebih baru** dibutuhkan. Setelah memasang Python, tutup terminal lama sebelum menjalankan installer lagi.

### Perintah manual di Command Prompt

Gunakan ini bila ingin melihat setiap langkah sendiri, dari folder yang memuat `app.py`:

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe app.py
```

Jika `python` belum dikenali tetapi Python 3.13 terpasang pada lokasi standarnya, perintah pertama dapat diganti dengan:

```bat
"%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -m venv .venv
```

Jangan salin `config.json` lama yang kosong. Pemakaian biasa tidak membutuhkan file konfigurasi: konfigurasi iBox bawaan sudah disertakan. Konfigurasi tambahan dijelaskan dalam [SELECTOR_GUIDE.md](SELECTOR_GUIDE.md).

## Yang dilakukan setiap pilihan tindakan

| Pilihan | Perilaku ketika stok dan harga cocok |
| --- | --- |
| **Pantau saja** (`notify`) | Membaca harga/stok asli dan menampilkan hasilnya. Tidak menambahkan barang ke keranjang. Ini pilihan awal. |
| **Siapkan di browser** (`assist`) | Membuka produk asli dengan warna, kapasitas, dan jumlah yang diverifikasi; menahan anggaran untuk sesi tersebut. Pengguna melanjutkan keranjang, alamat, pengiriman, dan pembayaran di browser iBox. |
| **Checkout otomatis** (`auto`) | Hanya dapat digunakan setelah pemetaan keranjang/checkout lengkap, diaktifkan di konfigurasi, dan diperiksa pada alur merchant. Bukan kemampuan yang dinyatakan sudah teruji untuk iBox pada paket ini. |

**Siapkan di browser** belum membuat pesanan iBox. Nomor sesi lokal bukan nomor pesanan toko. Batas anggaran di dashboard mengatur persiapan otomatis; setelah mengambil alih browser, periksa sendiri total akhir sebelum membuat pesanan atau membayar.

Tombol **Cek sekali** menjalankan pilihan tindakan produk aktif. Jadi, untuk pemeriksaan tanpa persiapan checkout, pilih **Pantau saja**. Monitor memeriksa semua produk aktif lintas profil, menurut prioritas. Pilihan profil di dashboard memfilter tampilan.

## Mengisi produk dan Rupiah dengan benar

| Isian | Cara mengisinya |
| --- | --- |
| URL produk | Salin halaman produk pada `https://ibox.co.id/product/...`. Link dari domain lain ditolak. |
| Nama produk | Gunakan hasil **Baca produk iBox**, misalnya `iPhone Duo`; nama diverifikasi kembali saat pemeriksaan. |
| Warna | Pilih atau tulis nilai warna sesuai halaman iBox. Warna yang tidak tersedia tidak diganti otomatis. |
| Kapasitas | Pilih nilai kapasitas sesuai halaman, misalnya `2 TB`. Kapasitas diperlakukan terpisah dari warna. |
| Jumlah target | Total unit yang diinginkan untuk entri tersebut. Tetap mengikuti batas pembelian merchant. |
| Harga maksimum per unit | Rupiah lengkap. `74000000` berarti **Rp74.000.000**; `74` berarti **Rp74**. |
| Total maksimum | Batas untuk jumlah barang beserta ruang ongkir/biaya. Minimal jumlah target × harga maksimum per unit. |
| Anggaran profil | Batas gabungan sesi checkout dan pesanan yang masih tercatat. Profil baru mulai dengan anggaran **Rp0**, sehingga perlu diisi sebelum menyiapkan pembelian. |
| Prioritas | Angka lebih kecil diperiksa lebih dahulu. |

Harga hasil pembacaan **tidak otomatis menaikkan batas harga atau anggaran**. Mengetik nominal tidak menjamin barang tersedia dengan nominal tersebut.

Halaman iBox dapat mengubah URL setelah varian dipilih. Tool memeriksa ulang pilihan warna dan kapasitas. Bila varian yang diminta tidak tersedia, hasilnya tidak memakai harga varian lain sebagai harga target.

Stok yang terlihat merupakan hasil pemeriksaan pada waktu yang tercantum. Harga dan stok dapat berubah sebelum pesanan diterima iBox. Lokasi, pengiriman, login atau pilihan toko dapat memengaruhi ketersediaan akhir.

## Status koneksi dan browser

Status **dashboard terhubung** berarti browser dashboard dapat menghubungi server Python di komputer ini. Status koneksi iBox menunjukkan hasil pemeriksaan halaman asli terakhir. Keduanya bukan bukti bahwa akun sudah login atau checkout otomatis sudah siap.

StockPilot membuka profil Chromium tersendiri untuk setiap profil lokal. Login yang sudah ada di Edge/Chrome pribadi tidak otomatis dipindahkan. Gunakan tombol pembuka iBox pada profil yang benar, kemudian login langsung pada browser yang terbuka.

Tab pemeriksaan stok dipisahkan dari tab yang dipakai login atau melanjutkan pembelian. Jika situs meminta verifikasi, selesaikan langsung pada browser. Tool tidak mengatasi CAPTCHA, mengubah batas pembelian, atau mengisi PIN/OTP pembayaran.

## Checkout, anggaran dan status lokal

Saat menyiapkan pembelian, aplikasi menahan batas total produk agar tidak menyiapkan target lain dengan anggaran yang sama. Sesi yang belum selesai perlu diperiksa sebelum anggarannya dipakai kembali.

| Status | Arti |
| --- | --- |
| `manual_checkout` | Produk disiapkan pada browser untuk dilanjutkan pengguna. Belum ada bukti pesanan merchant dibuat. |
| `preparing` | Persiapan transaksi sedang berjalan, dengan anggaran ditahan. |
| `submitting` | Niat mengirim pesanan otomatis sudah dicatat sebelum tombol diklik. |
| `awaiting_payment` | Adapter checkout menemukan referensi pesanan baru dan instruksi pembayaran. Status ini belum berarti lunas. |
| `needs_action` | Login, keranjang, varian, harga atau elemen halaman perlu diperiksa. |
| `unknown` | Proses terputus atau hasil pengiriman pesanan belum pasti. Periksa riwayat iBox sebelum mencoba lagi. |
| `paid_manual` | Pengguna mencatat bahwa pembayaran sudah dilakukan; bukan verifikasi otomatis dari iBox. |
| `cancelled_manual` | Pengguna mencatat sesi/pesanan sudah tidak aktif; anggaran lokal dilepas. |

**Perbarui status** hanya memperbarui catatan StockPilot. Menandai dibatalkan tidak mengirim pembatalan ke iBox. Periksa riwayat merchant terlebih dahulu jika pernah melanjutkan checkout. Target tetap dijeda setelah pembatalan; aktifkan ulang bila masih diinginkan.

Hasil submit yang tidak pasti tidak diulang otomatis. Pemulihan setelah aplikasi terputus mempertahankan sesi yang perlu diperiksa. Bila target yang pernah terpenuhi ingin ditambah, naikkan jumlah target pada entri yang sama agar riwayat tetap dipakai untuk menghitung sisa jumlah.

## Data dan pengaturan tambahan

Data versi ini disimpan di **`data/ibox_live_v2`**. Data versi lama tidak diimpor otomatis. Folder ini berisi database lokal dan sesi browser per profil. Cadangkan saat aplikasi sudah ditutup; jangan membagikan folder sesi browser.

Profil lokal memisahkan incaran, anggaran dan sesi browser untuk satu operator. Profil tersebut bukan akun server dengan izin akses terpisah. Aplikasi mendengarkan di `127.0.0.1` untuk pemakaian pada komputer yang sama.

Contoh perintah tambahan:

```bat
.venv\Scripts\python.exe app.py --port 8778
.venv\Scripts\python.exe app.py --interval 90
.venv\Scripts\python.exe app.py --no-browser
.venv\Scripts\python.exe app.py --config config.json
```

Interval pemeriksaan minimal **60 detik**. Kecepatan aktual juga dipengaruhi waktu pemuatan halaman. Stop menghentikan penjadwalan berikutnya; jangan menutup proses yang sedang mengirim pesanan sebelum statusnya diketahui bila memungkinkan.

### Linux/macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python app.py
```

`sh run.sh` dapat digunakan setelah environment dan browser dipasang. Pada Linux tertentu, Chromium memerlukan pustaka sistem tambahan; ikuti pesan instalasi Playwright.

## Pemecahan masalah

| Masalah | Langkah |
| --- | --- |
| Masih melihat tampilan lama | Tutup terminal lama dengan Ctrl+C. Jalankan `start_windows.bat` dari folder paket baru, lalu muat ulang dashboard dengan Ctrl+F5. |
| Port 8777 sudah dipakai | Tutup proses StockPilot lama, atau jalankan `start_windows.bat --port 8778` dan gunakan alamat yang dicetak terminal. |
| Python atau `py` tidak dikenali | Jalankan `install_windows.bat`. Installer tidak bergantung pada launcher `py`. |
| Chromium belum terpasang | Jalankan kembali `install_windows.bat` dengan koneksi internet, atau `.venv\Scripts\python.exe -m playwright install chromium`. |
| Browser iBox meminta login | Login langsung di browser StockPilot pada profil yang dipilih. Jangan masukkan password ke file konfigurasi. |
| Elemen/varian tidak bisa dibaca | Buka halaman asli, periksa apakah varian berubah atau situs meminta verifikasi. Aktifkan lagi pantauan setelah masalah selesai. Jangan mengganti pilihan varian hanya untuk menghilangkan error. |
| Anggaran tidak cukup | Isi anggaran profil sesuai keputusan belanja sendiri, lalu periksa sesi checkout yang masih menahan anggaran. |
| Checkout otomatis belum siap | Stock/login tetap dapat dipakai. Pemetaan checkout tambahan diperlukan; baca `SELECTOR_GUIDE.md`. |

## Struktur dan pengujian

| Berkas | Fungsi |
| --- | --- |
| `app.py` | Server dan API lokal, pengaturan runtime, penguncian instance. |
| `ibox_adapter.py` | Pembacaan produk iBox, pemilihan varian, dan browser untuk dilanjutkan pengguna. |
| `browser_adapter.py` | Adapter checkout umum untuk pemetaan tambahan. |
| `store.py` | Wishlist, profil, anggaran dan catatan checkout SQLite. |
| `engine.py` | Worker serial untuk pembacaan, discovery dan persiapan pembelian. |
| `static/` | Dashboard HTML/CSS/JavaScript tanpa proses build. |
| `tests/` | Pemeriksaan parser, varian, API, anggaran dan pencegahan pemrosesan ganda. |

Jalankan dari folder proyek:

```bat
.venv\Scripts\python.exe run_tests.py
```

Test fixture menggunakan data lokal untuk memeriksa kode, bukan stok merchant atau transaksi asli. Perintah di atas menjalankan tes logika, DOM tiruan, dan API lokal tanpa membuka Chromium. Tes browser pada situs fixture localhost dapat disertakan secara terpisah dengan `run_tests.py --browser-fixtures`. Hasil dan batas verifikasi paket tercatat di **`TEST_RESULTS.txt`**. Windows batch disiapkan untuk pengguna Windows; jangan menganggap keberhasilan uji Python di lingkungan Linux sebagai bukti uji Windows langsung.

## Rujukan

- [Produk iBox yang diperiksa](https://ibox.co.id/product/iphone-duo) — struktur halaman publik untuk nama, harga, warna, kapasitas, jumlah dan tombol keranjang.
- [Instalasi Playwright Python](https://playwright.dev/python/docs/library) — pemasangan paket dan browser.
- [Dokumentasi Python di Windows](https://docs.python.org/3.13/using/windows.html) — penggunaan interpreter dan instalasi Windows.
- [Autentikasi Playwright](https://playwright.dev/python/docs/auth) — penyimpanan state browser dan perlindungan sesi.

StockPilot merupakan alat lokal independen; tidak berafiliasi dengan iBox/Erajaya.

### Menghindari gagal download Chromium (ENOSPC)

Paket ini akan mendeteksi **Microsoft Edge** atau **Google Chrome** yang sudah terpasang di Windows.
Jika tersedia, Playwright memakai browser tersebut dalam mode berjendela dan **installer tidak akan mengunduh Chromium**.
Jika browser resmi tidak ditemukan, pemasang akan mencoba mengunduh Chromium dan memerlukan ruang kosong di disk.
Error `ENOSPC` berarti ruang disk tidak cukup; bukan tanda koneksi internet bermasalah.

Perbaikan manual pada instalasi lama: dalam `config.json`, isi `"browser": { "channel": "msedge" }`
(jangan hapus opsi browser lainnya). Atau biarkan kosong agar versi ini mendeteksi otomatis.
