"""Analisis hasil trading dari jurnal transaksi: statistik per kelompok & saran perbaikan.

Semua saran disertai jumlah data pendukung. Saran berbasis sampel kecil ditandai "data masih sedikit" —
selalu uji dulu dengan backtest / mode simulasi sebelum dipakai di LIVE.
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from .engine import rp
from .state import WIB

MIN_GROUP = 8        # minimal transaksi dalam satu kelompok agar dianggap
MIN_STRONG = 30      # di bawah ini, saran ditandai "data masih sedikit"

REASONS = {"take_profit": "Take profit", "stop_loss": "Stop loss", "trailing_stop": "Trailing stop",
           "trend_reversal": "Tren berbalik", "max_hold_time": "Batas waktu tahan",
           "manual_sellall": "Jual semua (manual)", "akhir_data": "Akhir data"}


def _g(x):
    return f"{x:g}".replace(".", ",")


def _d1(x):
    return f"{x:.1f}".replace(".", ",")


def _sd2(x):
    return f"{x:+.2f}".replace(".", ",")


def _f(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def round_trips(trades: List[dict]) -> List[dict]:
    """Ubah jurnal (BUY/SELL) menjadi daftar transaksi selesai (satu baris per penjualan)."""
    out = []
    last_buy: Dict[str, dict] = {}
    for t in trades:
        if t.get("sisi") == "BUY":
            last_buy[t["pair"]] = t
            continue
        if t.get("sisi") != "SELL" or t.get("pnl_idr") in ("", None):
            continue
        m = dict(t.get("meta") or {})
        buy = last_buy.pop(t["pair"], None)
        if buy and not m.get("rsi") and (buy.get("meta") or {}).get("rsi") is not None:
            m = {**buy["meta"], **m}
        ts = t.get("ts") or 0
        entry_ts = m.get("entry_time") or (buy.get("ts") if buy else None)
        pnl = _f(t["pnl_idr"], 0.0)
        cost = _f(m.get("cost_idr")) or (_f(buy.get("idr")) if buy else None) or 0
        out.append({
            "ts": ts, "pair": t["pair"], "reason": t.get("alasan", ""), "pnl": pnl,
            "pnl_pct": _f(t.get("pnl_pct"), (pnl / cost * 100) if cost else 0.0),
            "fee": (_f(t.get("fee_idr"), 0.0) or 0.0) + ((_f(buy.get("fee_idr"), 0.0) or 0.0) if buy else 0.0),
            "entry_ts": entry_ts, "hold_s": m.get("hold_s") or ((ts - entry_ts) if entry_ts else None),
            "rsi": m.get("rsi"), "mfe": m.get("mfe_pct"), "mae": m.get("mae_pct"),
            "spread": m.get("spread_pct"), "ema_trend_dist": m.get("ema_trend_dist_pct"),
            "sl": m.get("sl_pct"), "tp": m.get("tp_pct"), "trail": m.get("trail_pct"),
            "trail_act": m.get("trail_act_pct"), "rich": bool(m.get("mfe_pct") is not None),
        })
    return out


def stats(rows: List[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    gw = sum(r["pnl"] for r in wins)
    gl = -sum(r["pnl"] for r in losses)
    holds = [r["hold_s"] for r in rows if r.get("hold_s")]
    total = sum(r["pnl"] for r in rows)
    fees = sum(r.get("fee") or 0 for r in rows)
    return {
        "n": n, "wins": len(wins), "win_rate": len(wins) / n * 100, "pnl": total,
        "avg_pnl": total / n, "avg_pct": statistics.mean(r["pnl_pct"] for r in rows),
        "avg_win": gw / len(wins) if wins else 0.0, "avg_loss": -gl / len(losses) if losses else 0.0,
        "profit_factor": (gw / gl) if gl else None, "fees": fees,
        "gross_before_fees": total + fees,
        "avg_hold_h": statistics.mean(holds) / 3600 if holds else None,
    }


def group(rows: List[dict], key: Callable, order: Optional[List] = None) -> List[dict]:
    buckets: Dict = {}
    for r in rows:
        k = key(r)
        if k is None:
            continue
        buckets.setdefault(k, []).append(r)
    keys = order if order else sorted(buckets, key=lambda k: -len(buckets[k]))
    return [{"key": k, **stats(buckets[k])} for k in keys if k in buckets]


def _hour_bucket(r):
    t = r.get("entry_ts") or r["ts"]
    h = datetime.fromtimestamp(t, WIB).hour
    return ["00–05", "06–11", "12–17", "18–23"][h // 6]


def _hold_bucket(r):
    s = r.get("hold_s")
    if s is None:
        return None
    h = s / 3600
    for lim, lab in ((1, "< 1 jam"), (4, "1–4 jam"), (12, "4–12 jam"), (24, "12–24 jam")):
        if h < lim:
            return lab
    return "> 24 jam"


def _rsi_bucket(r):
    v = r.get("rsi")
    if v is None:
        return None
    for lim, lab in ((55, "< 55"), (60, "55–60"), (65, "60–65"), (70, "65–70")):
        if v < lim:
            return lab
    return "≥ 70"


def _cfg_key(r):
    if r.get("sl") is None:
        return None
    def f(x):
        return "–" if not x else f"{_g(x)}%"
    return f"SL {f(r['sl'])} · TP {f(r['tp'])} · trail {f(r['trail'])}"


def _pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def suggestions(rows: List[dict], cfg: dict, pair_cfg_fn) -> List[dict]:
    """Saran berbasis aturan. Setiap saran: level, judul, detail, n (jumlah data pendukung)."""
    out = []
    n = len(rows)

    def add(level, title, detail, support):
        out.append({"level": level, "title": title, "detail": detail, "n": support,
                    "weak": support < MIN_STRONG})

    if n < MIN_GROUP:
        add("info", "Data belum cukup untuk dianalisis",
            f"Baru {n} transaksi selesai. Saran mulai muncul setelah ±{MIN_GROUP} transaksi, "
            f"dan cukup meyakinkan setelah ±{MIN_STRONG} transaksi.", n)
        return out

    s = stats(rows)
    # 1) biaya memakan keuntungan
    if s["gross_before_fees"] > 0 and s["fees"] > 0.5 * s["gross_before_fees"]:
        add("warn", "Biaya memakan sebagian besar keuntungan",
            f"Fee & pajak {rp(s['fees'])} dari keuntungan kotor {rp(s['gross_before_fees'])} "
            f"({s['fees'] / s['gross_before_fees'] * 100:.0f}%). Target untung per transaksi terlalu kecil — "
            f"naikkan take profit / aktivasi trailing, atau kurangi frekuensi transaksi.", n)

    # 2) pair yang konsisten rugi
    for g in group(rows, lambda r: r["pair"]):
        if g["n"] >= MIN_GROUP and g["pnl"] < 0 and g["win_rate"] < 40:
            pc = pair_cfg_fn(g["key"])
            add("bad", f"{g['key'].upper()} konsisten rugi",
                f"{g['n']} transaksi, win rate {g['win_rate']:.0f}%, total {rp(g['pnl'])}. "
                f"Pertimbangkan menonaktifkan pair ini, atau memberi stop loss lebih longgar "
                f"(sekarang {_g(pc['exits']['stop_loss_pct'])}%) jika kerugiannya kebanyakan dari stop loss.", g["n"])
        elif g["n"] >= MIN_GROUP and g["pnl"] > 0 and g["win_rate"] >= 55:
            add("good", f"{g['key'].upper()} berkinerja baik",
                f"{g['n']} transaksi, win rate {g['win_rate']:.0f}%, total {rp(g['pnl'])}. "
                f"Pengaturannya sebaiknya tidak diubah dulu.", g["n"])

    rich = [r for r in rows if r["rich"]]
    losers = [r for r in rich if r["pnl"] <= 0]
    winners = [r for r in rich if r["pnl"] > 0]

    # 3) posisi rugi yang sempat untung -> trailing / TP terlalu jauh
    if len(losers) >= MIN_GROUP:
        act = cfg["exits"]["trailing_activation_pct"]
        gave_back = [r for r in losers if (r["mfe"] or 0) >= 1.5]
        if len(gave_back) / len(losers) >= 0.3:
            med = statistics.median(r["mfe"] for r in gave_back)
            add("warn", "Banyak posisi rugi yang sempat untung",
                f"{len(gave_back)} dari {len(losers)} transaksi rugi sempat naik ≥ +1,5% (median +{_d1(med)}%) "
                f"sebelum berbalik. Coba turunkan aktivasi trailing (sekarang {_g(act)}%) ke sekitar "
                f"{_g(max(1.0, round(med * 0.8, 1)))}% atau pasang take profit lebih dekat.", len(losers))

    # 4) stop loss bisa diperketat
    if len(winners) >= MIN_GROUP:
        p90 = _pct([-(r["mae"] or 0) for r in winners], 0.9)
        sl_now = statistics.median([r["sl"] for r in winners if r.get("sl")] or [cfg["exits"]["stop_loss_pct"]])
        if p90 is not None and p90 + 0.3 < sl_now * 0.7:
            add("info", "Stop loss mungkin bisa diperketat",
                f"90% transaksi yang akhirnya untung tidak pernah turun lebih dari −{_d1(p90)}% "
                f"(stop loss sekarang {_g(sl_now)}%). SL sekitar {_d1(p90 + 0.5)}% berpotensi memperkecil kerugian "
                f"tanpa banyak memotong transaksi yang untung.", len(winners))

    # 5) stop loss terlalu sempit
    sl_exits = [r for r in rich if r["reason"] == "stop_loss"]
    if len(sl_exits) >= MIN_GROUP and len(sl_exits) / max(1, len(rich)) > 0.5:
        add("warn", "Lebih dari separuh transaksi ditutup oleh stop loss",
            f"{len(sl_exits)} dari {len(rich)} transaksi. Kemungkinan SL terlalu sempit untuk gejolak harga, "
            f"atau sinyal beli masuk terlalu terlambat (setelah harga sudah naik). Uji SL lebih longgar dengan "
            f"`backtest --sweep`.", len(sl_exits))

    # 6) RSI saat beli
    rg = [g for g in group(rows, _rsi_bucket) if g["n"] >= MIN_GROUP]
    if len(rg) >= 2:
        worst = min(rg, key=lambda g: g["avg_pct"])
        best = max(rg, key=lambda g: g["avg_pct"])
        if worst["pnl"] < 0 < best["pnl"] and worst["key"] in ("65–70", "≥ 70"):
            add("warn", f"Beli saat RSI {worst['key']} cenderung rugi",
                f"{worst['n']} transaksi, rata-rata {_sd2(worst['avg_pct'])}% per transaksi, sedangkan RSI "
                f"{best['key']} rata-rata {_sd2(best['avg_pct'])}%. Pertimbangkan menurunkan RSI maksimum "
                f"(sekarang {_g(cfg['strategy']['rsi_max'])}).", worst["n"])

    # 7) jam transaksi
    hg = [g for g in group(rows, _hour_bucket) if g["n"] >= MIN_GROUP]
    for g in hg:
        if g["pnl"] < 0 and g["win_rate"] < 35 and s["pnl"] > g["pnl"]:
            add("info", f"Pembelian jam {g['key']} WIB kurang baik",
                f"{g['n']} transaksi, win rate {g['win_rate']:.0f}%, total {rp(g['pnl'])}. Pasar pada jam ini "
                f"mungkin lebih sepi / bergejolak. Perhatikan apakah pola ini berlanjut.", g["n"])

    # 8) batas waktu tahan
    mh = [r for r in rows if r["reason"] == "max_hold_time"]
    if len(mh) >= MIN_GROUP and sum(r["pnl"] for r in mh) < 0:
        add("info", "Posisi yang ditutup karena batas waktu cenderung rugi",
            f"{len(mh)} transaksi, total {rp(sum(r['pnl'] for r in mh))}. Batas waktu tahan yang lebih pendek "
            f"bisa mengurangi modal yang 'terjebak' di posisi yang tidak bergerak.", len(mh))

    if not any(o["level"] in ("bad", "warn") for o in out):
        add("good", "Tidak ada masalah menonjol",
            "Belum ditemukan pola rugi yang jelas. Lanjutkan pengumpulan data dan evaluasi rutin.", n)
    order = {"bad": 0, "warn": 1, "info": 2, "good": 3}
    out.sort(key=lambda o: (order[o["level"]], -o["n"]))
    return out


def build_analysis(trades: List[dict], cfg: dict, pair_cfg_fn, period: str = "all",
                   last_change_ts: Optional[float] = None, now: Optional[float] = None) -> dict:
    now = now or time.time()
    all_rows = round_trips(trades)
    since = {"7d": now - 7 * 86400, "30d": now - 30 * 86400, "90d": now - 90 * 86400}.get(period)
    rows = [r for r in all_rows if not since or r["ts"] >= since]

    compare = None
    if last_change_ts:
        before = [r for r in all_rows if (r.get("entry_ts") or r["ts"]) < last_change_ts]
        after = [r for r in all_rows if (r.get("entry_ts") or r["ts"]) >= last_change_ts]
        compare = {"since": last_change_ts, "before": stats(before), "after": stats(after)}

    dist = [round(r["pnl_pct"], 3) for r in rows]
    scatter = [{"mae": r["mae"], "mfe": r["mfe"], "pnl_pct": r["pnl_pct"], "pair": r["pair"],
                "reason": r["reason"], "ts": r["ts"]} for r in rows if r["rich"]]
    cum, curve = 0.0, []
    for r in rows:
        cum += r["pnl"]
        curve.append({"ts": r["ts"], "cum": cum})
    return {
        "period": period, "n_all": len(all_rows), "n_rich": sum(1 for r in rows if r["rich"]),
        "summary": stats(rows),
        "by_pair": group(rows, lambda r: r["pair"]),
        "by_reason": [{**g, "label": REASONS.get(g["key"], g["key"])} for g in group(rows, lambda r: r["reason"])],
        "by_hour": group(rows, _hour_bucket, ["00–05", "06–11", "12–17", "18–23"]),
        "by_hold": group(rows, _hold_bucket, ["< 1 jam", "1–4 jam", "4–12 jam", "12–24 jam", "> 24 jam"]),
        "by_rsi": group(rows, _rsi_bucket, ["< 55", "55–60", "60–65", "65–70", "≥ 70"]),
        "by_settings": group(rows, _cfg_key),
        "distribution": dist,
        "scatter": scatter,
        "curve": curve,
        "compare": compare,
        "suggestions": suggestions(rows, cfg, pair_cfg_fn),
    }
