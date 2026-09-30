"""Dashboard web: monitoring + login + pengaturan (proses terpisah dari bot trading).

Bot membaca pengaturan dari database; halaman ini hanya menulis pengaturan / kredensial ke database
dan membaca file status bot. Jika dashboard bermasalah, trading tetap berjalan.

  python -m bot web                     # default http://127.0.0.1:8080 (akses via SSH tunnel)
"""
from __future__ import annotations

import hmac
import secrets as pysecrets
import threading
import ipaddress
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .client import IndodaxError, PrivateClientV2, PublicClient
from .config import TIMEFRAMES, ConfigError
from .db import SecretError
from .settings import PAIR_FIELDS, pair_cfg
from .state import WIB, DbStateStore, StateStore, today_wib

log = logging.getLogger("bot.web")
HERE = Path(__file__).parent
PAGES = {"/": "web_ui.html", "/index.html": "web_ui.html", "/settings": "settings.html",
         "/login": "login.html", "/setup": "login.html", "/analysis": "analysis.html",
         "/optimizer": "optimizer.html"}
STATIC = {"/static/app.css": ("static/app.css", "text/css; charset=utf-8"),
          "/static/app.js": ("static/app.js", "application/javascript; charset=utf-8")}


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def build_summary(cfg: dict, store=None) -> dict:
    store = store or StateStore(cfg["data_dir"], cfg["mode"])
    st = store.load()
    status = store.read_status() or {}
    now = time.time()
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
            "stop_loss_pct": pair_cfg(cfg, pair)["exits"]["stop_loss_pct"],
            "take_profit_pct": pair_cfg(cfg, pair)["exits"]["take_profit_pct"],
            "custom": any((cfg.get("pair_settings", {}).get(pair) or {}).get(f) is not None for f in PAIR_FIELDS),
        })

    risk = cfg["risk"]
    exposure = sum(p.cost_idr for p in st.positions.values() if not p.dust)
    halted = st.halted_day == today_wib(now)
    storage_warning = None
    on_railway = os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_ENVIRONMENT_NAME")
    if on_railway and not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        if not os.environ.get("DATABASE_URL", "").startswith(("postgres", "mysql")):
            storage_warning = ("Database SQLite tersimpan di disk sementara Railway dan AKAN HILANG saat redeploy "
                               "(posisi, jurnal transaksi, pengaturan, API key). Tambahkan PostgreSQL "
                               "(DATABASE_URL) atau Volume di /app/data.")
        elif not os.environ.get("BOT_MASTER_KEY"):
            storage_warning = ("Variabel BOT_MASTER_KEY belum diisi, sehingga API key tidak bisa disimpan. "
                               "Isi BOT_MASTER_KEY di Variables Railway.")
    return {
        "storage_warning": storage_warning,
        "version": __version__,
        "now": now,
        "mode": cfg["mode"],
        "alive": alive, "alive_label": alive_label,
        "paused": store.flag("PAUSE"),
        "sellall_pending": store.flag("SELLALL"),
        "halted_today": halted,
        "entries_blocked": status.get("entries_blocked"),
        "timeframe": cfg["timeframe"],
        "running_since": st.first_started_at or status.get("first_started_at") or None,
        "session_since": status.get("session_started_at"),
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


TRADE_META_KEYS = ("hold_s", "mfe_pct", "mae_pct", "entry_price", "rsi")


def build_trades(cfg: dict, store, pair: str = "", side: str = "", limit: int = 100) -> dict:
    """Jurnal transaksi dengan filter pair / sisi, plus ringkasan untuk filter tersebut."""
    pair = (pair or "").lower().strip()
    side = (side or "").upper().strip()
    if pair and not re.fullmatch(r"[a-z0-9]{1,20}idr", pair):
        raise ValueError("pair tidak valid")
    if side not in ("", "BUY", "SELL"):
        raise ValueError("sisi harus BUY atau SELL")
    limit = max(1, min(int(limit or 100), 500))
    rows = store.read_trades(pair=pair or None)          # semua transaksi pair ini (untuk ringkasan)
    sells = [t for t in rows if t.get("sisi") == "SELL" and t.get("pnl_idr") not in ("", None)]
    pnl = [_f(t["pnl_idr"]) for t in sells]
    holds = [(t.get("meta") or {}).get("hold_s") for t in sells]
    holds = [h for h in holds if h]
    st = store.load()
    summary = {
        "buys": sum(1 for t in rows if t.get("sisi") == "BUY"), "sells": len(sells),
        "pnl": sum(pnl), "wins": sum(1 for v in pnl if v > 0),
        "win_rate": (sum(1 for v in pnl if v > 0) / len(pnl) * 100) if pnl else None,
        "fees": sum(_f(t.get("fee_idr")) for t in rows),
        "avg_hold_s": (sum(holds) / len(holds)) if holds else None,
        "first": rows[0].get("ts") if rows else None, "last": rows[-1].get("ts") if rows else None,
        "open": [p for p in st.positions if not pair or p == pair],
    }
    shown = [t for t in rows if not side or t.get("sisi") == side][-limit:]
    out = []
    for t in reversed(shown):
        m = t.get("meta") or {}
        out.append({**{k: v for k, v in t.items() if k != "meta"},
                    "meta": {k: m[k] for k in TRADE_META_KEYS if m.get(k) is not None}})
    known = set(cfg["pairs"]) | set(cfg.get("pair_settings") or {})
    try:
        known |= set(store.db.trade_pairs(store.mode))
    except AttributeError:
        known |= {t.get("pair") for t in store.read_trades() if t.get("pair")}
    return {"pair": pair, "side": side, "limit": limit, "trades": out, "summary": summary,
            "total_rows": len([t for t in rows if not side or t.get("sisi") == side]),
            "pairs": sorted(known), "mode": cfg["mode"]}


def tail_log(db, lines: int = 120) -> list:
    rows = db.tail_logs(lines) if db is not None else []
    return [r["message"] for r in rows]


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LoginGuard:
    """Batasi tebakan password: 5 gagal per IP -> dikunci 10 menit."""

    def __init__(self, limit=5, window=600):
        self.limit, self.window = limit, window
        self.fails = {}
        self.lock = threading.Lock()

    def attempt(self, ip) -> float:
        """Cek + catat percobaan dalam satu kunci (request paralel tidak bisa melewati batas).
        Kembalikan sisa waktu blokir (0 = boleh mencoba). Panggil ok() jika berhasil."""
        with self.lock:
            fails = [t for t in self.fails.get(ip, []) if time.time() - t < self.window]
            self.fails[ip] = fails
            if len(fails) >= self.limit:
                return max(1.0, self.window - (time.time() - fails[0]))
            fails.append(time.time())
            return 0

    def blocked(self, ip) -> float:
        with self.lock:
            fails = [t for t in self.fails.get(ip, []) if time.time() - t < self.window]
            self.fails[ip] = fails
            if len(fails) >= self.limit:
                return self.window - (time.time() - fails[0])
            return 0

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())

    def ok(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


CHART_PERIODS = {            # periode -> (timeframe candle, jumlah hari)
    "1d": ("15", 1),
    "7d": ("60", 7),
    "30d": ("240", 30),
    "90d": ("1D", 90),
}


def _wib_to_ts(s: str) -> float:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=WIB).timestamp()
    except (TypeError, ValueError):
        return 0.0


class ChartCache:
    """Candle harga dari Indodax (di-cache 60 detik per pair & periode)."""

    def __init__(self, public):
        from .market import Market
        self.market = Market(public)
        self.cache = {}
        self.lock = threading.Lock()

    def candles(self, pair: str, period: str):
        tf, days = CHART_PERIODS[period]
        key = (pair, period)
        with self.lock:
            hit = self.cache.get(key)
            if hit and time.time() - hit[0] < 60:
                return hit[1]
        info = self.market.info(pair)
        end = int(time.time())
        rows = self.market.candles_range(info.symbol, tf, end - days * 86400, end, closed_only=False)
        data = [[c.time, c.open, c.high, c.low, c.close] for c in rows]
        with self.lock:
            self.cache[key] = (time.time(), data)
        return data


def build_chart(cfg: dict, charts: ChartCache, pair: str, period: str, store=None) -> dict:
    if period not in CHART_PERIODS:
        raise ValueError("periode tidak dikenal")
    if not re.fullmatch(r"[a-z0-9]{1,20}idr", pair or ""):
        raise ValueError("pair tidak valid")
    candles = charts.candles(pair, period)
    tf, days = CHART_PERIODS[period]
    start = (candles[0][0] if candles else time.time() - days * 86400)
    store = store or StateStore(cfg["data_dir"], cfg["mode"])
    trades = []
    for t in store.read_trades():
        if t.get("pair") != pair:
            continue
        ts = t.get("ts") or _wib_to_ts(t.get("waktu_wib", ""))
        if ts < start:
            continue
        trades.append({"t": ts, "side": t.get("sisi"), "price": _f(t.get("harga")), "qty": _f(t.get("qty")),
                       "idr": _f(t.get("idr")), "pnl": _f(t.get("pnl_idr"), None) if t.get("pnl_idr") else None,
                       "pnl_pct": _f(t.get("pnl_pct"), None) if t.get("pnl_pct") else None,
                       "reason": t.get("alasan", "")})
    st = store.load()
    status = store.read_status() or {}
    q = (status.get("pairs") or {}).get(pair, {})
    pos = st.positions.get(pair)
    position = None
    if pos:
        lv = (status.get("levels") or {}).get(pair, {})
        position = {"entry": pos.entry_price, "qty": pos.qty, "opened_at": pos.opened_at, "cost": pos.cost_idr,
                    "stop": lv.get("stop"), "stop_reason": lv.get("stop_reason"), "take_profit": lv.get("take_profit")}
    last = q.get("last") or (candles[-1][4] if candles else None)
    return {"pair": pair, "period": period, "timeframe": tf, "candles": candles, "trades": trades,
            "position": position, "last": last, "bid": q.get("bid"), "updated": status.get("ts")}


class MarketCache:
    def __init__(self):
        self.public = PublicClient()
        self.data, self.at = None, 0.0
        self.lock = threading.Lock()

    def pairs(self):
        with self.lock:
            if self.data is None or time.time() - self.at > 300:
                pairs = self.public.pairs()
                try:
                    tickers = self.public.ticker_all().get("tickers", {})
                except Exception:
                    tickers = {}
                out = []
                for p in pairs:
                    pid = str(p.get("id", "")).lower()
                    if not pid.endswith("idr"):
                        continue
                    t = tickers.get(p.get("ticker_id") or "", {})
                    out.append({"id": pid, "name": p.get("description") or pid.upper(),
                                "min_idr": float(p.get("trade_min_base_currency") or 0),
                                "last": float(t.get("last") or 0), "vol_idr": float(t.get("vol_idr") or 0)})
                out.sort(key=lambda x: -x["vol_idr"])
                self.data, self.at = out, time.time()
            return self.data


def _num(v):
    if isinstance(v, str):
        v = v.replace(" ", "").replace(",", ".")
    return v


def make_optimizer(ctx, market=None):
    """Optimizer untuk dashboard; notifikasi Telegram memakai kredensial terbaru dari database."""
    from .market import Market
    from .notifier import Notifier
    from .optimizer import Optimizer
    svc, db = ctx.svc, ctx.db

    def notify(text):
        try:
            cfg, c = svc.load(), svc.credentials()
            Notifier(c["telegram_token"], c["telegram_chat_id"], cfg["telegram"].get("enabled"),
                     prefix="[SIMULASI] " if cfg["mode"] == "paper" else "").send(text)
        except Exception as e:
            log.warning("Notifikasi optimizer gagal: %s", e)

    return Optimizer(db, svc, market or Market(PublicClient()), lambda m: DbStateStore(db, m), notify)


def make_handler(ctx, setup_token: dict, optimizer=None):
    svc, db = ctx.svc, ctx.db
    guard = LoginGuard()
    market = MarketCache()
    charts = ChartCache(market.public)
    setup_lock = threading.Lock()
    opt = optimizer or make_optimizer(ctx, charts.market)

    def cfg_now():
        return svc.load()

    def store_for(mode):
        return DbStateStore(db, mode)

    class Handler(BaseHTTPRequestHandler):
        server_version = "IndodaxBotDashboard"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        # ---------------------------------------------------------------- utilitas
        @property
        def ip(self):
            return self.client_address[0]

        def _cookie(self, name):
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return ""

        def _session(self):
            return db.get_session(self._cookie("sid"))

        def _send(self, code, body: bytes, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
            for k, v in (extra or {}).items():
                if isinstance(v, list):
                    for x in v:
                        self.send_header(k, x)
                else:
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200, extra=None):
            self._send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8", extra)

        def _redirect(self, to):
            self._send(302, b"", "text/plain", {"Location": to})

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 100_000:  # -1 membuat rfile.read() menunggu sampai koneksi ditutup
                raise ValueError("permintaan terlalu besar")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise ValueError("format data tidak valid")
            if not isinstance(data, (dict, list)):
                raise ValueError("format data tidak valid")
            return data

        def _set_cookie(self, token, max_age):
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "") == "https" else ""
            return f"sid={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

        def _auth_api(self, need_csrf=False):
            sess = self._session()
            if not sess:
                self._json({"error": "sesi berakhir, silakan login lagi", "login": True}, 401)
                return None
            if need_csrf and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), sess["csrf"]):
                self._json({"error": "token keamanan tidak valid, muat ulang halaman"}, 403)
                return None
            return sess

        def _check_pw(self, sess, pw) -> bool:
            if guard.attempt(self.ip):
                return False
            if db.check_login(sess["username"], str(pw or "")):
                guard.ok(self.ip)
                return True
            return False

        # ---------------------------------------------------------------- GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if path in STATIC:
                f, ctype = STATIC[path]
                return self._send(200, (HERE / f).read_bytes(), ctype, {"Cache-Control": "no-cache"})
            if path in PAGES:
                no_users = db.count_users() == 0
                if path == "/setup" and not no_users:
                    return self._redirect("/login")
                if path == "/login" and no_users:
                    return self._redirect("/setup")
                if path not in ("/login", "/setup") and not self._session():
                    return self._redirect("/setup" if no_users else "/login")
                return self._send(200, (HERE / PAGES[path]).read_bytes(), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self._send(404, b"tidak ditemukan", "text/plain; charset=utf-8")

            if path == "/api/setup-status":
                return self._json({"needs_setup": db.count_users() == 0})
            sess = self._auth_api()
            if not sess:
                return
            try:
                if path == "/api/me":
                    return self._json({"username": sess["username"], "csrf": sess["csrf"]})
                if path == "/api/summary":
                    cfg = cfg_now()
                    return self._json(build_summary(cfg, store_for(cfg["mode"])))
                if path == "/api/log":
                    return self._json({"lines": tail_log(db)})
                if path == "/api/settings":
                    return self._json(self._settings_payload())
                if path == "/api/market/pairs":
                    return self._json({"pairs": market.pairs()})
                if path == "/api/audit":
                    return self._json({"items": db.recent_audit(60)})
                if path == "/api/analysis":
                    from urllib.parse import parse_qs, urlparse
                    from .analysis import build_analysis
                    qs = parse_qs(urlparse(self.path).query)
                    cfg = cfg_now()
                    mode = (qs.get("mode") or [cfg["mode"]])[0]
                    period = (qs.get("period") or ["all"])[0]
                    if mode not in ("paper", "live") or period not in ("all", "7d", "30d", "90d"):
                        return self._json({"error": "parameter tidak valid"}, 400)
                    last_change = next((a["ts"] for a in db.recent_audit(500)
                                        if a["action"].startswith(("settings_", "optimizer_apply",
                                                                   "optimizer_rollback"))), None)
                    data = build_analysis(store_for(mode).read_trades(), cfg, lambda p: pair_cfg(cfg, p),
                                          period, last_change)
                    data.update({"mode": mode, "current_mode": cfg["mode"]})
                    return self._json(data)
                if path == "/api/trades":
                    from urllib.parse import parse_qs, urlparse
                    qs = parse_qs(urlparse(self.path).query)
                    cfg = cfg_now()
                    try:
                        return self._json(build_trades(cfg, store_for(cfg["mode"]), (qs.get("pair") or [""])[0],
                                                       (qs.get("side") or [""])[0], (qs.get("limit") or ["100"])[0]))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                if path == "/api/optimizer":
                    return self._json(opt.overview())
                if path == "/api/chart":
                    from urllib.parse import parse_qs, urlparse
                    qs = parse_qs(urlparse(self.path).query)
                    pair = (qs.get("pair") or [""])[0].lower()
                    period = (qs.get("period") or ["1d"])[0]
                    try:
                        cfg = cfg_now()
                        return self._json(build_chart(cfg, charts, pair, period, store_for(cfg["mode"])))
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    except KeyError as e:
                        return self._json({"error": str(e).strip("'\"")}, 404)
            except Exception as e:
                log.exception("GET %s gagal", path)
                return self._json({"error": str(e)}, 500)
            self._json({"error": "tidak ditemukan"}, 404)

        def _settings_payload(self):
            cfg = cfg_now()
            info = db.secret_info()
            positions = store_for(cfg["mode"]).load().positions
            return {
                "sections": svc.sections(),
                "effective": {p: {"exits": pair_cfg(cfg, p)["exits"], "idr_per_trade": pair_cfg(cfg, p)["risk"]["idr_per_trade"]}
                              for p in cfg["pair_settings"]},
                "open_positions": list(positions),
                "timeframes": list(TIMEFRAMES),
                "credentials": {
                    "indodax": {"api_key": info.get("indodax_api_key"), "secret_key": info.get("indodax_secret_key")},
                    "telegram": {"token": info.get("telegram_token"), "chat_id": info.get("telegram_chat_id")},
                },
                "master_key": bool(os.environ.get("BOT_MASTER_KEY")),
                "database": "PostgreSQL" if db.url.startswith("postgres") else "SQLite",
            }

        # ---------------------------------------------------------------- POST / PUT / DELETE
        def do_POST(self):
            self._write("POST")

        def do_PUT(self):
            self._write("PUT")

        def do_DELETE(self):
            self._write("DELETE")

        def _write(self, method):
            path = self.path.split("?")[0]
            try:
                if path == "/api/login":
                    return self._login()
                if path == "/api/setup":
                    return self._setup()
                sess = self._auth_api(need_csrf=True)
                if not sess:
                    return
                body = self._body()
                user = sess["username"]
                if path == "/api/logout":
                    db.delete_session(self._cookie("sid"))
                    return self._json({"ok": True}, extra={"Set-Cookie": self._set_cookie("", 0)})
                if path.startswith("/api/control/"):
                    return self._control(path.rsplit("/", 1)[-1], user)
                if path.startswith("/api/settings/") and method == "PUT":
                    return self._update_settings(path.rsplit("/", 1)[-1], body, sess)
                if path == "/api/credentials/indodax":
                    return self._cred_indodax(method, body, sess)
                if path == "/api/credentials/indodax/test":
                    return self._test_indodax(body)
                if path == "/api/credentials/telegram":
                    return self._cred_telegram(method, body, sess)
                if path == "/api/account/password":
                    return self._change_password(body, sess)
                if path == "/api/optimizer/run" and method == "POST":
                    if not opt.trigger(user):
                        return self._json({"error": "Optimizer sedang berjalan, tunggu sampai selesai."}, 409)
                    db.audit(user, "optimizer_run", "dijalankan manual", self.ip)
                    return self._json({"ok": True, "message": "Optimizer dijalankan. Hasil muncul dalam 1–3 menit."})
                m = re.fullmatch(r"/api/optimizer/runs/(\d+)/(apply|reject|rollback)", path)
                if m and method == "POST":
                    rid, act = int(m.group(1)), m.group(2)
                    if act == "apply":
                        opt.apply_run(rid, user)
                        msg = "Usulan diterapkan. Bot memakai pengaturan baru dalam ±20 detik."
                    elif act == "reject":
                        opt.reject_run(rid, user)
                        msg = "Usulan ditolak."
                    else:
                        opt.rollback_run(rid, user)
                        msg = "Pengaturan sebelum perubahan ini dipulihkan."
                    return self._json({"ok": True, "message": msg, **opt.overview()})
                self._json({"error": "tidak ditemukan"}, 404)
            except (ValueError, ConfigError) as e:
                self._json({"error": str(e)}, 400)
            except SecretError as e:
                self._json({"error": str(e)}, 500)
            except Exception as e:
                log.exception("%s %s gagal", method, path)
                self._json({"error": f"terjadi kesalahan: {e}"}, 500)

        def _login(self):
            if self.headers.get("X-Dashboard") != "1":
                return self._json({"error": "permintaan ditolak"}, 403)
            b = self._body()
            wait = guard.attempt(self.ip)
            if wait:
                return self._json({"error": f"Terlalu banyak percobaan gagal. Coba lagi {int(wait // 60) + 1} menit lagi."}, 429)
            u = db.check_login(str(b.get("username", "")).strip(), str(b.get("password", "")))
            if not u:
                db.audit(str(b.get("username", ""))[:64], "login_failed", "", self.ip)
                return self._json({"error": "Username atau password salah."}, 401)
            guard.ok(self.ip)
            token, csrf = db.create_session(u["id"], self.ip)
            db.audit(u["username"], "login", "", self.ip)
            self._json({"ok": True, "csrf": csrf}, extra={"Set-Cookie": self._set_cookie(token, 12 * 3600)})

        def _setup(self):
            if self.headers.get("X-Dashboard") != "1":
                return self._json({"error": "permintaan ditolak"}, 403)
            b = self._body()
            with setup_lock:  # dua request setup bersamaan tidak boleh membuat dua admin
                return self._setup_locked(b)

        def _setup_locked(self, b):
            if db.count_users() > 0:
                return self._json({"error": "Admin sudah dibuat. Silakan login."}, 409)
            wait = guard.attempt(self.ip)
            if wait:
                return self._json({"error": "Terlalu banyak percobaan. Coba lagi nanti."}, 429)
            if not setup_token.get("value") or not hmac.compare_digest(
                    str(b.get("token", "")).strip().encode(), setup_token["value"].encode()):
                return self._json({"error": "Kode setup salah. Lihat log dashboard di VPS (journalctl -u indodax-bot-web)."}, 403)
            guard.ok(self.ip)  # kode benar; salah isi username/password tidak dihitung sebagai tebakan
            name = str(b.get("username", "")).strip()
            pw = str(b.get("password", ""))
            if not name or len(name) > 64:
                return self._json({"error": "Username wajib diisi."}, 400)
            if len(pw) < 10:
                return self._json({"error": "Password minimal 10 karakter."}, 400)
            db.add_user(name, pw)
            setup_token["value"] = None
            db.audit(name, "user_add", "admin pertama via halaman setup", self.ip)
            u = db.check_login(name, pw)
            token, csrf = db.create_session(u["id"], self.ip)
            self._json({"ok": True, "csrf": csrf}, extra={"Set-Cookie": self._set_cookie(token, 12 * 3600)})

        def _control(self, action, user):
            cfg = cfg_now()
            if not cfg["dashboard"]["allow_control"]:
                return self._json({"error": "Tombol kontrol dinonaktifkan di pengaturan."}, 403)
            st = store_for(cfg["mode"])
            if action == "pause":
                st.set_flag("PAUSE", True)
                msg = "Pembelian baru di-pause. Posisi terbuka tetap dijaga TP/SL."
            elif action == "resume":
                st.set_flag("PAUSE", False)
                msg = "Bot kembali boleh membuka posisi baru."
            elif action == "sellall":
                st.set_flag("PAUSE", True)
                st.set_flag("SELLALL", True)
                msg = "Perintah jual semua dikirim. Bot akan memprosesnya dalam beberapa detik; pembelian baru di-pause."
            else:
                return self._json({"error": "aksi tidak dikenal"}, 404)
            db.audit(user, f"control_{action}", "", self.ip)
            self._json({"ok": True, "message": msg})

        def _update_settings(self, section, body, sess):
            user = sess["username"]
            value = body.get("value") if isinstance(body, dict) else None
            if value is None:
                raise ValueError("data pengaturan kosong")
            if section == "pairs":
                rows = []
                for r in value:
                    row = {"pair": r.get("pair"), "enabled": bool(r.get("enabled", True))}
                    for f in PAIR_FIELDS:
                        row[f] = _num(r.get(f))
                    rows.append(row)
                value = rows
            elif isinstance(value, dict):
                value = {k: _num(v) for k, v in value.items()}
            if section == "general":
                cur = cfg_now()["mode"]
                new_mode = value.get("mode", cur)
                if cur == "live" and new_mode == "paper":
                    live_pos = [p for p, pos in store_for("live").load().positions.items()
                                if not pos.dust]
                    if live_pos:
                        return self._json({"error": "Masih ada posisi LIVE terbuka (" + ", ".join(live_pos).upper()
                                           + "). Jual dulu (tombol Jual semua di dashboard) sebelum pindah ke "
                                             "SIMULASI, agar posisi itu tidak dibiarkan tanpa stop loss."}, 400)
                if new_mode != cur:
                    if not self._check_pw(sess, body.get("password")):
                        return self._json({"error": "Password salah — perubahan mode dibatalkan."}, 403)
                    if new_mode == "live":
                        if body.get("confirm") != "LIVE":
                            return self._json({"error": "Ketik LIVE untuk mengonfirmasi mode uang sungguhan."}, 400)
                        c = svc.credentials()
                        if not c["api_key"] or not c["secret_key"]:
                            return self._json({"error": "Isi API key Indodax dulu sebelum beralih ke mode LIVE."}, 400)
            if section == "optimizer" and str(value.get("apply_in_live", "")).lower() in ("true", "1", "yes") \
                    and not cfg_now()["optimizer"]["apply_in_live"]:
                if not self._check_pw(sess, body.get("password")):
                    return self._json({"error": "Password diperlukan untuk mengizinkan optimizer mengubah "
                                                "pengaturan saat mode LIVE."}, 403)
            before = svc.sections().get(section)
            svc.update(section, value, user=user)
            after = svc.sections().get(section)
            db.audit(user, f"settings_{section}", _diff(before, after), self.ip)
            msg = "Tersimpan. Bot menerapkan perubahan dalam ±20 detik."
            if section == "general" and before.get("mode") != after.get("mode"):
                msg = f"Mode diubah ke {after['mode'].upper()}. Bot akan dimulai ulang otomatis dalam ±20 detik."
            self._json({"ok": True, "message": msg, "settings": self._settings_payload()})

        def _cred_indodax(self, method, body, sess):
            user = sess["username"]
            if not self._check_pw(sess, body.get("password")):
                return self._json({"error": "Password login salah."}, 403)
            if method == "DELETE":
                if cfg_now()["mode"] == "live":
                    return self._json({"error": "Tidak bisa menghapus API key saat mode LIVE. Pindah ke SIMULASI dulu."}, 400)
                db.delete_secret("indodax_api_key")
                db.delete_secret("indodax_secret_key")
                db.audit(user, "credentials_indodax_delete", "", self.ip)
                return self._json({"ok": True, "message": "API key dihapus.", "settings": self._settings_payload()})
            key = str(body.get("api_key", "")).strip()
            secret = str(body.get("secret_key", "")).strip()
            if len(key) < 10 or len(secret) < 20:
                return self._json({"error": "API key / secret key terlihat tidak lengkap."}, 400)
            info = None
            if body.get("test", True):
                ok, info = _test_keys(key, secret)
                if not ok:
                    return self._json({"error": f"Uji koneksi gagal, key TIDAK disimpan: {info}"}, 400)
            db.set_secret("indodax_api_key", key, user)
            db.set_secret("indodax_secret_key", secret, user)
            db.audit(user, "credentials_indodax_set", "tanpa uji" if info is None else "lolos uji koneksi", self.ip)
            msg = "API key tersimpan (terenkripsi) dan langsung dipakai bot."
            if info and info.get("canWithdraw"):
                msg += " PERINGATAN: key ini punya izin withdraw — sebaiknya buat key tanpa izin withdraw."
            self._json({"ok": True, "message": msg, "account": info, "settings": self._settings_payload()})

        def _test_indodax(self, body):
            c = svc.credentials()
            if not c["api_key"] or not c["secret_key"]:
                return self._json({"error": "Belum ada API key tersimpan."}, 400)
            ok, info = _test_keys(c["api_key"], c["secret_key"])
            if not ok:
                return self._json({"error": f"Gagal: {info}"}, 400)
            self._json({"ok": True, "account": info,
                        "message": f"Terhubung. canTrade={info.get('canTrade')}, canWithdraw={info.get('canWithdraw')}"})

        def _cred_telegram(self, method, body, sess):
            user = sess["username"]
            if not self._check_pw(sess, body.get("password")):
                return self._json({"error": "Password login salah."}, 403)
            if method == "DELETE":
                db.delete_secret("telegram_token")
                db.delete_secret("telegram_chat_id")
                db.audit(user, "credentials_telegram_delete", "", self.ip)
                return self._json({"ok": True, "message": "Kredensial Telegram dihapus.", "settings": self._settings_payload()})
            token = str(body.get("token", "")).strip()
            chat = str(body.get("chat_id", "")).strip()
            if not token or not chat:
                return self._json({"error": "Token dan chat ID wajib diisi."}, 400)
            import requests as rq
            try:
                r = rq.post(f"https://api.telegram.org/bot{token}/sendMessage",
                            data={"chat_id": chat, "text": "Tes notifikasi dari dashboard bot Indodax ✅"}, timeout=10)
                if r.status_code != 200:
                    return self._json({"error": f"Telegram menolak ({r.status_code}): periksa token / chat ID."}, 400)
            except rq.RequestException as e:  # pesan error requests memuat URL yang berisi token
                return self._json({"error": f"Tidak bisa menghubungi Telegram: {str(e).replace(token, '***')}"}, 400)
            db.set_secret("telegram_token", token, user)
            db.set_secret("telegram_chat_id", chat, user)
            db.audit(user, "credentials_telegram_set", "", self.ip)
            self._json({"ok": True, "message": "Tersimpan — pesan tes sudah dikirim ke Telegram.",
                        "settings": self._settings_payload()})

        def _change_password(self, body, sess):
            if not self._check_pw(sess, body.get("old")):
                return self._json({"error": "Password lama salah."}, 403)
            new = str(body.get("new", ""))
            if len(new) < 10:
                return self._json({"error": "Password baru minimal 10 karakter."}, 400)
            db.set_password(sess["username"], new)
            db.audit(sess["username"], "password_change", "via web", self.ip)
            self._json({"ok": True, "message": "Password diganti. Silakan login lagi.", "login": True},
                       extra={"Set-Cookie": self._set_cookie("", 0)})

    return Handler


def _diff(before, after) -> str:
    if isinstance(before, dict) and isinstance(after, dict):
        ch = [f"{k}: {before.get(k)} → {after.get(k)}" for k in after if before.get(k) != after.get(k)]
        return "; ".join(ch) or "tanpa perubahan"
    if isinstance(before, list) and isinstance(after, list):
        b = {r["pair"]: r for r in before}
        a = {r["pair"]: r for r in after}
        ch = [f"+{p}" for p in a if p not in b] + [f"-{p}" for p in b if p not in a]
        for p in a:
            if p in b and a[p] != b[p]:
                ch.append(f"{p}: " + ", ".join(f"{k} {b[p].get(k)}→{a[p].get(k)}" for k in a[p] if a[p].get(k) != b[p].get(k)))
        return "; ".join(ch) or "tanpa perubahan"
    return ""


def _test_keys(key: str, secret: str):
    try:
        api = PrivateClientV2(key, secret, PublicClient())
        api.sync_time()
        acc = api.account()
        return True, {"uid": acc.get("uid"), "canTrade": acc.get("canTrade"), "canWithdraw": acc.get("canWithdraw")}
    except IndodaxError as e:
        hint = ""
        if e.code in (-1002, -2014):
            hint = " (pastikan key TAPI v2, bukan key lama)"
        elif e.code == -2015:
            hint = " (periksa izin key & IP whitelist VPS)"
        return False, f"{e}{hint}"
    except Exception as e:
        return False, str(e)


def serve(ctx, host: str = None, port: int = None) -> None:
    d = ctx.cfg["dashboard"]
    env_port = os.environ.get("PORT", "").strip()      # Railway / Render / Heroku
    host = host or os.environ.get("DASHBOARD_HOST", "").strip() or ("0.0.0.0" if env_port else d["host"])
    port = int(port or env_port or d["port"])
    if not os.environ.get("BOT_MASTER_KEY"):
        log.warning("BOT_MASTER_KEY belum diisi — kredensial tidak bisa disimpan. Jalankan `python -m bot init`.")
    setup_token = {"value": None}
    admin_u = os.environ.get("ADMIN_USERNAME", "").strip()
    admin_p = os.environ.get("ADMIN_PASSWORD", "")
    if ctx.db.count_users() == 0 and admin_u and len(admin_p) >= 10:
        ctx.db.add_user(admin_u, admin_p)
        ctx.db.audit(admin_u, "user_add", "admin pertama dari variabel ADMIN_USERNAME/ADMIN_PASSWORD")
        log.warning("Akun admin '%s' dibuat dari variabel lingkungan. Hapus ADMIN_PASSWORD dari variabel "
                    "setelah berhasil login.", admin_u)
    if ctx.db.count_users() == 0:
        setup_token["value"] = pysecrets.token_hex(4).upper()
        log.warning("Belum ada akun admin. Buka dashboard dan masukkan KODE SETUP: %s "
                    "(atau buat lewat terminal: python -m bot user add admin)", setup_token["value"])
    from .optimizer import OptimizerThread
    opt = make_optimizer(ctx)
    OptimizerThread(opt).start()     # evaluasi & penyetelan strategi berkala di background
    httpd = ThreadingHTTPServer((host, port), make_handler(ctx, setup_token, opt))
    shown = "localhost" if _is_loopback(host) else host
    log.info("Dashboard aktif di http://%s:%d", shown, port)
    behind_https_proxy = any(os.environ.get(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
                                                         "RENDER", "DYNO"))
    if not _is_loopback(host) and not behind_https_proxy:
        log.warning("Dashboard terbuka ke jaringan tanpa HTTPS. Gunakan reverse proxy HTTPS atau SSH tunnel.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
