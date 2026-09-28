# Bot Trading Indodax

Bot Python yang memantau harga di Indodax, membeli saat tren naik terkonfirmasi, lalu menjual otomatis
saat target untung tercapai (*take profit*), saat harga berbalik dari puncak (*trailing stop*), atau saat
harga turun melewati batas rugi (*stop loss*). Memakai **Trade API v2** resmi Indodax.

> ⚠️ Bot ini alat bantu disiplin, bukan mesin pencetak uang. Strategi apa pun bisa rugi.
> Mulailah dari **mode paper (simulasi)**, lalu **live dengan dana kecil**.

---

## 1. Cara kerja singkat

Setiap `poll_seconds` (default 20 detik) bot:

1. Mengambil harga terkini semua pair (1 request).
2. Untuk tiap **posisi terbuka**: cek stop loss, trailing stop, take profit, dan batas lama tahan.
   Jika kena, langsung jual dengan order market.
3. Setiap ada **candle baru yang selesai** (default 15 menit), menghitung indikator dan mencari sinyal:

| Syarat beli (semua harus terpenuhi) | Default |
|---|---|
| Harga penutupan di atas EMA tren | EMA-100 |
| EMA cepat di atas EMA lambat, dan baru saja memotong ke atas | EMA-9 / EMA-21, dalam 3 candle |
| RSI dalam rentang momentum sehat | 50–70 |
| Lolos filter risiko | spread ≤ 0,6%, volume 24 jam ≥ Rp1 M, slot & modal tersedia |

| Aturan jual | Default |
|---|---|
| Stop loss | −2,5% dari harga beli |
| Take profit | +4% |
| Trailing stop | jual jika turun 1,5% dari puncak, aktif setelah untung ≥ 2% |
| Tren berbalik | EMA-9 turun ke bawah EMA-21 |
| Batas lama tahan | 72 jam |

**Pengaman:** batas modal per transaksi, jumlah posisi maksimum, total eksposur maksimum, batas rugi harian
(bot berhenti beli sampai besok), jeda setelah rugi per pair, dan saldo cadangan IDR.

Bot **hanya menjual koin yang dibelinya sendiri**. Koin yang Anda beli manual tidak disentuh. Jika Anda
menjual manual koin yang sedang dipegang bot, bot akan menyesuaikan catatannya saat restart.

---

## 2. Instalasi di VPS (Ubuntu/Debian)

```bash
git clone https://github.com/totopriyono78/indodax-trading-bot.git
cd indodax-trading-bot
bash deploy/install.sh
```

`install.sh` memasang Python venv & dependensi, lalu menjalankan `python -m bot init` yang:

1. membuat **database** (default SQLite di `data/bot.db`),
2. membuat **kunci enkripsi** `BOT_MASTER_KEY` di `.env` — **simpan cadangannya**; tanpa kunci ini API key
   yang tersimpan tidak bisa dibuka dan harus dimasukkan ulang,
3. meminta Anda membuat **akun admin** untuk login dashboard.

Butuh Python 3.9+ (Ubuntu 22.04/24.04 sudah cukup).

### Memakai PostgreSQL (opsional)

SQLite sudah cukup untuk satu bot. Jika ingin PostgreSQL:

```bash
sudo -u postgres psql -c "CREATE USER botuser WITH PASSWORD 'ganti-password';"
sudo -u postgres psql -c "CREATE DATABASE indodax_bot OWNER botuser;"
echo 'DATABASE_URL=postgresql+psycopg://botuser:ganti-password@localhost:5432/indodax_bot' >> .env
.venv/bin/pip install -r requirements-postgres.txt
.venv/bin/python -m bot init
```

---

## 3. Dashboard web: login & pengaturan

Semua pengaturan trading disimpan di database dan diubah lewat dashboard — tidak perlu mengedit file atau
me-restart bot. Bot membaca perubahan otomatis dalam ±20 detik.

```bash
sudo systemctl enable --now indodax-bot-web
```

### Cara membuka dashboard

**Pilihan A — SSH tunnel (paling aman, default).** Dashboard hanya mendengarkan di `127.0.0.1:8080` VPS.
Dari laptop: `ssh -L 8080:localhost:8080 user@IP_VPS`, biarkan terbuka, lalu buka **http://localhost:8080**.

**Pilihan B — dibuka dari internet / HP.** Ubah `dashboard.host: 0.0.0.0` di `config.yaml`, buka port
(`sudo ufw allow 8080/tcp`), restart `indodax-bot-web`. Karena halaman ini menyimpan API key, **pasang HTTPS**
(mis. Caddy + domain Anda) sebelum dipakai rutin dari jaringan publik.

### Login

- Akun admin dibuat saat `python -m bot init`. Jika belum ada akun, halaman pertama meminta **kode setup**
  yang tercetak di log (`journalctl -u indodax-bot-web -n 30`).
- Tambah akun / reset password dari terminal: `python -m bot user add NAMA`, `python -m bot user passwd NAMA`.
- 5 kali salah password → dikunci 10 menit. Sesi berlaku 12 jam.

### Menu Pengaturan

| Tab | Isi |
|---|---|
| **Pair & stop loss** | Tambah/hapus/nonaktifkan pair (daftar diambil langsung dari Indodax), dan atur **stop loss, take profit, trailing, dan modal per transaksi untuk tiap pair**. Kolom kosong = pakai pengaturan umum. |
| Stop loss & target umum | Nilai default untuk semua pair |
| Modal & risiko | Modal per transaksi, posisi maksimum, eksposur maksimum, batas rugi harian, filter spread/volume |
| Strategi | Parameter EMA/RSI |
| Mode & sistem | **SIMULASI / LIVE** (butuh password + ketik `LIVE`; bot restart otomatis), timeframe, biaya, modal simulasi, tombol kontrol dashboard |
| **API key Indodax** | Masukkan/ganti API key & secret: diuji ke Indodax dulu, disimpan **terenkripsi**, tidak pernah ditampilkan ulang, langsung dipakai bot tanpa restart. Butuh konfirmasi password. |
| Telegram | Aktif/nonaktif notifikasi, token & chat ID (dikirim pesan tes saat disimpan) |
| Akun & riwayat | Ganti password, riwayat login & semua perubahan pengaturan (siapa, kapan, apa) |

Pengaman tambahan: tidak bisa pindah dari LIVE ke SIMULASI selama masih ada posisi live terbuka (jual dulu),
dan jika pengaturan di database rusak, bot tetap jalan memakai pengaturan valid terakhir dengan pembelian
di-pause agar stop loss posisi terbuka tetap bekerja.

### Siapkan API key TAPI v2

1. Login Indodax → **https://indodax.com/trade_api** → buat key **TAPI v2** (key TAPI lama tidak bisa dipakai).
2. Izin: **View + Trade saja. Jangan centang Withdraw.**
3. IP whitelist: IP publik VPS (`curl -4 ifconfig.me`).
4. Tempel API key & secret di dashboard → Pengaturan → **API key Indodax**.

### Halaman Dashboard

Status bot (berjalan/terlambat/mati), PnL hari ini/bulan ini/total, grafik PnL, posisi terbuka dengan batas
stop loss saat ini, sinyal & SL/TP tiap pair, transaksi terakhir, log, serta tombol **Pause**, **Lanjutkan**,
dan **Jual semua posisi**. Diperbarui otomatis tiap 15 detik.

---

## 4. Urutan pemakaian yang disarankan

```bash
.venv/bin/python -m bot check                      # koneksi, pair, API key, izin, saldo
.venv/bin/python -m bot backtest --days 60         # uji strategi (memakai SL per pair dari database)
.venv/bin/python -m bot backtest --days 120 --sweep --pairs pepeidr   # cari SL/TP yang cocok per koin
sudo systemctl enable --now indodax-bot            # jalankan (mode SIMULASI dulu, 1–2 minggu)
journalctl -u indodax-bot -f                       # log langsung
```

Setelah yakin, pindah ke **LIVE** di dashboard (Pengaturan → Mode & sistem) dengan modal kecil.
State simulasi dan live disimpan terpisah (`data/state_paper.json` vs `data/state_live.json`).

### Perintah terminal

| Perintah | Fungsi |
|---|---|
| `python -m bot init` | Siapkan database, kunci enkripsi, akun admin (aman dijalankan ulang) |
| `python -m bot user add/passwd/list` | Kelola akun login dashboard |
| `python -m bot status` | Posisi terbuka, PnL hari ini & total, win rate |
| `python -m bot pause` / `resume` | Stop / lanjutkan membuka posisi baru |
| `python -m bot sellall` | Jual semua posisi bot dan pause pembelian baru |
| `sudo systemctl stop indodax-bot` | Matikan bot (posisi **tidak** dijual otomatis) |
| `data/trades_live.csv` | Jurnal transaksi (bisa dibuka di Excel) |

### Cadangan (backup)

Yang perlu dicadangkan: `.env` (terutama `BOT_MASTER_KEY`), `data/bot.db` (atau database PostgreSQL),
dan folder `data/` (posisi & jurnal transaksi).

---

## 5. Tempat data disimpan

| Data | Lokasi |
|---|---|
| Pengaturan, pair & stop loss per pair, akun login, API key (terenkripsi), riwayat perubahan | Database (`data/bot.db` atau PostgreSQL) |
| Posisi terbuka, PnL harian | `data/state_<mode>.json` |
| Jurnal transaksi | `data/trades_<mode>.csv` |
| Log | `data/bot.log` |

---

## 6. Notifikasi Telegram (opsional, disarankan)

1. Di Telegram, chat **@BotFather** → `/newbot` → salin token.
2. Kirim pesan apa saja ke bot baru Anda, lalu buka
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → salin angka `chat.id`.
3. Dashboard → Pengaturan → **Telegram**: isi token & chat ID, centang aktif.

Anda akan menerima pesan saat bot beli/jual, error, batas rugi harian tercapai, pengaturan berubah, dan ringkasan harian.

---

## 7. Hal yang perlu dipahami

- **Biaya menentukan segalanya.** Setiap beli + jual memakan kira-kira 0,8–1% (fee taker, PPh 0,21% saat
  jual, CFX, slippage). Take profit di bawah ~2% hampir pasti tidak menguntungkan. Cocokkan biaya di
  dashboard (Pengaturan → Mode & sistem) dengan tabel *All-in Fees* di menu Profil akun Anda.
- **Stop loss dijalankan oleh bot**, bukan oleh Indodax (API tidak mendukung stop-limit). Jika VPS atau bot
  mati, stop loss ikut tidak berjalan. systemd otomatis menyalakan ulang bot; notifikasi Telegram membantu
  Anda tahu jika ada masalah.
- **Koin kecil (PEPE dll.) lebih berisiko**: harga melompat cepat, order book tipis, sehingga eksekusi market
  bisa jauh dari harga terakhir. Filter spread & volume membantu, tapi tidak menghilangkan risikonya.
- **Target 10%/bulan tidak bisa dijamin.** Tidak ada strategi yang konsisten memberi hasil tetap. Pakai
  backtest dan periode paper trading untuk melihat angka yang realistis bagi strategi dan pair Anda, lalu
  pertahankan batas rugi harian.
- **Mematuhi SKU Indodax**: bot hanya memakai order market biasa, tanpa order palsu, tanpa wash trading,
  dan tanpa mencoba memanipulasi harga.

---

## 8. Struktur kode

```
bot/
  client.py      klien Public API & TAPI v2 (tanda tangan HMAC-SHA256, sinkron jam, rate limit)
  market.py      info pair, harga terkini, candle OHLC
  indicators.py  EMA, RSI
  strategy.py    sinyal beli & aturan jual (dipakai bot DAN backtest)
  broker.py      eksekusi order: PaperBroker (simulasi) & LiveBroker (sungguhan)
  engine.py      loop utama, manajemen risiko, notifikasi
  backtest.py    backtest & sweep parameter
  state.py       penyimpanan posisi, PnL, jurnal CSV
  notifier.py    Telegram
  db.py          database: pengaturan, kredensial terenkripsi, user, sesi, audit
  settings.py    pengaturan dari database + pengaturan per pair
  web.py         dashboard web: login, monitoring, pengaturan (+ *.html, static/)
tests/           uji otomatis dengan data & API palsu (python -m pytest)
deploy/          install.sh & layanan systemd
```

Ide pengembangan berikutnya: order limit (fee maker lebih murah), strategi tambahan (breakout, mean
reversion), ukuran posisi berbasis volatilitas (ATR).
