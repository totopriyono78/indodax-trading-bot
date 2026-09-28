"""Penyimpanan state bot (posisi terbuka, PnL harian) dan jurnal transaksi."""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from zoneinfo import ZoneInfo

WIB = ZoneInfo("Asia/Jakarta")


def today_wib(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), WIB).strftime("%Y-%m-%d")


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, WIB).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Position:
    pair: str
    qty: float                 # jumlah koin yang dibeli bot
    entry_price: float         # harga rata-rata beli
    cost_idr: float            # total IDR yang keluar (termasuk fee)
    opened_at: float
    highest: float             # harga tertinggi sejak dibeli (untuk trailing stop)
    mode: str
    order_id: str = ""
    client_order_id: str = ""
    reason: str = ""
    dust: bool = False         # nilai di bawah minimum order, tidak bisa dijual via bot

    @property
    def value_note(self) -> str:
        return f"{self.qty:.8f} @ {self.entry_price:,.0f}"


@dataclass
class BotState:
    positions: Dict[str, Position] = field(default_factory=dict)
    daily_pnl: Dict[str, float] = field(default_factory=dict)
    cooldown_until: Dict[str, float] = field(default_factory=dict)
    last_candle: Dict[str, int] = field(default_factory=dict)
    paper_balances: Dict[str, float] = field(default_factory=dict)
    halted_day: str = ""
    total_realized: float = 0.0
    trades_count: int = 0
    wins: int = 0


class StateStore:
    def __init__(self, data_dir: str, mode: str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.path = self.dir / f"state_{mode}.json"
        self.trades_path = self.dir / f"trades_{mode}.csv"

    def load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        st = BotState(**{k: v for k, v in raw.items() if k != "positions" and k in BotState.__dataclass_fields__})
        st.positions = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
        return st

    def save(self, st: BotState) -> None:
        data = asdict(st)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def status_path(self) -> Path:
        return self.dir / f"status_{self.mode}.json"

    def write_status(self, data: dict) -> None:
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.status_path)

    def read_status(self) -> Optional[dict]:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def read_trades(self, limit: int = 0) -> list:
        if not self.trades_path.exists():
            return []
        with self.trades_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return rows[-limit:] if limit else rows

    def log_trade(self, *, pair, side, qty, price, idr, fee_idr=0.0, pnl_idr=None, pnl_pct=None,
                  reason="", order_id=""):
        new = not self.trades_path.exists()
        with self.trades_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["waktu_wib", "mode", "pair", "sisi", "qty", "harga", "idr", "fee_idr",
                            "pnl_idr", "pnl_pct", "alasan", "order_id"])
            w.writerow([fmt_time(time.time()), self.mode, pair, side, f"{qty:.8f}", f"{price:.8f}",
                        f"{idr:.0f}", f"{fee_idr:.0f}",
                        "" if pnl_idr is None else f"{pnl_idr:.0f}",
                        "" if pnl_pct is None else f"{pnl_pct:.2f}", reason, order_id])
