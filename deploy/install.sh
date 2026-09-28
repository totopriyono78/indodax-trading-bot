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
echo "  1. nano .env           -> isi API key TAPI v2 (dan Telegram, opsional)"
echo "  2. nano config.yaml    -> sesuaikan pair & risiko (biarkan mode: paper dulu)"
echo "  3. .venv/bin/python -m bot check"
echo "  4. .venv/bin/python -m bot backtest --days 60"
echo "  5. sudo systemctl enable --now indodax-bot   (jalankan 24 jam)"
echo "  6. journalctl -u indodax-bot -f              (lihat log)"
echo "  7. sudo systemctl enable --now indodax-bot-web  (dashboard web, lihat README bagian Dashboard)"
echo
echo "IP publik VPS ini (untuk IP whitelist API key): $(curl -4 -s --max-time 5 ifconfig.me || echo 'cek manual: curl -4 ifconfig.me')"
