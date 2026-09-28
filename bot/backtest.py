"""Backtest: menjalankan strategi yang sama pada data candle historis.

Asumsi (sengaja dibuat konservatif):
- Beli di harga close candle sinyal + slippage.
- Jika dalam satu candle harga menyentuh stop DAN take profit, dianggap kena stop dulu.
- Jika candle dibuka di bawah stop (gap), jual di harga open.
- Fee beli/jual diambil dari config (all-in: trading fee + pajak + CFX).
"""
from __future__ import annotations

import copy
import itertools
from collections import Counter
from dataclasses import dataclass, field
from typing import List

from .config import TIMEFRAMES
from .engine import rp
from .market import Candle
from .strategy import ExitRules, TrendStrategy


@dataclass
class BtTrade:
    entry_time: int
    exit_time: int
    entry: float
    exit: float
    pnl_idr: float
    pnl_pct: float
    reason: str


@dataclass
class BtResult:
    pair: str
    trades: List[BtTrade] = field(default_factory=list)
    buy_hold_pct: float = 0.0
    stake: float = 0.0
    days: float = 0.0

    @property
    def n(self):
        return len(self.trades)

    @property
    def total(self):
        return sum(t.pnl_idr for t in self.trades)

    @property
    def win_rate(self):
        return (sum(1 for t in self.trades if t.pnl_idr > 0) / self.n * 100) if self.n else 0.0

    @property
    def profit_factor(self):
        g = sum(t.pnl_idr for t in self.trades if t.pnl_idr > 0)
        l = -sum(t.pnl_idr for t in self.trades if t.pnl_idr <= 0)
        return g / l if l else float("inf") if g else 0.0

    @property
    def max_drawdown(self):
        peak = cum = dd = 0.0
        for t in self.trades:
            cum += t.pnl_idr
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
        return dd

    @property
    def reasons(self):
        return Counter(t.reason for t in self.trades)


def backtest(cfg: dict, pair: str, candles: List[Candle]) -> BtResult:
    strat = TrendStrategy(cfg)
    rules = ExitRules(cfg)
    fee_b = cfg["fees"]["buy_pct"] / 100
    fee_s = cfg["fees"]["sell_pct"] / 100
    slip = cfg["paper"]["slippage_pct"] / 100
    stake = float(cfg["risk"]["idr_per_trade"])
    cooldown_s = cfg["risk"]["cooldown_minutes_after_loss"] * 60
    step = TIMEFRAMES[cfg["timeframe"]]

    res = BtResult(pair=pair, stake=stake)
    if len(candles) < strat.warmup + 2:
        return res
    closes = [c.close for c in candles]
    ind = strat.indicators(closes)
    res.buy_hold_pct = (closes[-1] / closes[strat.warmup] - 1) * 100
    res.days = (candles[-1].time - candles[strat.warmup].time) / 86400

    pos = None          # dict: entry, qty, highest, opened, entry_time
    cooldown_until = 0

    def close_pos(i, price, reason):
        nonlocal pos, cooldown_until
        proceeds = pos["qty"] * price * (1 - slip) * (1 - fee_s)
        pnl = proceeds - stake
        res.trades.append(BtTrade(pos["entry_time"], candles[i].time, pos["entry"], price,
                                  pnl, pnl / stake * 100, reason))
        if pnl <= 0:
            cooldown_until = candles[i].time + step + cooldown_s
        pos = None

    for i, c in enumerate(candles):
        if pos is not None:
            stop, sreason, tp = rules.levels(pos["entry"], pos["highest"])
            if c.open <= stop:
                close_pos(i, c.open, sreason)
            elif c.low <= stop:
                close_pos(i, stop, sreason)
            elif tp is not None and c.high >= tp:
                close_pos(i, max(tp, c.open), "take_profit")
            else:
                pos["highest"] = max(pos["highest"], c.high)
                stop2, sreason2, _ = rules.levels(pos["entry"], pos["highest"])
                close_t = c.time + step
                if c.close <= stop2:
                    close_pos(i, c.close, sreason2)
                elif rules.max_hold_s and close_t - pos["opened"] >= rules.max_hold_s:
                    close_pos(i, c.close, "max_hold_time")
                elif strat.evaluate(closes, True, ind, i).action == "exit":
                    close_pos(i, c.close, "trend_reversal")
            continue  # tidak beli di candle yang sama dengan penjualan

        if i + 1 < strat.warmup or c.time < cooldown_until:
            continue
        if strat.evaluate(closes, False, ind, i).action == "buy":
            price = c.close * (1 + slip)
            pos = {"entry": price, "qty": stake * (1 - fee_b) / price, "highest": price,
                   "opened": c.time + step, "entry_time": c.time + step}

    if pos is not None:
        close_pos(len(candles) - 1, candles[-1].close, "akhir_data")
    return res


def format_result(r: BtResult) -> str:
    if not r.n:
        return f"{r.pair}: tidak ada transaksi ({r.days:.0f} hari). Buy & hold: {r.buy_hold_pct:+.2f}%"
    reasons = ", ".join(f"{k} {v}" for k, v in r.reasons.most_common())
    pf = "∞" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
    return (f"{r.pair}: {r.n} transaksi dalam {r.days:.0f} hari | win rate {r.win_rate:.0f}% | "
            f"PnL {rp(r.total)} ({r.total / r.stake * 100:+.1f}% dari modal per trade) | "
            f"profit factor {pf} | max drawdown {rp(r.max_drawdown)} | "
            f"buy & hold {r.buy_hold_pct:+.1f}%\n    keluar karena: {reasons}")


def sweep(cfg: dict, data: dict, train_ratio: float = 0.7, top: int = 5):
    """Coba beberapa kombinasi TP/SL/trailing. Pilih di data awal (train), uji di data akhir (test)."""
    grid = {
        "take_profit_pct": [0, 3, 5, 8],
        "stop_loss_pct": [1.5, 2.5, 4],
        "trailing_stop_pct": [0, 1.5, 3],
    }
    rows = []
    for tp, sl, tr in itertools.product(*grid.values()):
        if tp == 0 and tr == 0 and not cfg["strategy"]["exit_on_trend_reversal"]:
            continue
        c2 = copy.deepcopy(cfg)
        c2["exits"].update(take_profit_pct=tp, stop_loss_pct=sl, trailing_stop_pct=tr)
        train = test = 0.0
        n_train = n_test = 0
        for pair, candles in data.items():
            cut = int(len(candles) * train_ratio)
            r1 = backtest(c2, pair, candles[:cut])
            r2 = backtest(c2, pair, candles[max(0, cut - TrendStrategy(c2).warmup):])
            train += r1.total
            test += r2.total
            n_train += r1.n
            n_test += r2.n
        rows.append((train, test, n_train, n_test, tp, sl, tr))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows[:top]
