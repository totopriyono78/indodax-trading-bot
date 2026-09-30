"""Uji modul optimizer (evaluasi & rekonfigurasi strategi di background)."""
import math
import random
import time
from types import SimpleNamespace

import pytest
import requests as rq

import bot.optimizer as om
from bot.market import Market
from bot.optimizer import BOUNDS, Optimizer, OptimizerThread
from bot.settings import pair_cfg
from bot.state import DbStateStore

from .fakes import FakePublic
from .test_web_db import _login, make_ctx, server  # noqa: F401  (fixture)

NOW = time.time()


def series(n, seed=1):
    rnd = random.Random(seed)
    out, p = [], 1000.0
    for i in range(n):
        p *= 1 + 0.0012 * math.sin(i / 40) + rnd.gauss(0, 0.004)
        out.append(p)
    return out


def add_trips(db, n, pair="btcidr", mode="paper", start=NOW - 5 * 86400, pnl=(800, -500), meta=None):
    for i in range(n):
        v = pnl[i % len(pnl)]
        db.add_trade(ts=start + i * 600, mode=mode, pair=pair, side="SELL", qty=1, price=1, idr=100000, fee_idr=0,
                     pnl_idr=v, pnl_pct=v / 1000, reason="x", order_id="", meta=meta)


def make_opt(ctx, clock=lambda: NOW, sent=None):
    pub = FakePublic({"BTCIDR": series(30 * 96 + 50, 1), "PEPEIDR": series(30 * 96 + 50, 2)}, NOW)
    return Optimizer(ctx.db, ctx.svc, Market(pub), lambda m: DbStateStore(ctx.db, m),
                     (sent.append if sent is not None else None), clock)


def fake_backtest(best):
    """Skor sintetis: puncak di `best` (dict parameter). Makin dekat, makin untung."""
    def bt(cfg, pair, candles):
        e = pair_cfg(cfg, pair)["exits"]
        dist = sum(abs(e[k] - v) for k, v in best.items())
        total = (10000 - 4000 * dist) * len(candles) / 2000
        return SimpleNamespace(total=total, n=10, win_rate=50.0)
    return bt


def set_pairs(ctx, **vals):
    rows = ctx.svc.sections()["pairs"]
    for r in rows:
        r.update(vals)
    ctx.svc.update("pairs", rows)


@pytest.fixture
def octx(tmp_path):
    ctx = make_ctx(tmp_path)
    set_pairs(ctx, stop_loss_pct=2.0, take_profit_pct=4.0, trailing_stop_pct=1.5, trailing_activation_pct=2.0)
    return ctx


def test_skips_without_enough_trades(octx):
    add_trips(octx.db, 5)
    run = make_opt(octx).run_once("manual")
    assert run["status"] == "skipped" and "belum cukup" in run["data"]["summary"]


def test_random_market_makes_no_change(octx):
    """Data acak: tidak ada kandidat yang konsisten lebih baik -> tidak mengubah apa pun."""
    add_trips(octx.db, 25)
    before = octx.svc.sections()["pairs"]
    run = make_opt(octx).run_once("manual")
    assert run["status"] in ("no_change", "applied")
    if run["status"] == "no_change":
        assert octx.svc.sections()["pairs"] == before


def test_auto_apply_in_paper_with_step_clamp(octx, monkeypatch):
    monkeypatch.setattr(om, "backtest", fake_backtest({"stop_loss_pct": 3.0, "take_profit_pct": 6.0}))
    add_trips(octx.db, 25)
    sent = []
    run = make_opt(octx, sent=sent).run_once("manual")
    assert run["status"] == "applied" and run["data"]["auto"]
    cfg = octx.svc.load()
    e = pair_cfg(cfg, "btcidr")["exits"]
    # maks. 25% per langkah: 2.0 -> 2.5, 4.0 -> 5.0
    assert e["stop_loss_pct"] == 2.5 and e["take_profit_pct"] == 5.0
    assert e["trailing_stop_pct"] == 1.5
    assert sent and "menerapkan" in sent[0]
    assert any(a["action"] == "optimizer_apply" for a in octx.db.recent_audit(10))
    # putaran berikutnya menunggu evaluasi perubahan ini
    run2 = make_opt(octx).run_once("manual")
    assert run2["status"] == "skipped" and "Menunggu evaluasi" in run2["data"]["summary"]


def test_live_only_suggests_then_manual_apply(octx, monkeypatch):
    monkeypatch.setattr(om, "backtest", fake_backtest({"stop_loss_pct": 3.0}))
    octx.db.put_settings({"general": {**octx.svc.sections()["general"], "mode": "live"}}, user="t")
    add_trips(octx.db, 25, mode="live")
    opt = make_opt(octx)
    run = opt.run_once("manual")
    assert run["status"] == "proposed" and run["trading_mode"] == "live"
    assert pair_cfg(octx.svc.load(), "btcidr")["exits"]["stop_loss_pct"] == 2.0   # belum berubah
    # pengaturan diubah manual setelah usulan -> apply ditolak agar tidak menimpa
    set_pairs(octx, stop_loss_pct=2.2)
    with pytest.raises(ValueError, match="sudah diubah"):
        opt.apply_run(run["id"], "admin")
    set_pairs(octx, stop_loss_pct=2.0)
    r = opt.apply_run(run["id"], "admin")
    assert r["status"] == "applied" and pair_cfg(octx.svc.load(), "btcidr")["exits"]["stop_loss_pct"] == 2.5


def test_bounds_respected(octx, monkeypatch):
    set_pairs(octx, stop_loss_pct=0.9)
    monkeypatch.setattr(om, "backtest", fake_backtest({"stop_loss_pct": 0.1}))
    add_trips(octx.db, 25)
    make_opt(octx).run_once("manual")
    assert pair_cfg(octx.svc.load(), "btcidr")["exits"]["stop_loss_pct"] >= BOUNDS["stop_loss_pct"][0]


def test_disabled_feature_stays_disabled(octx, monkeypatch):
    set_pairs(octx, take_profit_pct=0)
    monkeypatch.setattr(om, "backtest", fake_backtest({"take_profit_pct": 5.0, "stop_loss_pct": 3.0}))
    add_trips(octx.db, 25)
    make_opt(octx).run_once("manual")
    assert pair_cfg(octx.svc.load(), "btcidr")["exits"]["take_profit_pct"] == 0


def test_rollback_when_results_worsen(octx, monkeypatch):
    monkeypatch.setattr(om, "backtest", fake_backtest({"stop_loss_pct": 3.0}))
    add_trips(octx.db, 25, pnl=(800, 200))            # sebelum: untung
    t = [NOW]
    sent = []
    opt = make_opt(octx, clock=lambda: t[0], sent=sent)
    run = opt.run_once("manual")
    assert run["status"] == "applied"
    # setelah perubahan: 15 transaksi rugi di pair yang diubah
    add_trips(octx.db, 15, start=NOW + 3600, pnl=(-900, -400))
    t[0] = NOW + 2 * 86400
    opt.run_once("schedule")
    old = octx.db.get_opt_run(run["id"])
    assert old["status"] == "rolled_back" and old["data"]["evaluation"]["result"] == "rolled_back"
    e = pair_cfg(octx.svc.load(), "btcidr")["exits"]
    assert e["stop_loss_pct"] == 2.0
    assert any("dibatalkan" in s for s in sent)
    # putaran berikutnya tidak mengulang nilai yang sudah terbukti memburuk
    t[0] += 86400
    opt.run_once("schedule")
    assert pair_cfg(octx.svc.load(), "btcidr")["exits"]["stop_loss_pct"] != 2.5


def test_keep_change_when_results_ok(octx, monkeypatch):
    monkeypatch.setattr(om, "backtest", fake_backtest({"stop_loss_pct": 3.0}))
    add_trips(octx.db, 25, pnl=(800, -500))
    t = [NOW]
    opt = make_opt(octx, clock=lambda: t[0])
    run = opt.run_once("manual")
    add_trips(octx.db, 15, start=NOW + 3600, pnl=(900, 300))
    t[0] = NOW + 2 * 86400
    opt.run_once("schedule")
    assert octx.db.get_opt_run(run["id"])["data"]["evaluation"]["result"] == "ok"
    assert pair_cfg(octx.svc.load(), "btcidr")["exits"]["stop_loss_pct"] >= 2.5


def test_manual_rollback_keeps_user_edits(octx, monkeypatch):
    monkeypatch.setattr(om, "backtest", fake_backtest({"stop_loss_pct": 3.0, "take_profit_pct": 6.0}))
    add_trips(octx.db, 25)
    opt = make_opt(octx)
    run = opt.run_once("manual")
    rows = octx.svc.sections()["pairs"]
    for r in rows:
        if r["pair"] == "btcidr":
            r["take_profit_pct"] = 7.0          # diubah manual oleh pengguna
    octx.svc.update("pairs", rows)
    r = opt.rollback_run(run["id"], "admin")
    e = pair_cfg(octx.svc.load(), "btcidr")["exits"]
    assert e["stop_loss_pct"] == 2.0 and e["take_profit_pct"] == 7.0
    assert r["data"]["rollback"]["skipped"]


def test_scheduler_thread_runs_when_due(octx, monkeypatch):
    add_trips(octx.db, 3)
    opt = make_opt(octx)
    th = OptimizerThread(opt, check_every=0.05, first_delay=-1)
    th.start()
    deadline = time.time() + 20
    while time.time() < deadline and not octx.db.list_opt_runs(5):
        time.sleep(0.05)
    th.stop_event.set()
    runs = octx.db.list_opt_runs(5)
    assert runs and runs[0]["data"]["trigger"] == "schedule"


def test_config_validation(octx):
    from bot.config import ConfigError
    with pytest.raises(ConfigError):
        octx.svc.update("optimizer", {"mode": "yolo"})
    with pytest.raises(ConfigError):
        octx.svc.update("optimizer", {"max_step_pct": 500})
    cfg = octx.svc.update("optimizer", {"mode": "suggest", "interval_hours": 12})
    assert cfg["optimizer"]["mode"] == "suggest" and cfg["optimizer"]["interval_hours"] == 12


def test_api(server, monkeypatch):  # noqa: F811
    ctx, url, _ = server
    ctx.db.add_user("admin", "password-kuat-1")
    assert rq.get(url + "/api/optimizer").status_code == 401
    s = _login(url)
    r = s.get(url + "/api/optimizer")
    assert r.status_code == 200 and r.json()["settings"]["mode"] == "auto"
    assert s.get(url + "/optimizer").status_code == 200
    # tanpa CSRF ditolak
    assert rq.post(url + "/api/optimizer/run", json={}, headers={"X-Dashboard": "1"},
                   cookies=s.cookies).status_code in (401, 403)
    # izinkan apply_in_live wajib password
    r = s.put(url + "/api/settings/optimizer", json={"value": {"apply_in_live": True}})
    assert r.status_code == 403
    r = s.put(url + "/api/settings/optimizer", json={"value": {"apply_in_live": True}, "password": "password-kuat-1"})
    assert r.status_code == 200, r.text
    r = s.post(url + "/api/optimizer/run", json={})
    assert r.status_code in (200, 409)
    deadline = time.time() + 30
    while time.time() < deadline and not ctx.db.list_opt_runs(1):
        time.sleep(0.2)
    runs = s.get(url + "/api/optimizer").json()["runs"]
    assert runs and runs[0]["status"] in ("skipped", "error", "no_change")
    assert s.post(url + f"/api/optimizer/runs/{runs[0]['id']}/apply", json={}).status_code == 400
    assert s.post(url + "/api/optimizer/runs/999/rollback", json={}).status_code == 400
