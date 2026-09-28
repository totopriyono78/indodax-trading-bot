"""Eksekusi order. PaperBroker = simulasi, LiveBroker = order sungguhan via TAPI v2."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Dict, Tuple

import requests

from .client import IndodaxError, PrivateClientV2
from .market import PairInfo, Quote

log = logging.getLogger("bot.broker")


class OrderFailed(Exception):
    pass


@dataclass
class Fill:
    qty: float           # jumlah koin yang benar-benar diterima (beli) / terjual (jual)
    avg_price: float
    gross_idr: float     # qty x harga
    fee_idr: float       # fee + pajak (perkiraan jika estimated=True)
    net_idr: float       # beli: total IDR keluar; jual: total IDR diterima
    order_id: str = ""
    client_order_id: str = ""
    estimated: bool = False


def fmt_qty(qty: float, decimals: int = 8) -> str:
    """Bulatkan ke bawah & format tanpa notasi ilmiah (mis. 1e-05 -> '0.00001')."""
    q = Decimal(str(qty)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def make_client_order_id(pair: str, side: str) -> str:
    base = re.sub(r"[^a-z0-9]", "", pair.lower())[:12]
    return f"bot-{base}-{side[0].lower()}-{int(time.time() * 1000)}"[:36]


class PaperBroker:
    """Simulasi eksekusi memakai harga bid/ask asli + slippage + fee dari config."""

    mode = "paper"

    def __init__(self, cfg: dict, balances: Dict[str, float]):
        self.fee_buy = cfg["fees"]["buy_pct"] / 100
        self.fee_sell = cfg["fees"]["sell_pct"] / 100
        self.slip = cfg["paper"]["slippage_pct"] / 100
        self.bal = balances
        if not self.bal:
            self.bal["idr"] = float(cfg["paper"]["starting_idr"])

    def balances(self) -> Dict[str, Tuple[float, float]]:
        return {k: (v, 0.0) for k, v in self.bal.items()}

    def buy(self, pair: PairInfo, idr: float, quote: Quote) -> Fill:
        if self.bal.get("idr", 0) < idr:
            raise OrderFailed(f"saldo IDR simulasi tidak cukup ({self.bal.get('idr', 0):,.0f})")
        price = quote.ask * (1 + self.slip)
        fee = idr * self.fee_buy
        qty = float(fmt_qty((idr - fee) / price))
        self.bal["idr"] = self.bal.get("idr", 0) - idr
        self.bal[pair.coin] = self.bal.get(pair.coin, 0) + qty
        return Fill(qty, price, idr - fee, fee, idr, order_id=f"paper-{int(time.time())}")

    def sell(self, pair: PairInfo, qty: float, quote: Quote) -> Fill:
        qty = min(qty, self.bal.get(pair.coin, 0))
        if qty <= 0:
            raise OrderFailed("tidak ada saldo koin simulasi untuk dijual")
        price = quote.bid * (1 - self.slip)
        gross = qty * price
        fee = gross * self.fee_sell
        self.bal[pair.coin] = self.bal.get(pair.coin, 0) - qty
        if self.bal[pair.coin] < 1e-12:
            self.bal.pop(pair.coin)
        self.bal["idr"] = self.bal.get("idr", 0) + gross - fee
        return Fill(qty, price, gross, fee, gross - fee, order_id=f"paper-{int(time.time())}")


class LiveBroker:
    """Order MARKET sungguhan. Hasil eksekusi (qty, harga, fee) diambil dari riwayat trade."""

    mode = "live"
    TERMINAL = {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}

    def __init__(self, cfg: dict, api: PrivateClientV2):
        self.api = api
        self.fee_buy = cfg["fees"]["buy_pct"] / 100
        self.fee_sell = cfg["fees"]["sell_pct"] / 100

    def balances(self) -> Dict[str, Tuple[float, float]]:
        acc = self.api.account(omit_zero=True)
        out = {}
        for b in acc.get("balances", []):
            out[str(b["asset"]).lower()] = (float(b.get("free") or 0), float(b.get("locked") or 0))
        return out

    # ---- internal ----
    def _place(self, pair: PairInfo, side: str, **kw) -> Tuple[dict, str]:
        cid = make_client_order_id(pair.id, side)
        try:
            resp = self.api.create_order(pair.symbol, side, "MARKET", client_order_id=cid, **kw)
        except IndodaxError as e:
            # Error eksplisit dari API (kode -xxxx, HTTP 4xx) = order ditolak. Tapi HTTP 5xx /
            # respon bukan JSON (mis. 502 dari gateway) / -1001 bisa terjadi SETELAH order diterima.
            uncertain = e.code == -1001 or (e.code is None and (e.status is None or e.status >= 500))
            if not uncertain:
                raise
            log.warning("Respon tidak jelas saat kirim order %s (%s); memeriksa status...", cid, e)
            resp = self._lookup(pair, cid, e)
        except requests.RequestException as e:
            # Jaringan putus setelah order terkirim? Cek dulu sebelum menganggap gagal.
            log.warning("Koneksi bermasalah saat kirim order %s (%s); memeriksa status...", cid, e)
            resp = self._lookup(pair, cid, e)
        return resp, cid

    def _lookup(self, pair: PairInfo, cid: str, cause, attempts: int = 5) -> dict:
        """Cari order berdasarkan client order id setelah pengiriman yang tidak pasti."""
        not_found = 0
        for i in range(attempts):
            time.sleep(3)
            try:
                return self.api.get_order(pair.symbol, orig_client_order_id=cid)
            except IndodaxError as e2:
                if e2.code == -2013:
                    not_found += 1
                log.debug("get_order %s: %s", cid, e2)
            except requests.RequestException as e2:
                log.debug("get_order %s: %s", cid, e2)
        if not_found == attempts:
            raise OrderFailed(f"order tidak terkirim: {cause}")
        raise OrderFailed(f"STATUS ORDER {cid} TIDAK PASTI ({cause}). Cek riwayat order di Indodax "
                          f"secara manual!")

    def _wait_filled(self, pair: PairInfo, order_id, cid: str, timeout: float = 15) -> dict:
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            try:
                last = self.api.get_order(pair.symbol, order_id=order_id, orig_client_order_id=cid)
                if str(last.get("status", "")).upper() in self.TERMINAL:
                    return last
            except (IndodaxError, requests.RequestException) as e:
                log.debug("get_order: %s", e)
            time.sleep(1.5)
        return last

    def _fills(self, pair: PairInfo, cid: str, retries: int = 4):
        for _ in range(retries):
            try:
                data = self.api.my_trades(pair.id, client_order_id=cid)
                rows = data.get("data", data) if isinstance(data, dict) else data
                if rows:
                    return rows
            except (IndodaxError, requests.RequestException) as e:
                log.debug("my_trades: %s", e)
            time.sleep(2)
        return []

    def _summarize(self, pair: PairInfo, rows, side: str, order: dict, cid: str, quote_price: float) -> Fill:
        oid = str(order.get("orderId", ""))
        if rows:
            qty = sum(float(r["qty"]) for r in rows)
            gross = sum(float(r.get("quoteQty") or float(r["qty"]) * float(r["price"])) for r in rows)
            fee_idr = sum(float(r.get("commission") or 0) for r in rows
                          if str(r.get("commissionAsset", "idr")).lower() == "idr")
            fee_coin = sum(float(r.get("commission") or 0) for r in rows
                           if str(r.get("commissionAsset", "")).lower() == pair.coin)
            avg = gross / qty if qty else quote_price
            if side == "BUY":
                return Fill(qty - fee_coin, avg, gross, fee_idr + fee_coin * avg, gross + fee_idr, oid, cid)
            return Fill(qty, avg, gross, fee_idr, gross - fee_idr, oid, cid)
        # Fallback: riwayat trade belum muncul -> perkiraan dari status order
        qty = float(order.get("executedQty") or 0)
        if qty <= 0:
            raise OrderFailed(f"order {cid} tidak tereksekusi (status: {order.get('status')})")
        gross = qty * quote_price
        log.warning("Riwayat trade %s belum tersedia; memakai perkiraan harga & fee", cid)
        if side == "BUY":
            fee = gross * self.fee_buy
            return Fill(qty, quote_price, gross, fee, gross + fee, oid, cid, estimated=True)
        fee = gross * self.fee_sell
        return Fill(qty, quote_price, gross, fee, gross - fee, oid, cid, estimated=True)

    def _coin_total(self, coin: str):
        try:
            free, locked = self.balances().get(coin, (0.0, 0.0))
            return free + locked
        except (IndodaxError, requests.RequestException) as e:
            log.debug("balances: %s", e)
            return None

    def _fallback_buy(self, pair: PairInfo, order: dict, cid: str, idr: float, price: float, before) -> Fill:
        """Riwayat trade belum muncul: hitung qty dari selisih saldo (paling andal), lalu executedQty."""
        oid = str(order.get("orderId", ""))
        after = self._coin_total(pair.coin)
        qty = 0.0
        if before is not None and after is not None and after - before > 0:
            qty = after - before
        else:
            ex = float(order.get("executedQty") or 0)
            if ex > 0 and price > 0 and 0.5 <= ex * price / idr <= 1.5:   # pastikan satuannya koin
                qty = ex
        if qty <= 0:
            raise OrderFailed(f"STATUS ORDER {cid} TIDAK PASTI (hasil eksekusi tidak terbaca). "
                              f"Cek riwayat order di Indodax secara manual!")
        log.warning("Riwayat trade %s belum tersedia; qty dari saldo/status order, harga & fee perkiraan", cid)
        fee = idr * self.fee_buy
        return Fill(qty, (idr - fee) / qty, idr - fee, fee, idr, oid, cid, estimated=True)

    # ---- publik ----
    def buy(self, pair: PairInfo, idr: float, quote: Quote) -> Fill:
        before = self._coin_total(pair.coin)
        resp, cid = self._place(pair, "BUY", quote_order_qty=int(idr))
        order = self._wait_filled(pair, resp.get("orderId"), cid) or resp
        rows = self._fills(pair, cid)
        if not rows:
            return self._fallback_buy(pair, order, cid, idr, quote.ask, before)
        return self._summarize(pair, rows, "BUY", order, cid, quote.ask)

    def sell(self, pair: PairInfo, qty: float, quote: Quote) -> Fill:
        bal = self.balances().get(pair.coin, (0.0, 0.0))[0]
        qty = min(qty, bal)
        if float(fmt_qty(qty)) <= 0:
            raise OrderFailed(f"saldo {pair.coin.upper()} yang bisa dijual = 0")
        # Jika Indodax menolak jumlah desimal, coba lagi dengan desimal lebih sedikit.
        resp = cid = None
        last_err = None
        for dec in (8, 6, 4, 2, 0):
            q = fmt_qty(qty, dec)
            if float(q) <= 0:
                break
            try:
                resp, cid = self._place(pair, "SELL", quantity=q)
                break
            except IndodaxError as e:
                if e.code == -1111 or (e.code == -1130 and "quantity" in str(e).lower()):
                    log.warning("Jumlah %s %s ditolak (%s); coba dengan desimal lebih sedikit",
                                q, pair.coin.upper(), e)
                    last_err = e
                    continue
                raise
        if resp is None:
            raise last_err or OrderFailed(f"jumlah {pair.coin.upper()} terlalu kecil untuk dijual")
        order = self._wait_filled(pair, resp.get("orderId"), cid) or resp
        return self._summarize(pair, self._fills(pair, cid), "SELL", order, cid, quote.bid)
