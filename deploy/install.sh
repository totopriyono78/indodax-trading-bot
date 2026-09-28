#!/usr/bin/env bash
# Instalasi bot di VPS (Ubuntu/Debian). Jalankan dari folder bot:  bash deploy/install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(whoami)"
cd "$DIR"

echo "==> Memasang Python venv & dependensi"
if ! python3 -m venv --help >/dev/null 2>&1; then
  sudo apt-get update && sudo apt-get install -y python3-venv
fi
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

echo "==> Menyiapkan file konfigurasi"
[ -f config.yaml ] || cp config.example.yaml config.yaml
if [ ! -f .env ]; then cp .env.example .env; fi
chmod 600 .env
mkdir -p data

if grep -q "^DATABASE_URL=postgres" .env 2>/dev/null; then
  .venv/bin/pip install -r requirements-postgres.txt -q
fi

echo "==> Database, kunci enkripsi & akun admin dashboard"
.venv/bin/python -m bot init

echo "==> Sinkronisasi jam (penting untuk tanda tangan API)"
sudo timedatectl set-ntp true || true

echo "==> Memasang layanan systemd"
sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER_NAME|g" deploy/indodax-bot.service \
  | sudo tee /etc/systemd/system/indodax-bot.service >/dev/null
sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$USER_NAME|g" deploy/indodax-bot-web.service \
  | sudo tee /etc/systemd/system/indodax-bot-web.service >/dev/null
sudo systemctl daemon-reload

echo
echo "Selesai. Langkah berikutnya:"
echo "  1. sudo systemctl enable --now indodax-bot-web   (dashboard web)"
echo "  2. Buka dashboard (lihat README bagian Dashboard), login, lalu di menu Pengaturan:"
echo "     - isi API key Indodax (TAPI v2)"
echo "     - atur pair & stop loss per pair (biarkan mode SIMULASI dulu)"
echo "  3. .venv/bin/python -m bot check"
echo "  4. .venv/bin/python -m bot backtest --days 60"
echo "  5. sudo systemctl enable --now indodax-bot       (bot jalan 24 jam)"
echo "  6. journalctl -u indodax-bot -f                  (lihat log)"
echo
echo "PENTING: simpan cadangan BOT_MASTER_KEY dari file .env di tempat aman."
echo
echo "IP publik VPS ini (untuk IP whitelist API key): $(curl -4 -s --max-time 5 ifconfig.me || echo 'cek manual: curl -4 ifconfig.me')"
