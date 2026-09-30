"""Tes database pengaturan, kredensial terenkripsi, login web, dan pengaturan per pair."""
import copy
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests as rq

from bot.config import DEFAULTS, ConfigError
from bot.db import Database, SecretError, generate_master_key, hash_password, verify_password
from bot.settings import SettingsService, assemble, load_bootstrap, pair_cfg, validate_all

from .fakes import trend_series
from .test_bot import build_engine, find_buy_index


def make_ctx(tmp_path, key=None):
    boot = {"data_dir": str(tmp_path), "dashboard": {"host": "127.0.0.1", "port": 0}, "_raw": {}}
    import os
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'bot.db'}"   # uji juga di PostgreSQL
    db = Database(url, key or generate_master_key())
    if os.environ.get("TEST_DATABASE_URL"):
        from bot.db import metadata
        metadata.drop_all(db.engine)
    db.create_all()
    svc = SettingsService(db, boot)
    svc.ensure_seeded({"pairs": ["btcidr", "pepeidr"]})
    return SimpleNamespace(db=db, svc=svc, boot=boot, cfg=svc.load())


# ---------------- database & pengaturan ----------------
def test_password_hash():
    h = hash_password("rahasia-panjang")
    assert verify_password("rahasia-panjang", h) and not verify_password("salah", h)


def test_secrets_encrypted_and_hint(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.db.set_secret("indodax_secret_key", "abcdefghijklmnopqrstuvwxyz123456", "admin")
    with ctx.db.engine.connect() as c:
        raw = c.exec_driver_sql("select ciphertext from secrets").scalar_one()
    assert "abcdefghij" not in raw                                   # tidak tersimpan polos
    assert ctx.db.get_secret("indodax_secret_key").endswith("123456")
    assert ctx.db.secret_info()["indodax_secret_key"]["hint"] == "••••3456"
    other = Database(ctx.db.url, generate_master_key())              # kunci berbeda
    with pytest.raises(SecretError):
        other.get_secret("indodax_secret_key")


def test_seed_and_per_pair_override(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.cfg["pairs"] == ["btcidr", "pepeidr"]
    v0 = ctx.db.version("config_version")
    ctx.svc.update("pairs", [{"pair": "btcidr", "enabled": True},
                             {"pair": "pepeidr", "enabled": True, "stop_loss_pct": 6, "idr_per_trade": 50000},
                             {"pair": "dogeidr", "enabled": False}], user="admin")
    cfg = ctx.svc.load()
    assert ctx.db.version("config_version") == v0 + 1
    assert cfg["pairs"] == ["btcidr", "pepeidr"]
    assert pair_cfg(cfg, "pepeidr")["exits"]["stop_loss_pct"] == 6
    assert pair_cfg(cfg, "pepeidr")["risk"]["idr_per_trade"] == 50000
    assert pair_cfg(cfg, "btcidr")["exits"]["stop_loss_pct"] == DEFAULTS["exits"]["stop_loss_pct"]


def test_invalid_settings_rejected(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ConfigError):
        ctx.svc.update("pairs", [{"pair": "btcidr", "stop_loss_pct": 0}])
    with pytest.raises(ConfigError):
        ctx.svc.update("pairs", [{"pair": "btcidr", "idr_per_trade": 1000}])
    with pytest.raises(ConfigError):
        ctx.svc.update("exits", {"stop_loss_pct": 0})
    with pytest.raises(ConfigError):
        ctx.svc.update("strategy", {"ema_fast": 50, "ema_slow": 21})
    assert ctx.svc.load()["exits"]["stop_loss_pct"] == DEFAULTS["exits"]["stop_loss_pct"]  # tidak berubah


def test_seed_from_old_config_yaml(tmp_path):
    (tmp_path / "config.yaml").write_text("mode: paper\npairs: [solidr]\nexits:\n  stop_loss_pct: 3.5\n")
    boot = load_bootstrap(str(tmp_path / "config.yaml"))
    boot["data_dir"] = str(tmp_path)
    db = Database(f"sqlite:///{tmp_path / 'x.db'}", generate_master_key())
    db.create_all()
    svc = SettingsService(db, boot)
    assert svc.ensure_seeded() and not svc.ensure_seeded()
    cfg = svc.load()
    assert cfg["pairs"] == ["solidr"] and cfg["exits"]["stop_loss_pct"] == 3.5


# ---------------- engine: stop loss per pair & reload ----------------
class Src:
    def __init__(self):
        self.cfg = None
        self.creds = None

    def poll_config(self):
        c, self.cfg = self.cfg, None
        return c

    def poll_credentials(self):
        c, self.creds = self.creds, None
        return c


def test_engine_uses_per_pair_stop_loss_and_reloads(tmp_path):
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    eng, pub, clock = build_engine(tmp_path, closes)
    src = Src()
    eng.config_source = src
    eng.startup()
    eng.tick()
    pos = eng.state.positions["btcidr"]
    # stop loss khusus BTC 8% -> turun 4% tidak memicu jual
    cfg = copy.deepcopy(eng.cfg)
    cfg["pair_settings"] = {"btcidr": {"pair": "btcidr", "enabled": True, "stop_loss_pct": 8.0,
                                       "take_profit_pct": None, "trailing_stop_pct": None,
                                       "trailing_activation_pct": None, "idr_per_trade": None}}
    src.cfg = cfg
    pub.last_override["BTCIDR"] = pos.entry_price * 0.96
    clock.t += 20
    eng.tick()
    assert "btcidr" in eng.state.positions
    pub.last_override["BTCIDR"] = pos.entry_price * 0.91
    clock.t += 20
    eng.tick()
    assert "btcidr" not in eng.state.positions


def test_engine_mode_change_requests_restart(tmp_path):
    eng, pub, clock = build_engine(tmp_path, trend_series()[:200])
    src = Src()
    eng.config_source = src
    eng.startup()
    cfg = copy.deepcopy(eng.cfg)
    cfg["mode"] = "live"
    src.cfg = cfg
    eng.tick()
    assert eng._stop and eng.exit_code == 75


# ---------------- web: setup, login, CSRF, pengaturan, kredensial ----------------
@pytest.fixture
def server(tmp_path):
    from bot.web import make_handler
    ctx = make_ctx(tmp_path)
    token = {"value": "KODE1234"}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ctx, token))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield ctx, f"http://127.0.0.1:{httpd.server_address[1]}", token
    httpd.shutdown()


H = {"X-Dashboard": "1"}


def _login(url, user="admin", pw="password-kuat-1"):
    s = rq.Session()
    r = s.post(url + "/api/login", json={"username": user, "password": pw}, headers=H)
    assert r.status_code == 200, r.text
    s.headers.update({"X-CSRF-Token": r.json()["csrf"], **H})
    return s


def test_setup_requires_code_then_login(server):
    ctx, url, token = server
    r = rq.get(url + "/", allow_redirects=False)
    assert r.status_code == 302 and r.headers["Location"] == "/setup"
    assert rq.get(url + "/api/summary").status_code == 401
    r = rq.post(url + "/api/setup", json={"token": "SALAH", "username": "admin", "password": "password-kuat-1"}, headers=H)
    assert r.status_code == 403
    r = rq.post(url + "/api/setup", json={"token": "KODE1234", "username": "admin", "password": "pendek"}, headers=H)
    assert r.status_code == 400
    r = rq.post(url + "/api/setup", json={"token": "KODE1234", "username": "admin", "password": "password-kuat-1"}, headers=H)
    assert r.status_code == 200 and "sid=" in r.headers["Set-Cookie"] and "HttpOnly" in r.headers["Set-Cookie"]
    assert token["value"] is None
    r = rq.post(url + "/api/setup", json={"token": "KODE1234", "username": "x", "password": "password-kuat-2"}, headers=H)
    assert r.status_code == 409                                     # setup hanya sekali
    s = _login(url)
    assert s.get(url + "/api/summary").status_code == 200
    assert s.get(url + "/settings").status_code == 200


def test_login_bruteforce_lock_and_csrf(server):
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    for _ in range(5):
        assert rq.post(url + "/api/login", json={"username": "admin", "password": "x"}, headers=H).status_code == 401
    r = rq.post(url + "/api/login", json={"username": "admin", "password": "password-kuat-1"}, headers=H)
    assert r.status_code == 429


def test_csrf_required_for_changes(server):
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    s = _login(url)
    bad = rq.Session()
    bad.cookies = s.cookies
    r = bad.put(url + "/api/settings/exits", json={"value": {"stop_loss_pct": 3}})
    assert r.status_code == 403


def test_settings_api_pairs_and_audit(server):
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    s = _login(url)
    d = s.get(url + "/api/settings").json()
    assert [r["pair"] for r in d["sections"]["pairs"]] == ["btcidr", "pepeidr"]
    rows = [{"pair": "btcidr", "enabled": True, "stop_loss_pct": ""},
            {"pair": "pepeidr", "enabled": True, "stop_loss_pct": "7,5", "take_profit_pct": "12"},
            {"pair": "SOL_IDR", "enabled": False}]
    r = s.put(url + "/api/settings/pairs", json={"value": rows})
    assert r.status_code == 200, r.text
    cfg = ctx.svc.load()
    assert pair_cfg(cfg, "pepeidr")["exits"]["stop_loss_pct"] == 7.5
    assert "solidr" in cfg["pair_settings"] and "solidr" not in cfg["pairs"]
    r = s.put(url + "/api/settings/pairs", json={"value": [{"pair": "btcidr", "stop_loss_pct": "-1"}]})
    assert r.status_code == 400
    r = s.put(url + "/api/settings/exits", json={"value": {"stop_loss_pct": "3,2"}})
    assert r.status_code == 200 and ctx.svc.load()["exits"]["stop_loss_pct"] == 3.2
    items = s.get(url + "/api/audit").json()["items"]
    assert any(a["action"] == "settings_pairs" and "pepeidr" in a["detail"] for a in items)


def test_mode_live_needs_password_confirm_and_key(server):
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    s = _login(url)
    r = s.put(url + "/api/settings/general", json={"value": {"mode": "live"}, "password": "salah", "confirm": "LIVE"})
    assert r.status_code == 403
    r = s.put(url + "/api/settings/general", json={"value": {"mode": "live"}, "password": "password-kuat-1", "confirm": "LIVE"})
    assert r.status_code == 400 and "API key" in r.json()["error"]
    ctx.db.set_secret("indodax_api_key", "KEY-1234567890")
    ctx.db.set_secret("indodax_secret_key", "s" * 40)
    r = s.put(url + "/api/settings/general", json={"value": {"mode": "live"}, "password": "password-kuat-1", "confirm": "nope"})
    assert r.status_code == 400
    r = s.put(url + "/api/settings/general", json={"value": {"mode": "live"}, "password": "password-kuat-1", "confirm": "LIVE"})
    assert r.status_code == 200 and ctx.svc.load()["mode"] == "live"


def test_credentials_saved_encrypted_never_returned(server):
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    s = _login(url)
    body = {"api_key": "ABCDEFGH-12345678", "secret_key": "x" * 60 + "WXYZ", "password": "salah", "test": False}
    assert s.post(url + "/api/credentials/indodax", json=body).status_code == 403
    body["password"] = "password-kuat-1"
    r = s.post(url + "/api/credentials/indodax", json=body)
    assert r.status_code == 200, r.text
    assert "x" * 20 not in r.text                                     # secret tidak dikirim balik
    assert r.json()["settings"]["credentials"]["indodax"]["secret_key"]["hint"] == "••••WXYZ"
    assert ctx.svc.credentials()["secret_key"].endswith("WXYZ")
    assert ctx.db.version("secrets_version") >= 2


def test_password_change_logs_out(server):
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    s = _login(url)
    r = s.post(url + "/api/account/password", json={"old": "password-kuat-1", "new": "password-baru-2"})
    assert r.status_code == 200
    assert s.get(url + "/api/summary").status_code == 401
    _login(url, pw="password-baru-2")


# ---------------- perbaikan hasil review ----------------
@pytest.mark.parametrize("section,value", [
    ("exits", {"stop_loss_pct": "nan"}),
    ("exits", {"stop_loss_pct": "inf"}),
    ("risk", {"max_total_exposure_idr": "inf"}),
    ("exits", {"max_hold_hours": -1}),
    ("general", {"poll_seconds": 0}),
    ("strategy", {"rsi_period": 0}),
    ("pairs", [{"pair": "btcidr", "stop_loss_pct": "NaN"}]),
    ("pairs", [{"pair": "btcidr", "trailing_stop_pct": 150}]),
    ("pairs", [{"pair": "<img src=x onerror=alert(1)>idr"}]),
])
def test_non_finite_and_unsafe_values_rejected(tmp_path, section, value):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ConfigError):
        ctx.svc.update(section, value)


def test_pair_enabled_string_false(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.svc.update("pairs", [{"pair": "btcidr", "enabled": True}, {"pair": "pepeidr", "enabled": "false"}])
    assert ctx.svc.load()["pairs"] == ["btcidr"]


def test_invalid_master_key_does_not_crash(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'k.db'}", "bukan-kunci-fernet")
    db.create_all()
    with pytest.raises(SecretError):
        db.set_secret("x", "y")


def test_version_bump_is_sql_increment(tmp_path):
    ctx = make_ctx(tmp_path)
    v = ctx.db.version("config_version")
    ctx.db.put_settings({"fees": {"buy_pct": 0.3, "sell_pct": 0.51}})
    ctx.db.put_settings({"fees": {"buy_pct": 0.3, "sell_pct": 0.51}})
    assert ctx.db.version("config_version") == v + 2
    assert ctx.db.version("baru") == 0
    with ctx.db.engine.begin() as c:
        ctx.db._bump(c, "baru")
    assert ctx.db.version("baru") == 1


def test_engine_reload_pair_lookup_failure_drops_disabled_pairs(tmp_path):
    eng, pub, clock = build_engine(tmp_path, trend_series()[:200])
    src = Src()
    eng.config_source = src
    eng.startup()
    assert "btcidr" in eng.pairs
    cfg = copy.deepcopy(eng.cfg)
    cfg["pairs"] = [p for p in cfg["pairs"] if p != "btcidr"]
    real = eng.market.load_pairs
    eng.market.load_pairs = lambda: (_ for _ in ()).throw(RuntimeError("jaringan putus"))
    src.cfg = cfg
    eng.tick()
    assert "btcidr" not in eng.pairs and eng._pairs_stale
    eng.market.load_pairs = real
    clock.t += 20
    eng.tick()
    assert not eng._pairs_stale and "btcidr" not in eng.pairs


def test_notifier_redacts_token_in_log(caplog):
    from bot.notifier import Notifier
    n = Notifier("123:SECRET-TOKEN", "42", True)
    import bot.notifier as nm
    orig = nm.requests.post
    def boom(url, **kw):
        raise nm.requests.ConnectionError(f"Max retries exceeded with url: {url}")
    nm.requests.post = boom
    try:
        n.send("halo")
    finally:
        nm.requests.post = orig
    assert "SECRET-TOKEN" not in caplog.text and "***" in caplog.text


def test_setup_race_creates_single_admin(server):
    ctx, url, token = server
    from concurrent.futures import ThreadPoolExecutor
    def go(i):
        return rq.post(url + "/api/setup", json={"token": "KODE1234", "username": f"admin{i}",
                                                 "password": "password-kuat-1"}, headers=H).status_code
    with ThreadPoolExecutor(6) as ex:
        codes = list(ex.map(go, range(6)))
    assert codes.count(200) == 1 and ctx.db.count_users() == 1


def test_login_guard_parallel_attempts_limited(server):
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    from concurrent.futures import ThreadPoolExecutor
    def go(_):
        return rq.post(url + "/api/login", json={"username": "admin", "password": "x"}, headers=H).status_code
    with ThreadPoolExecutor(12) as ex:
        codes = list(ex.map(go, range(12)))
    assert codes.count(401) == 5 and codes.count(429) == 7


def test_negative_content_length_rejected(server):
    import socket
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    host, port = url.replace("http://", "").split(":")
    s = socket.create_connection((host, int(port)), timeout=5)
    s.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\nX-Dashboard: 1\r\nContent-Length: -1\r\n\r\n")
    assert b" 400 " in s.recv(200)
    s.close()


def test_cannot_switch_to_paper_with_open_live_positions(server):
    from bot.state import DbStateStore, Position
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    ctx.db.set_secret("indodax_api_key", "KEY-1234567890")
    ctx.db.set_secret("indodax_secret_key", "s" * 40)
    ctx.svc.update("general", {"mode": "live"})
    store = DbStateStore(ctx.db, "live")
    st = store.load()
    st.positions["pepeidr"] = Position("pepeidr", 1000, 0.16, 100000, time.time(), 0.16, "live")
    store.save(st)
    s = _login(url)
    r = s.put(url + "/api/settings/general", json={"value": {"mode": "paper"}, "password": "password-kuat-1"})
    assert r.status_code == 400 and "PEPEIDR" in r.json()["error"]
    st.positions.clear(); store.save(st)
    r = s.put(url + "/api/settings/general", json={"value": {"mode": "paper"}, "password": "password-kuat-1"})
    assert r.status_code == 200


def test_railway_helpers(tmp_path, monkeypatch):
    from bot.db import normalize_db_url
    assert normalize_db_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_db_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_db_url("postgresql+psycopg://x") == "postgresql+psycopg://x"
    assert normalize_db_url("") == ""
    (tmp_path / "config.example.yaml").write_text("pairs: [solidr]\n")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/app/data")
    boot = load_bootstrap(str(tmp_path / "config.yaml"))      # config.yaml tidak ada -> pakai contoh
    assert boot["data_dir"] == "/app/data" and boot["_raw"]["pairs"] == ["solidr"]
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    assert load_bootstrap(str(tmp_path / "config.yaml"))["data_dir"] == str(tmp_path / "d")


def test_master_key_file_fallback(tmp_path, monkeypatch):
    import os
    from bot.__main__ import load_master_key_file
    monkeypatch.delenv("BOT_MASTER_KEY", raising=False)
    load_master_key_file(str(tmp_path), create=False)
    assert not os.environ.get("BOT_MASTER_KEY")
    load_master_key_file(str(tmp_path), create=True)
    k = os.environ["BOT_MASTER_KEY"]
    assert (tmp_path / ".master_key").read_text().strip() == k
    monkeypatch.delenv("BOT_MASTER_KEY")
    load_master_key_file(str(tmp_path), create=False)          # proses bot membaca kunci yang sama
    assert os.environ["BOT_MASTER_KEY"] == k


def test_chart_endpoint_candles_trades_position(server):
    import bot.web as W
    from bot.state import DbStateStore, Position
    from .fakes import FakePublic
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    now = time.time()
    closes = [100 + i * 0.1 for i in range(200)]
    fp = FakePublic({"PEPEIDR": closes, "BTCIDR": closes}, now=now)
    # ganti sumber candle pada handler yang sedang berjalan
    import gc
    for obj in gc.get_objects():
        if isinstance(obj, W.ChartCache):
            obj.market.public = fp
            obj.cache.clear()
    store = DbStateStore(ctx.db, "paper")
    store.log_trade(pair="pepeidr", side="BUY", qty=10, price=110, idr=100000, fee_idr=300, reason="tren naik")
    store.log_trade(pair="btcidr", side="BUY", qty=1, price=105, idr=100000)
    st = store.load()
    st.positions["pepeidr"] = Position("pepeidr", 10, 110, 100000, now - 600, 111, "paper")
    store.save(st)
    store.write_status({"ts": now, "pairs": {"pepeidr": {"last": 119.5, "bid": 119.4}},
                        "levels": {"pepeidr": {"stop": 107.25, "stop_reason": "stop_loss", "take_profit": 114.4}}})
    s = _login(url)
    d = s.get(url + "/api/chart?pair=pepeidr&period=1d").json()
    assert len(d["candles"]) >= 90 and d["candles"][-1][4] == closes[-1]
    assert [t["side"] for t in d["trades"]] == ["BUY"] and d["trades"][0]["price"] == 110   # hanya transaksi pair ini
    assert d["position"]["entry"] == 110 and d["position"]["stop"] == 107.25 and d["last"] == 119.5
    assert s.get(url + "/api/chart?pair=../../etc&period=1d").status_code == 400
    assert s.get(url + "/api/chart?pair=pepeidr&period=5y").status_code == 400
    assert rq.get(url + "/api/chart?pair=pepeidr&period=1d").status_code == 401



# ---------------- penyimpanan di database ----------------
def test_db_store_roundtrip_flags_trades(tmp_path):
    from bot.state import DbStateStore, Position
    ctx = make_ctx(tmp_path)
    st_store = DbStateStore(ctx.db, "paper")
    st = st_store.load()
    assert not st.positions
    st.positions["btcidr"] = Position("btcidr", 0.001, 1.7e9, 100000, 1.0, 1.7e9, "paper")
    st.daily_pnl["2026-09-29"] = 1234.5
    st.paper_balances = {"idr": 900000}
    st_store.save(st)
    st_store.save(st)                                             # update, bukan insert ganda
    again = DbStateStore(ctx.db, "paper").load()
    assert again.positions["btcidr"].entry_price == 1.7e9 and again.daily_pnl["2026-09-29"] == 1234.5
    assert DbStateStore(ctx.db, "live").load().positions == {}   # mode terpisah
    st_store.log_trade(pair="btcidr", side="BUY", qty=0.001, price=1.7e9, idr=100000, fee_idr=300)
    st_store.log_trade(pair="btcidr", side="SELL", qty=0.001, price=1.8e9, idr=105000, pnl_idr=5000, pnl_pct=5,
                       reason="take_profit")
    rows = st_store.read_trades()
    assert [r["sisi"] for r in rows] == ["BUY", "SELL"] and rows[1]["pnl_idr"] == "5000" and rows[0]["pnl_idr"] == ""
    assert [r["sisi"] for r in st_store.read_trades(1)] == ["SELL"]
    assert not st_store.flag("PAUSE")
    st_store.set_flag("PAUSE"); assert DbStateStore(ctx.db, "live").flag("PAUSE")   # flag global
    st_store.set_flag("PAUSE", False); assert not st_store.flag("PAUSE")
    st_store.write_status({"ts": 5, "pairs": {}})
    assert st_store.read_status()["ts"] == 5


def test_import_old_files_once(tmp_path):
    from bot.state import DbStateStore, Position, StateStore
    ctx = make_ctx(tmp_path)
    old = StateStore(str(tmp_path), "paper")
    st = old.load(); st.positions["dogeidr"] = Position("dogeidr", 25, 4000, 100000, 1.0, 4000, "paper"); old.save(st)
    old.log_trade(pair="dogeidr", side="BUY", qty=25, price=4000, idr=100000)
    db_store = DbStateStore(ctx.db, "paper")
    assert set(db_store.import_files(str(tmp_path))) == {"state_paper.json", "trades_paper.csv"}
    assert "dogeidr" in db_store.load().positions and len(db_store.read_trades()) == 1
    assert db_store.import_files(str(tmp_path)) == []                # tidak diimpor dua kali
    assert (tmp_path / "state_paper.json.imported").exists()


def test_engine_with_db_store_and_db_logs(tmp_path):
    import logging
    from bot.broker import PaperBroker
    from bot.dblog import DbLogHandler
    from bot.engine import Engine
    from bot.market import Market
    from bot.notifier import Notifier
    from bot.state import DbStateStore
    from .fakes import FakePublic, STEP
    from .test_bot import Clock, cfg_for
    ctx = make_ctx(tmp_path)
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    now = (int(time.time() // STEP) + 1) * STEP + 5
    cfg = cfg_for(tmp_path, strategy={"rsi_max": 90})
    pub = FakePublic({"BTCIDR": closes}, now=now)
    store = DbStateStore(ctx.db, "paper")
    h = DbLogHandler(ctx.db, "bot", flush_every=0.1)
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(h)
    try:
        eng = Engine(cfg, Market(pub), PaperBroker(cfg, {}), store, Notifier(), clock=Clock(now))
        eng.startup()
        eng.tick()
        assert "btcidr" in DbStateStore(ctx.db, "paper").load().positions       # tersimpan di DB
        assert DbStateStore(ctx.db, "paper").read_trades()[0]["sisi"] == "BUY"
        assert DbStateStore(ctx.db, "paper").read_status()["pairs"]["btcidr"]["last"] > 0
        store.set_flag("SELLALL")
        eng.clock.t += 20
        eng.tick()
        assert not DbStateStore(ctx.db, "paper").load().positions and not store.flag("SELLALL")
        time.sleep(0.4)
    finally:
        root.removeHandler(h)
        root.setLevel(old_level)
        h.close()
    msgs = [r["message"] for r in ctx.db.tail_logs(50)]
    assert any("BELI" in m or "Bot aktif" in m for m in msgs)


def test_uptime_duration_format_and_tracking(tmp_path):
    from bot.state import DbStateStore, fmt_duration
    assert fmt_duration(10 * 86400 + 5 * 3600 + 10 * 60 + 59) == "10 hari, 5 jam, 10 menit"
    assert fmt_duration(3 * 86400 + 7 * 60) == "3 hari, 0 jam, 7 menit"
    assert fmt_duration(2 * 3600 + 60) == "2 jam, 1 menit"
    assert fmt_duration(59) == "0 menit"
    from bot.broker import PaperBroker
    from bot.engine import Engine
    from bot.market import Market
    from bot.notifier import Notifier
    from bot.web import build_summary
    from .fakes import FakePublic, STEP
    from .test_bot import Clock, cfg_for
    ctx = make_ctx(tmp_path)
    cfg = cfg_for(tmp_path)
    t0 = (int(time.time() // STEP) + 1) * STEP + 5
    pub = FakePublic({"BTCIDR": trend_series()[:150]}, now=t0)
    store = DbStateStore(ctx.db, "paper")
    eng = Engine(cfg, Market(pub), PaperBroker(cfg, {}), store, Notifier(), clock=Clock(t0))
    eng.startup(); eng.tick()
    assert store.load().first_started_at == t0
    # "restart" 3 hari kemudian: waktu mulai pertama tetap, sesi baru
    t1 = t0 + 3 * 86400 + 2 * 3600
    pub.now = t1
    eng2 = Engine(cfg, Market(pub), PaperBroker(cfg, {}), DbStateStore(ctx.db, "paper"), Notifier(), clock=Clock(t1))
    eng2.startup(); eng2.tick()
    d = build_summary(cfg, DbStateStore(ctx.db, "paper"))
    assert d["running_since"] == t0 and d["session_since"] == t1


def test_trades_api_filter(server):
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    t0 = time.time() - 3600
    for i, (pair, side, pnl) in enumerate([("btcidr", "BUY", None), ("btcidr", "SELL", 1500), ("pepeidr", "BUY", None),
                                            ("pepeidr", "SELL", -800), ("btcidr", "BUY", None), ("btcidr", "SELL", -200)]):
        ctx.db.add_trade(ts=t0 + i * 60, mode="paper", pair=pair, side=side, qty=1, price=100, idr=100000,
                         fee_idr=300, pnl_idr=pnl, pnl_pct=None if pnl is None else pnl / 1000, reason="x",
                         order_id="", meta='{"hold_s": 120, "rsi": 55, "entry_ctx_big": "x"}' if side == "SELL" else None)
    s = _login(url)
    assert rq.get(url + "/api/trades").status_code == 401
    r = s.get(url + "/api/trades").json()
    assert len(r["trades"]) == 6 and r["trades"][0]["sisi"] == "SELL" and "btcidr" in r["pairs"]
    r = s.get(url + "/api/trades", params={"pair": "btcidr"}).json()
    assert {t["pair"] for t in r["trades"]} == {"btcidr"} and len(r["trades"]) == 4
    sm = r["summary"]
    assert sm["buys"] == 2 and sm["sells"] == 2 and sm["pnl"] == 1300 and sm["wins"] == 1 and sm["avg_hold_s"] == 120
    assert r["trades"][0]["meta"] == {"hold_s": 120, "rsi": 55}
    r = s.get(url + "/api/trades", params={"pair": "btcidr", "side": "SELL"}).json()
    assert [t["sisi"] for t in r["trades"]] == ["SELL", "SELL"] and r["summary"]["buys"] == 2
    r = s.get(url + "/api/trades", params={"side": "BUY", "limit": 2}).json()
    assert len(r["trades"]) == 2 and r["total_rows"] == 3
    assert s.get(url + "/api/trades", params={"pair": "x'; drop"}).status_code == 400
    assert s.get(url + "/api/trades", params={"side": "HOLD"}).status_code == 400


def test_file_store_trade_filter(tmp_path):
    from bot.state import StateStore
    st = StateStore(str(tmp_path), "paper")
    st.log_trade(pair="btcidr", side="BUY", qty=1, price=1, idr=100)
    st.log_trade(pair="ethidr", side="BUY", qty=1, price=1, idr=100)
    st.log_trade(pair="btcidr", side="SELL", qty=1, price=1, idr=100, pnl_idr=5, pnl_pct=5)
    assert len(st.read_trades(pair="btcidr")) == 2
    assert [t["pair"] for t in st.read_trades(side="BUY")] == ["btcidr", "ethidr"]
