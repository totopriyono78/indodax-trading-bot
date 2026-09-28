"""Klien palsu untuk pengujian tanpa koneksi ke Indodax."""
import math

STEP = 900  # candle 15 menit


def trend_series(n=400, start=100.0):
    """Turun pelan, lalu naik kuat — memicu sinyal beli tren."""
    out, p = [], start
    for i in range(n):
        if i < 250:
            p *= 0.9995 + 0.002 * math.sin(i / 5)
        else:
            p *= 1.004 + 0.002 * math.sin(i / 3)
        out.append(p)
    return out


class FakePublic:
    def __init__(self, closes_by_symbol, now, vol_idr=5e9, spread=0.001):
        self.closes = closes_by_symbol   # {"BTCIDR": [..]} satu close per candle, candle terakhir berakhir di `now`
        self.now = now
        self.vol_idr = vol_idr
        self.spread = spread
        self.last_override = {}

    def server_time(self):
        return int(self.now * 1000)

    def pairs(self):
        out = []
        for sym in self.closes:
            coin = sym[:-3].lower()
            out.append({"id": sym.lower(), "symbol": sym, "ticker_id": f"{coin}_idr",
                        "traded_currency": coin, "base_currency": "idr",
                        "trade_min_base_currency": 10000, "trade_min_traded_currency": 0.00000001})
        return out

    def _candles(self, sym):
        closes = self.closes[sym]
        end = int(self.now // STEP) * STEP
        t0 = end - STEP * len(closes)
        rows = []
        prev = closes[0]
        for i, c in enumerate(closes):
            rows.append({"Time": t0 + i * STEP, "Open": prev, "High": max(prev, c) * 1.001,
                         "Low": min(prev, c) * 0.999, "Close": c, "Volume": "1"})
            prev = c
        return rows

    def ohlc(self, symbol, tf, frm, to):
        return [r for r in self._candles(symbol) if frm <= r["Time"] <= to]

    def ticker_all(self):
        t = {}
        for sym, closes in self.closes.items():
            last = self.last_override.get(sym, closes[-1])
            coin = sym[:-3].lower()
            t[f"{coin}_idr"] = {"last": str(last), "buy": str(last * (1 - self.spread / 2)),
                                "sell": str(last * (1 + self.spread / 2)), "vol_idr": str(self.vol_idr),
                                "server_time": int(self.now)}
        return {"tickers": t}


class FakePrivate:
    """Meniru respon TAPI v2 untuk LiveBroker."""

    def __init__(self, price=100.0, idr=1_000_000.0, coin="btc"):
        self.price = price
        self.bal = {"idr": idr, coin: 0.0}
        self.coin = coin
        self.orders = {}
        self.trades = {}
        self.calls = []
        self._id = 1000

    def account(self, omit_zero=True):
        return {"canTrade": True, "canWithdraw": False,
                "balances": [{"asset": k.upper(), "free": str(v), "locked": "0"} for k, v in self.bal.items()]}

    def create_order(self, symbol, side, order_type, quantity=None, quote_order_qty=None, price=None,
                     client_order_id=None, **kw):
        self.calls.append((side, order_type, quantity, quote_order_qty))
        self._id += 1
        if side == "BUY":
            idr = float(quote_order_qty)
            fee = idr * 0.003
            qty = (idr - fee) / self.price
            self.bal["idr"] -= idr
            self.bal[self.coin] += qty
            trade = {"qty": str(qty), "price": str(self.price), "quoteQty": str(qty * self.price),
                     "commission": str(fee), "commissionAsset": "idr"}
        else:
            qty = float(quantity)
            gross = qty * self.price
            fee = gross * 0.0051
            self.bal[self.coin] -= qty
            self.bal["idr"] += gross - fee
            trade = {"qty": str(qty), "price": str(self.price), "quoteQty": str(gross),
                     "commission": str(fee), "commissionAsset": "idr"}
        order = {"symbol": symbol, "orderId": self._id, "clientOrderId": client_order_id,
                 "side": side, "type": order_type, "executedQty": str(qty), "status": "FILLED"}
        self.orders[client_order_id] = order
        self.trades[client_order_id] = [trade]
        return order

    def get_order(self, symbol, order_id=None, orig_client_order_id=None):
        return self.orders[orig_client_order_id]

    def my_trades(self, symbol, client_order_id=None, **kw):
        return {"data": self.trades.get(client_order_id, [])}
