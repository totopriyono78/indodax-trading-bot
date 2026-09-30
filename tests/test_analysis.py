"""Tes pencatatan transaksi lengkap & analisis."""
import copy
import random
import time

import pytest

from bot.analysis import build_analysis, round_trips, stats
from bot.config import DEFAULTS
from bot.settings import pair_cfg

from .test_web_db import _login, make_ctx, server  # noqa: F401  (fixture)
from .fakes import trend_series
from .test_bot import build_engine, find_buy_index


def _trade(ts, pair, side, pnl=None, pct=None, reason="", meta=None, fee=300):
    return {"ts": ts, "pair": pair, "sisi": side, "pnl_idr": "" if pnl is None else f"{pnl:.0f}",
            "pnl_pct": "" if pct is None else f"{pct:.2f}", "alasan": reason, "fee_idr": str(fee),
            "idr": "100000", "meta": meta or {}}


def synthetic(n=60, seed=1):
    r = random.Random(seed)
    out, t = [], 1_790_000_000
    for i in range(n):
        pair = r.choice(["btcidr", "pepeidr"])
        rsi = r.uniform(50, 72)
        good = pair == "btcidr" and rsi < 65
        mfe = r.uniform(2, 5) if good else r.uniform(1.6, 3.0)   # posisi rugi sempat untung
        mae = -r.uniform(0.2, 0.8) if good else -r.uniform(2.5, 3)
        pct = r.uniform(2, 4) if good else -r.uniform(2.5, 3.2)
        out.append(_trade(t, pair, "BUY", meta={"rsi": rsi, "sl_pct": 2.5, "tp_pct": 4, "trail_pct": 1.5,
                                                 "trail_act_pct": 2}))
        t += 3600 * r.uniform(1, 10)
        out.append(_trade(t, pair, "SELL", pnl=pct * 1000, pct=pct, reason="take_profit" if good else "stop_loss",
                          meta={"mfe_pct": mfe, "mae_pct": mae, "hold_s": 7200, "entry_time": t - 7200,
                                "cost_idr": 100000, "rsi": rsi, "sl_pct": 2.5, "tp_pct": 4, "trail_pct": 1.5,
                                "trail_act_pct": 2}))
        t += 3600
    return out


def test_round_trips_and_stats():
    rows = round_trips(synthetic(20))
    assert len(rows) == 20 and all(r["rich"] for r in rows)
    s = stats(rows)
    assert s["n"] == 20 and 0 <= s["win_rate"] <= 100 and s["fees"] == 20 * 600
    assert round(s["pnl"], 6) == round(sum(r["pnl"] for r in rows), 6)


def test_analysis_groups_and_suggestions():
    cfg = copy.deepcopy(DEFAULTS)
    d = build_analysis(synthetic(80), cfg, lambda p: pair_cfg(cfg, p))
    assert d["summary"]["n"] == 80
    assert {g["key"] for g in d["by_pair"]} == {"btcidr", "pepeidr"}
    assert sum(g["n"] for g in d["by_rsi"]) == 80
    titles = " | ".join(s["title"] for s in d["suggestions"])
    assert "PEPEIDR konsisten rugi" in titles
    assert "Banyak posisi rugi yang sempat untung" in titles
    assert "Stop loss mungkin bisa diperketat" in titles
    assert all(s["n"] > 0 for s in d["suggestions"])
    assert len(d["scatter"]) == 80 and len(d["distribution"]) == 80


def test_analysis_small_sample_message():
    cfg = copy.deepcopy(DEFAULTS)
    d = build_analysis(synthetic(3), cfg, lambda p: pair_cfg(cfg, p))
    assert d["suggestions"][0]["title"].startswith("Data belum cukup")


def test_compare_before_after_change():
    cfg = copy.deepcopy(DEFAULTS)
    trades = synthetic(40)
    mid = trades[40]["ts"]
    d = build_analysis(trades, cfg, lambda p: pair_cfg(cfg, p), last_change_ts=mid)
    c = d["compare"]
    assert c["before"]["n"] + c["after"]["n"] == 40 and c["before"]["n"] > 0 and c["after"]["n"] > 0


def test_engine_records_entry_context_mfe_mae(tmp_path):
    i = find_buy_index(tmp_path)
    closes = trend_series()[: i + 1]
    eng, pub, clock = build_engine(tmp_path, closes)
    eng.startup()
    eng.tick()
    pos = eng.state.positions["btcidr"]
    assert pos.entry_ctx["sl_pct"] == 2.5 and pos.entry_ctx["rsi"] is not None
    assert "ema_trend_dist_pct" in pos.entry_ctx and pos.entry_ctx["spread_pct"] >= 0
    e = pos.entry_price
    for f in (1.02, 0.985, 1.03):                    # naik, turun, naik lagi
        pub.last_override["BTCIDR"] = e * f
        clock.t += 20
        eng.tick()
    pub.last_override["BTCIDR"] = e * 1.06           # take profit
    clock.t += 20
    eng.tick()
    sell = [t for t in eng.store.read_trades() if t["sisi"] == "SELL"]
    assert sell  # file store tidak menyimpan meta; cek lewat DB store di tes berikut


def test_db_trade_meta_and_migration(tmp_path):
    from sqlalchemy import create_engine
    from bot.db import Database, generate_master_key
    from bot.state import DbStateStore
    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url)
    with eng.begin() as c:                                        # skema lama tanpa kolom meta
        c.exec_driver_sql("CREATE TABLE trades (id INTEGER PRIMARY KEY, ts FLOAT NOT NULL, mode VARCHAR(16) NOT NULL,"
                          " pair VARCHAR(32) NOT NULL, side VARCHAR(8) NOT NULL, qty FLOAT NOT NULL, price FLOAT NOT NULL,"
                          " idr FLOAT NOT NULL, fee_idr FLOAT NOT NULL, pnl_idr FLOAT, pnl_pct FLOAT,"
                          " reason VARCHAR(255) NOT NULL, order_id VARCHAR(128) NOT NULL)")
        c.exec_driver_sql("INSERT INTO trades (ts, mode, pair, side, qty, price, idr, fee_idr, reason, order_id) "
                          "VALUES (1, 'paper', 'btcidr', 'BUY', 1, 1, 1, 0, '', '')")
    db = Database(url, generate_master_key())
    db.create_all()                                               # menambah kolom meta
    st = DbStateStore(db, "paper")
    st.log_trade(pair="btcidr", side="SELL", qty=1, price=2, idr=2, pnl_idr=1, pnl_pct=100, reason="take_profit",
                 meta={"mfe_pct": 3.2, "mae_pct": -0.4})
    rows = st.read_trades()
    assert rows[0]["meta"] == {} and rows[1]["meta"]["mfe_pct"] == 3.2


def test_engine_db_store_sell_meta(tmp_path):
    from bot.broker import PaperBroker
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
    eng = Engine(cfg, Market(pub), PaperBroker(cfg, {}), store, Notifier(), clock=Clock(now))
    eng.startup(); eng.tick()
    e = store.load().positions["btcidr"].entry_price
    for f in (1.025, 0.99, 1.05):
        pub.last_override["BTCIDR"] = e * f
        eng.clock.t += 20
        eng.tick()
    rows = store.read_trades()
    sell = [r for r in rows if r["sisi"] == "SELL"][0]
    m = sell["meta"]
    assert m["mfe_pct"] >= 2.4 and m["mae_pct"] <= -0.9 and m["hold_s"] > 0 and m["rsi"] is not None
    assert m["sl_pct"] == 2.5 and rows[0]["meta"]["rsi"] is not None
    d = build_analysis(rows, cfg, lambda p: pair_cfg(cfg, p))
    assert d["summary"]["n"] == 1 and d["n_rich"] == 1


def test_analysis_api(server):  # noqa: F811
    from bot.state import DbStateStore
    import requests as rq
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    st = DbStateStore(ctx.db, "paper")
    for t in synthetic(30):
        st.log_trade(pair=t["pair"], side=t["sisi"], qty=1, price=1, idr=100000, fee_idr=300,
                     pnl_idr=float(t["pnl_idr"]) if t["pnl_idr"] else None,
                     pnl_pct=float(t["pnl_pct"]) if t["pnl_pct"] else None, reason=t["alasan"], meta=t["meta"])
    s = _login(url)
    d = s.get(url + "/api/analysis?period=all").json()
    assert d["summary"]["n"] == 30 and d["mode"] == "paper" and d["suggestions"]
    assert s.get(url + "/api/analysis?mode=hack").status_code == 400
    assert s.get(url + "/analysis").status_code == 200
    assert rq.get(url + "/api/analysis").status_code == 401
