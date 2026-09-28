"""Dashboard web untuk memantau bot (proses terpisah dari bot trading).

Membaca file di data_dir (state, status, jurnal transaksi, log) — tidak pernah memanggil API privat
Indodax, sehingga dashboard yang bermasalah tidak bisa mengganggu trading.

  python -m bot web                     # default http://127.0.0.1:8080 (akses via SSH tunnel)
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
import os
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .state import WIB, StateStore, today_wib

log = logging.getLogger("bot.web")
UI_FILE = Path(__file__).with_name("web_ui.html")


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def build_summary(cfg: dict) -> dict:
    store = StateStore(cfg["data_dir"], cfg["mode"])
    st = store.load()
    status = store.read_status() or {}
    now = time.time()
    flags = Path(cfg["data_dir"])
    pairs_status = status.get("pairs", {})
    levels = status.get("levels", {})
    poll = cfg["poll_seconds"]

    age = now - status["ts"] if status.get("ts") else None
    if age is None:
        alive, alive_label = "unknown", "Belum ada data — bot belum pernah berjalan"
    elif age <= max(90, poll * 4):
        alive, alive_label = "good", f"Berjalan · update {age:.0f} detik lalu"
    elif age <= 600:
        alive, alive_label = "warning", f"Terlambat · update terakhir {age / 60:.0f} menit lalu"
    else:
        alive, alive_label = "critical", f"Tidak aktif · update terakhir {age / 60:.0f} menit lalu"

    positions = []
    unreal = 0.0
    for pair, p in st.positions.items():
        q = pairs_status.get(pair, {})
        bid = _f(q.get("bid")) or _f(q.get("last"))
        value = p.qty * bid if bid else None
        pnl = (value - p.cost_idr) if value is not None else None
        if pnl is not None:
            unreal += pnl
        lv = levels.get(pair, {})
        positions.append({
            "pair": pair, "qty": p.qty, "entry": p.entry_price, "cost": p.cost_idr, "price": bid or None,
            "value": value, "pnl": pnl, "pnl_pct": (pnl / p.cost_idr * 100) if pnl is not None and p.cost_idr else None,
            "opened_at": p.opened_at, "highest": p.highest, "stop": lv.get("stop"),
            "stop_reason": lv.get("stop_reason"), "take_profit": lv.get("take_profit"),
            "dust": p.dust, "reason": p.reason,
        })

    trades = store.read_trades()
    sells = [t for t in trades if t.get("sisi") == "SELL" and t.get("pnl_idr")]
    cum, curve = 0.0, []
    for t in sells:
        cum += _f(t["pnl_idr"])
        curve.append({"t": t["waktu_wib"], "pair": t["pair"], "pnl": _f(t["pnl_idr"]), "cum": cum,
                      "reason": t.get("alasan", "")})

    days = sorted(st.daily_pnl.items())[-30:]
    wins = sum(1 for t in sells if _f(t["pnl_idr"]) > 0)
    gross_win = sum(_f(t["pnl_idr"]) for t in sells if _f(t["pnl_idr"]) > 0)
    gross_loss = -sum(_f(t["pnl_idr"]) for t in sells if _f(t["pnl_idr"]) <= 0)
    month = datetime.now(WIB).strftime("%Y-%m")
    month_pnl = sum(v for d, v in st.daily_pnl.items() if d.startswith(month))

    signals = []
    for pair in cfg["pairs"]:
        q = pairs_status.get(pair, {})
        sig = q.get("signal") or {}
        signals.append({
            "pair": pair, "last": q.get("last"), "spread_pct": q.get("spread_pct"), "vol_idr": q.get("vol_idr"),
            "action": sig.get("action"), "reason": sig.get("reason"), "rsi": sig.get("rsi"),
            "candle_time": sig.get("time"), "in_position": pair in st.positions,
            "cooldown_until": q.get("cooldown_until") or 0,
        })

    risk = cfg["risk"]
    exposure = sum(p.cost_idr for p in st.positions.values() if not p.dust)
    halted = st.halted_day == today_wib(now)
    return {
        "version": __version__,
        "now": now,
        "mode": cfg["mode"],
        "alive": alive, "alive_label": alive_label,
        "paused": (flags / "PAUSE").exists(),
        "sellall_pending": (flags / "SELLALL").exists(),
        "halted_today": halted,
        "entries_blocked": status.get("entries_blocked"),
        "timeframe": cfg["timeframe"],
        "pnl_today": st.daily_pnl.get(today_wib(now), 0.0),
        "pnl_month": month_pnl,
        "pnl_total": st.total_realized,
        "unrealized": unreal,
        "trades_count": len(sells),
        "win_rate": wins / len(sells) * 100 if sells else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss else None,
        "exposure": exposure,
        "paper_equity": status.get("paper_equity"),
        "paper_start": cfg["paper"]["starting_idr"],
        "positions": positions,
        "signals": signals,
        "curve": curve[-500:],
        "daily": [{"d": d, "pnl": v} for d, v in days],
        "trades": list(reversed(trades[-50:])),
        "settings": {
            "pairs": cfg["pairs"],
            "idr_per_trade": risk["idr_per_trade"], "max_open_positions": risk["max_open_positions"],
            "max_total_exposure_idr": risk["max_total_exposure_idr"],
            "daily_loss_limit_idr": risk["daily_loss_limit_idr"],
            "take_profit_pct": cfg["exits"]["take_profit_pct"], "stop_loss_pct": cfg["exits"]["stop_loss_pct"],
            "trailing_stop_pct": cfg["exits"]["trailing_stop_pct"],
            "trailing_activation_pct": cfg["exits"]["trailing_activation_pct"],
            "max_hold_hours": cfg["exits"]["max_hold_hours"],
        },
        "allow_control": bool(cfg["dashboard"]["allow_control"]),
    }


def tail_log(cfg: dict, lines: int = 120) -> list:
    path = Path(cfg["data_dir"]) / "bot.log"
    if not path.exists():
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        return [ln.rstrip("\n") for ln in deque(f, maxlen=lines)]


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_handler(cfg: dict, user: str, password: str):
    ui = UI_FILE.read_text(encoding="utf-8")
    flags = Path(cfg["data_dir"])
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode() if password else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "IndodaxBotDashboard"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _authorized(self) -> bool:
            if expected is None:
                return True
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), expected.encode())

        def _send(self, code: int, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def _deny(self):
            self._send(401, b"Login diperlukan", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="Bot Indodax", charset="UTF-8"'})

        def do_GET(self):
            if not self._authorized():
                return self._deny()
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, ui.encode(), "text/html; charset=utf-8")
            if path == "/api/summary":
                try:
                    return self._json(build_summary(cfg))
                except Exception as e:
                    log.exception("summary gagal")
                    return self._json({"error": str(e)}, 500)
            if path == "/api/log":
                return self._json({"lines": tail_log(cfg)})
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._authorized():
                return self._deny()
            # Header khusus ini tidak bisa dikirim situs lain tanpa izin CORS -> mencegah CSRF.
            if self.headers.get("X-Dashboard") != "1":
                return self._json({"error": "permintaan ditolak"}, 403)
            if not cfg["dashboard"]["allow_control"]:
                return self._json({"error": "kontrol dinonaktifkan di config (dashboard.allow_control)"}, 403)
            action = self.path.split("?")[0].rsplit("/", 1)[-1]
            if action == "pause":
                (flags / "PAUSE").touch()
                msg = "Pembelian baru di-pause. Posisi terbuka tetap dijaga TP/SL."
            elif action == "resume":
                (flags / "PAUSE").unlink(missing_ok=True)
                msg = "Bot kembali boleh membuka posisi baru."
            elif action == "sellall":
                (flags / "PAUSE").touch()
                (flags / "SELLALL").touch()
                msg = "Perintah jual semua dikirim. Bot akan memprosesnya dalam beberapa detik; pembelian baru di-pause."
            else:
                return self._json({"error": "aksi tidak dikenal"}, 404)
            log.warning("Dashboard: aksi %s dari %s", action, self.client_address[0])
            self._json({"ok": True, "message": msg})

    return Handler


def serve(cfg: dict, host: str = None, port: int = None) -> None:
    d = cfg["dashboard"]
    host = host or d["host"]
    port = int(port or d["port"])
    user = os.environ.get("DASHBOARD_USER", "").strip() or "admin"
    password = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not password and not _is_loopback(host):
        raise SystemExit(
            f"Dashboard akan dibuka di {host} (bisa diakses dari internet) tetapi DASHBOARD_PASSWORD kosong.\n"
            f"Isi DASHBOARD_PASSWORD di .env, atau gunakan host 127.0.0.1 + SSH tunnel.")
    if password and len(password) < 10:
        log.warning("DASHBOARD_PASSWORD sebaiknya minimal 10 karakter")
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg, user, password))
    shown = "localhost" if _is_loopback(host) else host
    log.info("Dashboard aktif di http://%s:%d (mode %s)%s", shown, port, cfg["mode"],
             "" if password else " — tanpa password (hanya dari VPS sendiri)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
