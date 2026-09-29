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

    # flag global (PAUSE / SELLALL): file di data_dir
    def flag(self, name: str) -> bool:
        return (self.dir / name).exists()

    def set_flag(self, name: str, on: bool = True) -> None:
        f = self.dir / name
        if on:
            f.touch()
        else:
            f.unlink(missing_ok=True)

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


def _state_from_dict(raw: dict) -> BotState:
    st = BotState(**{k: v for k, v in raw.items() if k != "positions" and k in BotState.__dataclass_fields__})
    st.positions = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
    return st


TRADE_KEYS = ["waktu_wib", "mode", "pair", "sisi", "qty", "harga", "idr", "fee_idr", "pnl_idr", "pnl_pct",
              "alasan", "order_id"]


class DbStateStore:
    """Sama seperti StateStore, tetapi semua data disimpan di database (SQLite / PostgreSQL).

    Dengan PostgreSQL, posisi, jurnal transaksi, status, flag, dan log tetap ada walau server/container
    diganti (mis. redeploy di Railway) — tidak perlu volume.
    """

    def __init__(self, db, mode: str, data_dir: Optional[str] = None):
        from sqlalchemy import select  # noqa: F401  (memastikan SQLAlchemy tersedia)
        self.db = db
        self.mode = mode
        self.dir = Path(data_dir) if data_dir else None

    # ---- state
    def load(self) -> BotState:
        raw = self.db.kv_get("bot_state", self.mode)
        return _state_from_dict(raw) if raw else BotState()

    def save(self, st: BotState) -> None:
        self.db.kv_put("bot_state", self.mode, asdict(st))

    # ---- status snapshot untuk dashboard
    def write_status(self, data: dict) -> None:
        self.db.kv_put("bot_status", self.mode, data)

    def read_status(self) -> Optional[dict]:
        return self.db.kv_get("bot_status", self.mode)

    # ---- flag global
    def flag(self, name: str) -> bool:
        return self.db.get_meta("flag_" + name) == "1"

    def set_flag(self, name: str, on: bool = True) -> None:
        self.db.set_meta("flag_" + name, "1" if on else "0")

    # ---- jurnal transaksi
    def log_trade(self, *, pair, side, qty, price, idr, fee_idr=0.0, pnl_idr=None, pnl_pct=None,
                  reason="", order_id=""):
        self.db.add_trade(ts=time.time(), mode=self.mode, pair=pair, side=side, qty=float(qty),
                          price=float(price), idr=float(idr), fee_idr=float(fee_idr or 0),
                          pnl_idr=None if pnl_idr is None else float(pnl_idr),
                          pnl_pct=None if pnl_pct is None else float(pnl_pct), reason=reason or "",
                          order_id=str(order_id or ""))

    def read_trades(self, limit: int = 0) -> list:
        """Format sama dengan CSV lama (string), agar dashboard & ekspor tetap cocok."""
        out = []
        for r in self.db.list_trades(self.mode, limit):
            out.append({
                "waktu_wib": fmt_time(r["ts"]), "mode": r["mode"], "pair": r["pair"], "sisi": r["side"],
                "qty": f"{r['qty']:.8f}", "harga": f"{r['price']:.8f}", "idr": f"{r['idr']:.0f}",
                "fee_idr": f"{r['fee_idr']:.0f}",
                "pnl_idr": "" if r["pnl_idr"] is None else f"{r['pnl_idr']:.0f}",
                "pnl_pct": "" if r["pnl_pct"] is None else f"{r['pnl_pct']:.2f}",
                "alasan": r["reason"], "order_id": r["order_id"], "ts": r["ts"],
            })
        return out

    # ---- migrasi dari versi berbasis file
    def import_files(self, data_dir: str) -> list:
        """Pindahkan state_<mode>.json & trades_<mode>.csv lama ke database (sekali). Kembalikan catatan."""
        notes = []
        d = Path(data_dir)
        f_state = d / f"state_{self.mode}.json"
        if f_state.exists() and self.db.kv_get("bot_state", self.mode) is None:
            self.save(_state_from_dict(json.loads(f_state.read_text(encoding="utf-8"))))
            f_state.rename(f_state.with_suffix(".json.imported"))
            notes.append(f_state.name)
        f_tr = d / f"trades_{self.mode}.csv"
        if f_tr.exists() and not self.db.list_trades(self.mode, 1):
            with f_tr.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        ts = datetime.strptime(row["waktu_wib"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=WIB).timestamp()
                        self.db.add_trade(ts=ts, mode=self.mode, pair=row["pair"], side=row["sisi"],
                                          qty=float(row["qty"]), price=float(row["harga"]), idr=float(row["idr"]),
                                          fee_idr=float(row.get("fee_idr") or 0),
                                          pnl_idr=float(row["pnl_idr"]) if row.get("pnl_idr") else None,
                                          pnl_pct=float(row["pnl_pct"]) if row.get("pnl_pct") else None,
                                          reason=row.get("alasan", ""), order_id=row.get("order_id", ""))
                    except (KeyError, ValueError):
                        continue
            f_tr.rename(f_tr.with_suffix(".csv.imported"))
            notes.append(f_tr.name)
        return notes
