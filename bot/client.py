"""Klien API Indodax: Public REST API dan Trade API v2 (TAPI v2).

Dokumentasi resmi: https://github.com/btcid/indodax-official-api-docs
"""
import hashlib
import hmac
import logging
import time
import urllib.parse

import requests

log = logging.getLogger("bot.client")


class IndodaxError(Exception):
    """Error dari API Indodax (atau kegagalan request)."""

    def __init__(self, message, code=None, status=None, payload=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.payload = payload

    def __str__(self):
        return f"[HTTP {self.status} code {self.code}] {self.args[0]}"


class _Throttle:
    """Menjaga jarak minimum antar-request agar tidak kena rate limit."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self):
        delay = self._last + self.min_interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()


class PublicClient:
    """Public API — tanpa API key. Batas resmi: 180 request/menit."""

    def __init__(self, base_url="https://indodax.com", timeout=10, session=None):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.s = session or requests.Session()
        self._throttle = _Throttle(0.4)

    def _get(self, path, params=None, retries=3):
        last = None
        for attempt in range(retries):
            self._throttle.wait()
            try:
                r = self.s.get(self.base + path, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    last = "rate limit (429)"
                    time.sleep(5 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise IndodaxError(r.text[:200], status=r.status_code)
                return r.json()
            except (requests.ConnectionError, requests.Timeout, ValueError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise IndodaxError(f"GET {path} gagal setelah {retries} percobaan: {last}")

    def server_time(self):
        """Waktu server dalam milidetik."""
        return int(self._get("/api/server_time")["server_time"])

    def pairs(self):
        return self._get("/api/pairs")

    def ticker_all(self):
        return self._get("/api/ticker_all")

    def ticker(self, pair_id):
        return self._get(f"/api/ticker/{pair_id}")

    def depth(self, pair_id):
        return self._get(f"/api/depth/{pair_id}")

    def ohlc(self, symbol, tf, frm, to):
        """Candle OHLC. symbol mis. 'BTCIDR', tf mis. '15', frm/to unix detik."""
        data = self._get(
            "/tradingview/history_v2",
            params={"symbol": symbol, "tf": tf, "from": int(frm), "to": int(to)},
        )
        return data if isinstance(data, list) else []


class PrivateClientV2:
    """Trade API v2 — butuh API key khusus TAPIv2 (dibuat di indodax.com/trade_api).

    Signature: HMAC-SHA256 atas query string / body memakai secret key,
    dikirim di header `Sign`; API key di header `X-APIKEY`.
    """

    def __init__(self, api_key, secret_key, public, base_url="https://api.indodax.com",
                 recv_window=5000, timeout=15, session=None):
        if not api_key or not secret_key:
            raise ValueError("API key / secret key kosong")
        self.api_key = api_key
        self.secret = secret_key.encode()
        self.public = public
        self.base = base_url.rstrip("/")
        self.recv_window = recv_window
        self.timeout = timeout
        self.s = session or requests.Session()
        self._throttle = _Throttle(0.25)
        self.offset_ms = 0

    # ---- utilitas ----
    def sync_time(self):
        """Selaraskan jam lokal dengan server (request ditolak kalau selisih > recvWindow)."""
        t0 = time.time() * 1000
        server = self.public.server_time()
        t1 = time.time() * 1000
        self.offset_ms = int(server - (t0 + t1) / 2)
        log.info("Selisih jam lokal vs server Indodax: %d ms", self.offset_ms)
        return self.offset_ms

    def _timestamp(self):
        return int(time.time() * 1000) + self.offset_ms

    def sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def _request(self, method, path, params=None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params["timestamp"] = self._timestamp()
        params["recvWindow"] = self.recv_window
        qs = urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/json",
            "X-APIKEY": self.api_key,
            "Sign": self.sign(qs),
        }
        url = self.base + path
        self._throttle.wait()
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            r = self.s.post(url, data=qs, headers=headers, timeout=self.timeout)
        else:
            r = self.s.request(method, f"{url}?{qs}", headers=headers, timeout=self.timeout)

        try:
            data = r.json()
        except ValueError:
            raise IndodaxError(f"Respon bukan JSON: {r.text[:200]}", status=r.status_code)

        is_error_body = isinstance(data, dict) and "code" in data and "msg" in data \
            and data.get("code") not in (0, 200, None)
        if r.status_code >= 400 or is_error_body:
            msg = data.get("msg", str(data)) if isinstance(data, dict) else str(data)
            code = data.get("code") if isinstance(data, dict) else None
            raise IndodaxError(msg, code=code, status=r.status_code, payload=data)
        return data

    # ---- endpoint ----
    def account(self, omit_zero=True):
        return self._request("GET", "/api/v2/account",
                             {"omitZeroBalances": "true" if omit_zero else "false"})

    def create_order(self, symbol, side, order_type, quantity=None, quote_order_qty=None,
                     price=None, client_order_id=None, time_in_force=None, stp_mode=None):
        return self._request("POST", "/api/v2/order", {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "price": price,
            "quantity": quantity,
            "quoteOrderQty": quote_order_qty,
            "newClientOrderId": client_order_id,
            "timeInForce": time_in_force,
            "selfTradePreventionMode": stp_mode,
        })

    def cancel_order(self, symbol, order_id=None, orig_client_order_id=None):
        return self._request("DELETE", "/api/v2/order", {
            "symbol": symbol, "orderId": order_id, "origClientOrderId": orig_client_order_id,
        })

    def open_orders(self, symbol=None):
        return self._request("GET", "/api/v2/openOrders", {"symbol": symbol})

    def get_order(self, symbol, order_id=None, orig_client_order_id=None):
        return self._request("GET", "/api/v2/order", {
            "symbol": symbol, "orderId": order_id, "origClientOrderId": orig_client_order_id,
        })

    def my_trades(self, symbol, client_order_id=None, order_id=None, limit=None):
        return self._request("GET", "/api/v2/myTrades", {
            "symbol": symbol.lower(), "clientOrderId": client_order_id,
            "orderId": order_id, "limit": limit,
        })

    def order_histories(self, symbol, start_time=None, end_time=None, limit=None):
        return self._request("GET", "/api/v2/order/histories", {
            "symbol": symbol.lower(), "startTime": start_time, "endTime": end_time, "limit": limit,
        })
