import copy
import hashlib
import hmac
import time
import urllib.parse
from unittest import mock

import pytest

from bot.backtest import backtest, sweep
from bot.broker import LiveBroker, PaperBroker, fmt_qty, make_client_order_id
from bot.client import PrivateClientV2
from bot.config import DEFAULTS, ConfigError, _merge, validate
from bot.engine import Engine
from bot.indicators import ema, rsi
from bot.market import Market
from bot.notifier import Notifier
from bot.state import StateStore
from bot.strategy import ExitRules, TrendStrategy

from .fakes import STEP, FakePrivate, FakePublic, trend_series


def cfg_for(tmp_path, **over):
    c = copy.deepcopy(DEFAULTS)
    c["pairs"] = ["btcidr"]
    c["data_dir"] = str(tmp_path)
    c["risk"]["min_24h_volume_idr"] = 0
    for k, v in over.items():
        c[k] = _merge(c[k], v) if isinstance(v, dict) else v
    validate(c)
    return c


# ---------------- indikator ----------------
def test_ema_matches_manual():
    vals = [1, 2, 3, 4, 5, 6]
    e = ema(vals, 3)
    assert e[:2] == [None, None]
    assert e[2] == pytest.approx(2.0)
    assert e[3] == pytest.approx(3.0)   # 4*0.5 + 2*0.5
    assert e[5] == pytest.approx(5.0)


def test_rsi_extremes():
    assert rsi(list(range(1, 40)), 14)[-1] == 100.0
    assert rsi(list(range(40, 1, -1)), 14)[-1] == 0.0


# ---------------- strategi ----------------
def test_strategy_buys_in_uptrend_and_not_in_downtrend(tmp_path):
    cfg = cfg_for(tmp_path, strategy={"rsi_max": 90})
    s = TrendStrategy(cfg)
    closes = trend_series()
    ind = s.indicators(closes)
    buys = [i for i in range(len(closes)) if s.evaluate(closes, False, ind, i).action == "buy"]
    assert buys, "harus ada sinyal beli saat tren naik"
    assert all(i >= 250 for i in buys), "tidak boleh beli di fase turun"


def test_exit_rules_levels():
    cfg = copy.deepcopy(DEFAULTS)
    r = ExitRules(cfg)  # TP 4, SL 2.5, trail 1.5 aktif di +2
    stop, why, tp = r.levels(100, 100)
    assert stop == pytest.approx(97.5) and why == "stop_loss" and tp == pytest.approx(104)
    stop, why, _ = r.levels(100, 103)       # sudah +3% -> trailing aktif
    assert why == "trailing_stop" and stop == pytest.approx(103 * 0.985)
    now = time.time()
    assert r.check(100, 100, 97.4, now, now) == "stop_loss"
    assert r.check(100, 100, 104.1, now, now) == "take_profit"
    assert r.check(100, 103, 101.4, now, now) == "trailing_stop"
    assert r.check(100, 100, 100.5, now - 73 * 3600, now) == "max_hold_time"
    assert r.check(100, 100, 100.5, now, now) is None


def test_config_validation_rejects_no_stop_loss(tmp_path):
    c = copy.deepcopy(DEFAULTS)
    c["exits"]["stop_loss_pct"] = 0
    with pytest.raises(ConfigError):
        validate(c)


# ---------------- klien & tanda tangan ----------------
def test_v2_signature_and_request():
    public = mock.Mock()
    api = PrivateClientV2("KEY", "SECRET", public)
    sess = mock.Mock()
    resp = mock.Mock(status_code=200)
    resp.json.return_value = {"symbol": "BTCIDR", "orderId": 1}
    sess.post.return_value = resp
    api.s = sess
    api.create_order("BTCIDR", "BUY", "MARKET", quote_order_qty=50000, client_order_id="x1")
    _, kw = sess.post.call_args
    body = kw["data"]
    params = dict(urllib.parse.parse_qsl(body))
    assert params["symbol"] == "BTCIDR" and params["quoteOrderQty"] == "50000"
    assert "quantity" not in params and "timestamp" in params
    assert kw["headers"]["X-APIKEY"] == "KEY"
    assert kw["headers"]["Sign"] == hmac.new(b"SECRET", body.encode(), hashlib.sha256).hexdigest()


def test_v2_error_is_raised():
    api = PrivateClientV2("KEY", "SECRET", mock.Mock())
    resp = mock.Mock(status_code=403)
    resp.json.return_value = {"code": -2015, "msg": "Access denied"}
    api.s = mock.Mock(request=mock.Mock(return_value=resp))
    from bot.client import IndodaxError
    with pytest.raises(IndodaxError) as ei:
        api.account()
    assert ei.value.code == -2015


def test_fmt_qty_and_client_id():
    assert fmt_qty(0.000012345678999) == "0.00001234"
    assert fmt_qty(1e-5) == "0.00001"
    assert fmt_qty(123456789.0) == "123456789"
    cid = make_client_order_id("pepeidr", "BUY")
    assert len(cid) <= 36 and cid.startswith("bot-pepeidr-b-")


# ---------------- broker live (dengan API palsu) ----------------
def test_live_broker_buy_sell_roundtrip(tmp_path):
    cfg = cfg_for(tmp_path)
    api = FakePrivate(price=100.0)
    pub = FakePublic({"BTCIDR": [100.0] * 5}, now=time.time())
    m = Market(pub)
    info = m.info("btcidr")
    q = m.quotes()["btcidr"]
    b = LiveBroker(cfg, api)
    f = b.buy(info, 100000, q)
    assert api.calls[0] == ("BUY", "MARKET", None, 100000)
    assert f.net_idr == pytest.approx(100000) and f.fee_idr == pytest.approx(300)
    assert f.qty == pytest.approx(997.0)
    s = b.sell(info, f.qty, q)
    assert s.qty == pytest.approx(997.0, rel=1e-6)
    assert s.net_idr == pytest.approx(99700 * (1 - 0.0051), rel=1e-6)


def _live_setup(tmp_path):
    cfg = cfg_for(tmp_path)
    api = FakePrivate(price=100.0)
    pub = FakePublic({"BTCIDR": [100.0] * 5}, now=time.time())
    m = Market(pub)
    return cfg, api, m.info("btcidr"), m.quotes()["btcidr"]


def test_live_broker_recovers_order_after_timeout_or_5xx(tmp_path):
    import requests
    from bot.client import IndodaxError
    cfg, api, info, q = _live_setup(tmp_path)
    real = api.create_order
    for exc in (requests.Timeout("timeout"), IndodaxError("<html>502</html>", status=502)):
        def flaky(*a, _exc=exc, **kw):
            real(*a, **kw)          # order sebenarnya tereksekusi di bursa...
            raise _exc              # ...tapi respon hilang
        api.create_order = flaky
        with mock.patch("bot.broker.time.sleep"):
            f = LiveBroker(cfg, api).buy(info, 100000, q)
        assert f.qty == pytest.approx(997.0) and not f.estimated


def test_live_broker_order_not_sent_raises_order_failed(tmp_path):
    import requests
    from bot.broker import OrderFailed
    from bot.client import IndodaxError
    cfg, api, info, q = _live_setup(tmp_path)

    def boom(*a, **kw):
        raise requests.ConnectionError("down")

    def not_found(*a, **kw):
        raise IndodaxError("Order not found", code=-2013, status=400)
    api.create_order, api.get_order = boom, not_found
    with mock.patch("bot.broker.time.sleep"), pytest.raises(OrderFailed, match="tidak terkirim"):
        LiveBroker(cfg, api).buy(info, 100000, q)
    # error eksplisit dari API tidak dicek ulang, langsung diteruskan
    api.create_order = lambda *a, **kw: (_ for _ in ()).throw(
        IndodaxError("Insufficient balance", code=-4026, status=400))
    with pytest.raises(IndodaxError):
        LiveBroker(cfg, api).buy(info, 100000, q)


# ---------------- engine end-to-end (paper) ----------------
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def build_engine(tmp_path, closes, broker=None, **cfg_over):
    now = (int(time.time() // STEP) + 1) * STEP + 5
    cfg = cfg_for(tmp_path, strategy={"rsi_max": 90}, **cfg_over)
    pub = FakePublic({"BTCIDR": closes}, now=now)
    market = Market(pub)
    broker = broker or PaperBroker(cfg, {})
    clock = Clock(now)
    eng = Engine(cfg, market, broker, StateStore(cfg["data_dir"], broker.mode), Notifier(), clock=clock)
    return eng, pub, clock


def find_buy_index(tmp_path):
    cfg = cfg_for(tmp_path, strategy={"rsi_max": 90})
    s = TrendStrategy(cfg)
    closes = trend_series()
    ind = s.indicators(closes)
    return next(i for i in range(len(closes)) if s.evaluate(closes, False, ind, i).action == "buy")


def test_engine_buys_then_takes_profit(tmp_path):
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    eng, pub, clock = build_engine(tmp_path, closes)
    eng.startup()
    eng.tick()
    assert "btcidr" in eng.state.positions
    pos = eng.state.positions["btcidr"]
    assert eng.broker.bal["idr"] == pytest.approx(900000)
    # harga naik 5% -> take profit (4%)
    pub.last_override["BTCIDR"] = pos.entry_price * 1.05
    clock.t += 20
    eng.tick()
    assert "btcidr" not in eng.state.positions
    assert eng.state.total_realized > 0 and eng.state.wins == 1
    assert (tmp_path / "trades_paper.csv").read_text().count("\n") == 3  # header + beli + jual


def test_engine_stop_loss_cooldown_and_daily_halt(tmp_path):
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    eng, pub, clock = build_engine(tmp_path, closes, risk={"daily_loss_limit_idr": 1000})
    eng.startup()
    eng.tick()
    pos = eng.state.positions["btcidr"]
    pub.last_override["BTCIDR"] = pos.entry_price * 0.96   # -4% -> stop loss 2.5%
    clock.t += 20
    eng.tick()
    assert "btcidr" not in eng.state.positions
    assert eng.state.total_realized < 0
    assert eng.state.cooldown_until["btcidr"] > clock.t
    assert eng.state.halted_day != ""
    assert eng._entry_block_reason("btcidr", eng.market.quotes()["btcidr"]) == "batas rugi harian tercapai"


def test_engine_respects_pause_and_sellall(tmp_path):
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    eng, pub, clock = build_engine(tmp_path, closes)
    (tmp_path / "PAUSE").touch()
    eng.startup()
    eng.tick()
    assert not eng.state.positions
    (tmp_path / "PAUSE").unlink()
    eng.state.last_candle.clear()
    eng._candle_try.clear()
    eng.tick()
    assert eng.state.positions
    (tmp_path / "SELLALL").touch()
    clock.t += 20
    eng.tick()
    assert not eng.state.positions and not (tmp_path / "SELLALL").exists()


def test_engine_state_persists_across_restart(tmp_path):
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    eng, pub, clock = build_engine(tmp_path, closes)
    eng.startup()
    eng.tick()
    assert eng.state.positions
    eng2, _, _ = build_engine(tmp_path, closes)
    assert "btcidr" in eng2.state.positions
    assert eng2.broker.bal["idr"] == pytest.approx(900000)


def test_engine_live_with_fake_api(tmp_path):
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    api = FakePrivate(price=closes[-1])
    cfg = cfg_for(tmp_path)
    eng, pub, clock = build_engine(tmp_path, closes, broker=LiveBroker(cfg, api))
    eng.startup()
    eng.tick()
    pos = eng.state.positions["btcidr"]
    assert pos.qty == pytest.approx(api.bal["btc"])
    api.price = pos.entry_price * 0.95
    pub.last_override["BTCIDR"] = api.price
    clock.t += 20
    eng.tick()
    assert not eng.state.positions
    assert api.bal["btc"] == pytest.approx(0, abs=1e-8)


def test_reconcile_drops_position_sold_manually(tmp_path):
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    api = FakePrivate(price=closes[-1])
    cfg = cfg_for(tmp_path)
    eng, pub, clock = build_engine(tmp_path, closes, broker=LiveBroker(cfg, api))
    eng.startup()
    eng.tick()
    assert eng.state.positions
    api.bal["btc"] = 0.0      # pengguna menjual manual
    eng2, _, _ = build_engine(tmp_path, closes, broker=LiveBroker(cfg, api))
    eng2.startup()
    assert not eng2.state.positions


# ---------------- backtest ----------------
def _candles(closes):
    from bot.market import Candle
    out, prev = [], closes[0]
    for k, c in enumerate(closes):
        out.append(Candle(k * STEP, prev, max(prev, c) * 1.001, min(prev, c) * 0.999, c, 1))
        prev = c
    return out


def test_backtest_runs_and_accounts_fees(tmp_path):
    cfg = cfg_for(tmp_path, strategy={"rsi_max": 90})
    r = backtest(cfg, "btcidr", _candles(trend_series(600)))
    assert r.n >= 1
    flat = copy.deepcopy(cfg)
    flat["exits"].update(take_profit_pct=0.0001)
    r2 = backtest(flat, "btcidr", _candles(trend_series(600)))
    # TP sangat kecil -> tiap trade rugi karena fee
    assert all(t.pnl_idr < 0 for t in r2.trades)


def test_sweep_returns_rows(tmp_path):
    cfg = cfg_for(tmp_path, strategy={"rsi_max": 90})
    rows = sweep(cfg, {"btcidr": _candles(trend_series(600))}, top=3)
    assert len(rows) == 3


def test_no_immediate_rebuy_after_exit(tmp_path):
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    eng, pub, clock = build_engine(tmp_path, closes)
    eng.startup()
    eng.tick()
    pos = eng.state.positions["btcidr"]
    pub.last_override["BTCIDR"] = pos.entry_price * 1.05
    clock.t += 20
    eng.tick()
    assert not eng.state.positions
    reason = eng._entry_block_reason("btcidr", eng.market.quotes()["btcidr"])
    assert reason and reason.startswith("cooldown")


def test_run_loop_stops_cleanly(tmp_path):
    closes = trend_series()[:200]
    eng, pub, clock = build_engine(tmp_path, closes)
    calls = {"n": 0}

    def fake_sleep(s):
        calls["n"] += 1
        if calls["n"] > 3:
            eng.stop()
    eng.sleep = fake_sleep
    eng.run()
    assert (tmp_path / "state_paper.json").exists()


def test_zero_bid_does_not_mark_position_dust(tmp_path):
    i = find_buy_index(tmp_path)
    eng, pub, clock = build_engine(tmp_path, trend_series()[: i + 1])
    eng.startup()
    eng.tick()
    assert "btcidr" in eng.state.positions
    from bot.market import Quote
    assert eng._close("btcidr", Quote(1.0, 0.0, 1.0, 0, 0), "stop_loss") is False
    assert not eng.state.positions["btcidr"].dust


def _live_broker(tmp_path, price=100.0):
    cfg = cfg_for(tmp_path)
    api = FakePrivate(price=price)
    pub = FakePublic({"BTCIDR": [price] * 5}, now=time.time())
    m = Market(pub)
    return LiveBroker(cfg, api), api, m.info("btcidr"), m.quotes()["btcidr"]


def test_buy_fallback_uses_balance_delta(tmp_path, monkeypatch):
    b, api, info, q = _live_broker(tmp_path)
    monkeypatch.setattr("bot.broker.time.sleep", lambda s: None)
    api.my_trades = lambda *a, **k: {"data": []}          # riwayat trade belum muncul
    f = b.buy(info, 100000, q)
    assert f.estimated and f.qty == pytest.approx(api.bal["btc"])


def test_sell_retries_with_fewer_decimals(tmp_path, monkeypatch):
    from bot.client import IndodaxError
    b, api, info, q = _live_broker(tmp_path)
    monkeypatch.setattr("bot.broker.time.sleep", lambda s: None)
    api.bal["btc"] = 12.34567891
    orig = api.create_order
    seen = []

    def picky(symbol, side, order_type, quantity=None, **kw):
        seen.append(quantity)
        if side == "SELL" and "." in str(quantity) and len(str(quantity).split(".")[1]) > 4:
            raise IndodaxError("Quantity validation failed.", code=-1111, status=400)
        return orig(symbol, side, order_type, quantity=quantity, **kw)
    api.create_order = picky
    f = b.sell(info, 12.34567891, q)
    assert seen[:3] == ["12.34567891", "12.345678", "12.3456"]
    assert f.qty == pytest.approx(12.3456)


def test_dust_position_not_counted(tmp_path):
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    eng, pub, clock = build_engine(tmp_path, closes, risk={"max_open_positions": 1})
    eng.startup()
    eng.tick()
    eng.state.positions["btcidr"].dust = True
    eng.state.cooldown_until.clear()
    q = eng.market.quotes()["btcidr"]
    assert eng._entry_block_reason("btcidr", q) is None


# ---------------- dashboard web ----------------
def _serve(cfg, password="rahasia-1234"):
    import threading
    from http.server import ThreadingHTTPServer
    from bot.web import make_handler
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, "admin", password))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_dashboard_summary_auth_and_controls(tmp_path):
    import requests as rq
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    eng, pub, clock = build_engine(tmp_path, closes)
    eng.startup()
    eng.tick()                                   # beli -> ada posisi + status_paper.json
    pub.last_override["BTCIDR"] = eng.state.positions["btcidr"].entry_price * 1.05
    clock.t += 20
    eng.tick()                                   # take profit -> ada transaksi jual
    eng.startup()
    eng.state.cooldown_until.clear()
    eng.state.last_candle.clear(); eng._candle_try.clear()
    eng.tick()                                   # beli lagi -> posisi terbuka
    assert (tmp_path / "status_paper.json").exists()
    httpd, url = _serve(eng.cfg)
    try:
        assert rq.get(url + "/api/summary").status_code == 401
        assert rq.get(url + "/api/summary", auth=("admin", "salah")).status_code == 401
        r = rq.get(url + "/api/summary", auth=("admin", "rahasia-1234"))
        d = r.json()
        assert d["mode"] == "paper" and d["alive"] in ("good", "warning", "critical")
        assert d["trades_count"] == 1 and d["pnl_total"] > 0
        assert d["positions"][0]["pair"] == "btcidr" and d["positions"][0]["stop"] > 0
        assert d["signals"][0]["action"] in ("buy", "hold", "exit")
        assert "<title>" in rq.get(url + "/", auth=("admin", "rahasia-1234")).text
        # CSRF: POST tanpa header khusus ditolak
        assert rq.post(url + "/api/pause", auth=("admin", "rahasia-1234")).status_code == 403
        h = {"X-Dashboard": "1"}
        assert rq.post(url + "/api/pause", auth=("admin", "rahasia-1234"), headers=h).json()["ok"]
        assert (tmp_path / "PAUSE").exists()
        rq.post(url + "/api/resume", auth=("admin", "rahasia-1234"), headers=h)
        assert not (tmp_path / "PAUSE").exists()
        rq.post(url + "/api/sellall", auth=("admin", "rahasia-1234"), headers=h)
        assert (tmp_path / "SELLALL").exists() and (tmp_path / "PAUSE").exists()
    finally:
        httpd.shutdown()


def test_dashboard_refuses_public_without_password(tmp_path, monkeypatch):
    from bot.web import serve
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    cfg = cfg_for(tmp_path)
    with pytest.raises(SystemExit):
        serve(cfg, host="0.0.0.0", port=0)


def test_dashboard_controls_can_be_disabled(tmp_path):
    import requests as rq
    cfg = cfg_for(tmp_path, dashboard={"allow_control": False})
    httpd, url = _serve(cfg, password="")
    try:
        r = rq.post(url + "/api/sellall", headers={"X-Dashboard": "1"})
        assert r.status_code == 403 and not (tmp_path / "SELLALL").exists()
        assert rq.get(url + "/api/summary").json()["alive"] == "unknown"
    finally:
        httpd.shutdown()
