"""Strategi: sinyal beli berbasis tren (EMA + RSI) dan aturan keluar (TP / SL / trailing).

Logika yang sama dipakai oleh bot live/paper dan oleh backtest, supaya hasil
backtest mencerminkan perilaku bot sebenarnya.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .indicators import ema, rsi


@dataclass
class Signal:
    action: str            # "buy", "exit" (trend reversal), atau "hold"
    reason: str
    close: float
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    ema_trend: Optional[float] = None
    rsi: Optional[float] = None


class TrendStrategy:
    """Beli saat tren naik terkonfirmasi:
      1. harga penutupan di atas EMA tren (mis. EMA-100) -> tren besar naik
      2. EMA cepat di atas EMA lambat (dan baru saja memotong ke atas jika require_cross)
      3. RSI di antara rsi_min dan rsi_max -> momentum ada tapi belum jenuh beli
    Sinyal keluar karena tren berbalik: EMA cepat turun ke bawah EMA lambat.
    """

    def __init__(self, cfg: dict):
        s = cfg["strategy"]
        self.fast = int(s["ema_fast"])
        self.slow = int(s["ema_slow"])
        self.trend = int(s["ema_trend"])
        self.rsi_period = int(s["rsi_period"])
        self.rsi_min = float(s["rsi_min"])
        self.rsi_max = float(s["rsi_max"])
        self.require_cross = bool(s["require_cross"])
        self.cross_lookback = max(1, int(s.get("cross_lookback", 3)))
        self.exit_on_reversal = bool(s["exit_on_trend_reversal"])

    @property
    def warmup(self) -> int:
        return max(self.trend, self.slow, self.rsi_period) + self.cross_lookback + 2

    def indicators(self, closes: List[float]):
        return (ema(closes, self.fast), ema(closes, self.slow),
                ema(closes, self.trend), rsi(closes, self.rsi_period))

    def evaluate(self, closes: List[float], in_position: bool, ind=None, i: int = None) -> Signal:
        """Evaluasi pada candle tertutup ke-i (default: candle terakhir)."""
        if i is None:
            i = len(closes) - 1
        if i + 1 < self.warmup:
            return Signal("hold", "data candle belum cukup", closes[i] if closes else 0.0)
        ef, es, et, r = ind if ind is not None else self.indicators(closes)
        c = closes[i]
        sig = Signal("hold", "", c, ef[i], es[i], et[i], r[i])
        if None in (ef[i], es[i], et[i], r[i]):
            sig.reason = "indikator belum siap"
            return sig

        if in_position:
            if self.exit_on_reversal and ef[i] < es[i]:
                sig.action, sig.reason = "exit", "tren berbalik (EMA cepat < EMA lambat)"
            return sig

        if c <= et[i]:
            sig.reason = "harga di bawah EMA tren"
            return sig
        if ef[i] <= es[i]:
            sig.reason = "EMA cepat belum di atas EMA lambat"
            return sig
        if self.require_cross:
            crossed = any(
                ef[j - 1] is not None and es[j - 1] is not None and ef[j - 1] <= es[j - 1] and ef[j] > es[j]
                for j in range(max(1, i - self.cross_lookback + 1), i + 1)
            )
            if not crossed:
                sig.reason = "belum ada persilangan EMA baru"
                return sig
        if not (self.rsi_min <= r[i] <= self.rsi_max):
            sig.reason = f"RSI {r[i]:.1f} di luar rentang {self.rsi_min:.0f}-{self.rsi_max:.0f}"
            return sig
        sig.action = "buy"
        sig.reason = f"tren naik: EMA{self.fast}>EMA{self.slow}, harga>EMA{self.trend}, RSI {r[i]:.1f}"
        return sig


class ExitRules:
    """Menghitung level jual untuk posisi terbuka."""

    def __init__(self, cfg: dict):
        e = cfg["exits"]
        self.tp = float(e["take_profit_pct"]) / 100
        self.sl = float(e["stop_loss_pct"]) / 100
        self.trail = float(e["trailing_stop_pct"]) / 100
        self.trail_act = float(e["trailing_activation_pct"]) / 100
        self.max_hold_s = float(e.get("max_hold_hours") or 0) * 3600

    def levels(self, entry: float, highest: float):
        """Kembalikan (stop_price, stop_reason, take_profit_price atau None)."""
        stop = entry * (1 - self.sl)
        reason = "stop_loss"
        if self.trail > 0 and highest >= entry * (1 + self.trail_act):
            trail_stop = highest * (1 - self.trail)
            if trail_stop > stop:
                stop, reason = trail_stop, "trailing_stop"
        tp = entry * (1 + self.tp) if self.tp > 0 else None
        return stop, reason, tp

    def check(self, entry: float, highest: float, price: float, opened_at: float, now: float) -> Optional[str]:
        """Dipanggil tiap cek harga. Kembalikan alasan jual atau None."""
        stop, reason, tp = self.levels(entry, highest)
        if price <= stop:
            return reason
        if tp is not None and price >= tp:
            return "take_profit"
        if self.max_hold_s and now - opened_at >= self.max_hold_s:
            return "max_hold_time"
        return None
