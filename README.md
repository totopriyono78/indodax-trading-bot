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

## 2. Siapkan API key TAPI v2

Key lama yang terlihat di akun Anda kemungkinan key TAPI versi lama, yang **tidak bisa** dipakai untuk v2.

1. Login ke Indodax → buka **https://indodax.com/trade_api** → buat key baru **TAPI v2**.
2. Izin: centang **View** dan **Trade** saja. **Jangan centang Withdraw.**
3. **IP whitelist**: isi dengan IP publik VPS Anda (cek di VPS: `curl -4 ifconfig.me`).
4. Simpan API key & secret key. Secret hanya ditampilkan sekali.

Jangan pernah membagikan secret key ke siapa pun, termasuk menempelkannya di chat.

---

## 3. Instalasi di VPS (Ubuntu/Debian)

```bash
# salin folder bot ke VPS, misalnya:
scp indodax-bot.zip user@IP_VPS:~
ssh user@IP_VPS
unzip indodax-bot.zip && cd indodax-bot

bash deploy/install.sh        # venv, dependensi, config, layanan systemd
nano .env                     # isi INDODAX_API_KEY dan INDODAX_SECRET_KEY
nano config.yaml              # atur pair & risiko (biarkan mode: paper)
```

Butuh Python 3.9+ (Ubuntu 22.04/24.04 sudah cukup).

---

## 4. Urutan pemakaian yang disarankan

```bash
# a) Cek koneksi, pair, API key, izin, saldo
.venv/bin/python -m bot check

# b) Backtest strategi dengan data historis Indodax
.venv/bin/python -m bot backtest --days 60
.venv/bin/python -m bot backtest --days 90 --pairs btcidr,dogeidr --timeframe 60

# c) Bandingkan kombinasi TP/SL/trailing (dipilih di 70% data awal, diuji di 30% akhir)
.venv/bin/python -m bot backtest --days 120 --sweep

# d) Jalankan simulasi (mode: paper) 24 jam lewat systemd — minimal 1–2 minggu
sudo systemctl enable --now indodax-bot
journalctl -u indodax-bot -f           # lihat log langsung
.venv/bin/python -m bot status         # posisi, PnL, ekuitas simulasi

# e) Setelah yakin: ubah config.yaml -> mode: live, idr_per_trade kecil, lalu
sudo systemctl restart indodax-bot
```

State simulasi dan live disimpan terpisah (`data/state_paper.json` vs `data/state_live.json`).

### Perintah sehari-hari

| Perintah | Fungsi |
|---|---|
| `python -m bot web` | Jalankan dashboard web secara manual (lihat bagian 5) |
| `python -m bot status` | Posisi terbuka, PnL hari ini & total, win rate |
| `python -m bot pause` | Stop membuka posisi baru (posisi lama tetap dijaga TP/SL) |
| `python -m bot resume` | Boleh membuka posisi baru lagi |
| `python -m bot sellall` | Minta bot yang sedang jalan menjual semua posisinya dan pause pembelian baru |
| `sudo systemctl stop indodax-bot` | Matikan bot (posisi **tidak** dijual otomatis) |
| `data/trades_live.csv` | Jurnal semua transaksi (bisa dibuka di Excel) |
| `data/bot.log` | Log lengkap, termasuk alasan setiap sinyal |

---

## 5. Dashboard web (monitoring dari browser)

Dashboard berjalan sebagai proses terpisah di VPS dan hanya **membaca** catatan bot, jadi tidak
mengganggu trading. Isinya:

- status bot (berjalan / terlambat / mati) dan mode (simulasi / live);
- PnL hari ini, bulan ini, total, belum terealisasi, win rate, modal terpakai (dan ekuitas simulasi);
- grafik PnL kumulatif dan PnL harian 30 hari;
- posisi terbuka lengkap dengan batas stop loss / trailing dan take profit saat ini;
- sinyal terakhir tiap pair beserta alasannya (mis. "harga di bawah EMA tren");
- transaksi terakhir, pengaturan aktif, dan log bot;
- tombol **Pause pembelian**, **Lanjutkan**, dan **Jual semua posisi** (bisa dimatikan di config).

Halaman diperbarui otomatis tiap 15 detik dan nyaman dibuka di HP.

```bash
sudo systemctl enable --now indodax-bot-web
```

### Cara membukanya

**Pilihan A — SSH tunnel (paling aman, default).** Dashboard hanya mendengarkan di `127.0.0.1:8080` VPS.
Dari laptop Anda:

```bash
ssh -L 8080:localhost:8080 user@IP_VPS
```

Biarkan terminal itu terbuka, lalu buka **http://localhost:8080** di browser laptop.

**Pilihan B — dibuka langsung dari internet / HP.**

1. Di `.env` isi `DASHBOARD_PASSWORD` (minimal 10 karakter, acak). Dashboard menolak berjalan
   di alamat publik tanpa password.
2. Di `config.yaml` ubah `dashboard.host: 0.0.0.0`.
3. Buka port di firewall VPS: `sudo ufw allow 8080/tcp`.
4. `sudo systemctl restart indodax-bot-web`, lalu buka `http://IP_VPS:8080` (login `admin` + password).

Catatan: pilihan B memakai HTTP biasa, sehingga password bisa disadap di jaringan publik (Wi-Fi kafe dll.).
Untuk pemakaian rutin, pasang HTTPS lewat reverse proxy (mis. Caddy dengan domain Anda) atau pakai
pilihan A. Jika ragu, set `dashboard.allow_control: false` agar dashboard hanya bisa melihat.

## 6. Notifikasi Telegram (opsional, disarankan)

1. Di Telegram, chat **@BotFather** → `/newbot` → salin token.
2. Kirim pesan apa saja ke bot baru Anda, lalu buka
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → salin angka `chat.id`.
3. Isi `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` di `.env`, set `telegram.enabled: true` di `config.yaml`.
4. `python -m bot check` akan mengirim pesan tes.

Anda akan menerima pesan saat bot beli/jual, error, batas rugi harian tercapai, dan ringkasan harian.

---

## 7. Hal yang perlu dipahami

- **Biaya menentukan segalanya.** Setiap beli + jual memakan kira-kira 0,8–1% (fee taker, PPh 0,21% saat
  jual, CFX, slippage). Take profit di bawah ~2% hampir pasti tidak menguntungkan. Cocokkan nilai `fees`
  di config dengan tabel *All-in Fees* di menu Profil akun Anda.
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
  web.py         dashboard web (+ web_ui.html)
tests/           uji otomatis dengan data & API palsu (python -m pytest)
deploy/          install.sh & layanan systemd
```

Ide pengembangan berikutnya: order limit (fee maker lebih murah), strategi tambahan (breakout, mean
reversion), ukuran posisi berbasis volatilitas (ATR).
