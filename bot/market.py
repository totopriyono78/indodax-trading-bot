"""Data pasar: info pair (batas minimum order), harga terkini, dan candle OHLC."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .client import PublicClient
from .config import TIMEFRAMES

log = logging.getLogger("bot.market")


@dataclass
class PairInfo:
    id: str               # btcidr
    symbol: str           # BTCIDR
    ticker_id: str        # btc_idr
    coin: str             # btc
    min_idr: float        # nilai order minimum dalam IDR
    min_coin: float       # jumlah koin minimum


@dataclass
class Quote:
    last: float
    bid: float            # harga beli tertinggi (yang bisa kita jual ke sana)
    ask: float            # harga jual terendah (yang bisa kita beli dari sana)
    vol_idr: float
    server_time: int

    @property
    def spread_pct(self) -> float:
        if self.bid <= 0:
            return 999.0
        return (self.ask - self.bid) / self.bid * 100


@dataclass
class Candle:
    time: int             # unix detik, waktu buka candle
    open: float
    high: float
    low: float
    close: float
    volume: float


class Market:
    def __init__(self, public: PublicClient):
        self.public = public
        self.pairs: Dict[str, PairInfo] = {}
        self._pairs_loaded_at = 0.0

    def load_pairs(self, force: bool = False) -> Dict[str, PairInfo]:
        if self.pairs and not force and time.time() - self._pairs_loaded_at < 6 * 3600:
            return self.pairs
        out = {}
        for p in self.public.pairs():
            try:
                pid = str(p["id"]).lower()
                out[pid] = PairInfo(
                    id=pid,
                    symbol=str(p.get("symbol") or pid.upper()),
                    ticker_id=str(p.get("ticker_id") or f"{p.get('traded_currency')}_{p.get('base_currency')}"),
                    coin=str(p.get("traded_currency") or pid[:-3]).lower(),
                    min_idr=float(p.get("trade_min_base_currency") or 0),
                    min_coin=float(p.get("trade_min_traded_currency") or 0),
                )
            except (KeyError, TypeError, ValueError) as e:
                log.debug("Lewati pair tak terbaca %s: %s", p, e)
        self.pairs = out
        self._pairs_loaded_at = time.time()
        return out

    def info(self, pair_id: str) -> PairInfo:
        pairs = self.load_pairs()
        if pair_id not in pairs:
            raise KeyError(f"Pair '{pair_id}' tidak ada di Indodax")
        return pairs[pair_id]

    def quotes(self) -> Dict[str, Quote]:
        """Harga terkini semua pair dalam 1 request (key: pair id, mis. 'btcidr')."""
        data = self.public.ticker_all().get("tickers", {})
        out = {}
        for tid, t in data.items():
            try:
                pid = tid.replace("_", "").lower()
                out[pid] = Quote(
                    last=float(t["last"]),
                    bid=float(t["buy"]),
                    ask=float(t["sell"]),
                    vol_idr=float(t.get("vol_idr") or 0),
                    server_time=int(t.get("server_time") or 0),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def candles(self, symbol: str, tf: str, count: int, closed_only: bool = True,
                end: Optional[int] = None) -> List[Candle]:
        """Ambil `count` candle terakhir. Candle yang belum selesai dibuang jika closed_only."""
        step = TIMEFRAMES[tf]
        now = int(end or time.time())
        start = now - step * (count + 2)
        return self.candles_range(symbol, tf, start, now, closed_only=closed_only, now=now)[-count:]

    def candles_range(self, symbol: str, tf: str, start: int, end: int,
                      closed_only: bool = True, now: Optional[int] = None,
                      chunk: int = 1000) -> List[Candle]:
        step = TIMEFRAMES[tf]
        now = int(now or time.time())
        by_time: Dict[int, Candle] = {}
        cur = int(start)
        while cur < end:
            nxt = min(end, cur + step * chunk)
            for row in self.public.ohlc(symbol, tf, cur, nxt):
                try:
                    c = Candle(int(row["Time"]), float(row["Open"]), float(row["High"]),
                               float(row["Low"]), float(row["Close"]), float(row.get("Volume") or 0))
                except (KeyError, TypeError, ValueError):
                    continue
                by_time[c.time] = c
            cur = nxt
        out = sorted(by_time.values(), key=lambda c: c.time)
        if closed_only:
            out = [c for c in out if c.time + step <= now]
        return out
