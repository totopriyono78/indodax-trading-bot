"""Mesin utama bot: loop monitoring harga, sinyal beli, dan penjualan (TP / SL / trailing)."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, Optional

from .broker import OrderFailed
from .client import IndodaxError
from .config import TIMEFRAMES
from .market import Market, Quote
from .notifier import Notifier
from .state import BotState, Position, StateStore, fmt_time, today_wib
from .settings import pair_cfg
from .strategy import ExitRules, TrendStrategy

log = logging.getLogger("bot.engine")

AUTH_ERRORS = {-1002, -1022, -2014, -2015}


def _id_num(x: float, decimals: int) -> str:
    s = f"{x:,.{decimals}f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def rp(x: float) -> str:
    """Format Rupiah gaya Indonesia: Rp1.234.567"""
    return ("-" if x < 0 else "") + "Rp" + _id_num(abs(x), 0)


def px(p: float) -> str:
    """Format harga koin: 1.234.567.000 / 2.345,67 / 0,00001234"""
    if p >= 100:
        return _id_num(p, 0)
    if p >= 1:
        return _id_num(p, 2)
    s = _id_num(p, 10).rstrip("0")
    return s.rstrip(",") if s.endswith(",") else s


class Engine:
    def __init__(self, cfg: dict, market: Market, broker, store: StateStore, notifier: Notifier,
                 clock=time.time, sleep=time.sleep, config_source=None):
        self.cfg = cfg
        self.market = market
        self.broker = broker
        self.store = store
        self.notify = notifier
        self.clock = clock
        self.sleep = sleep
        self.config_source = config_source
        self.exit_code = 0
        self._apply(cfg)
        self.state: BotState = store.load()
        if broker.mode == "paper":
            broker.bal = self.state.paper_balances = self.state.paper_balances or broker.bal
        self.entries_blocked: Optional[str] = None
        self._retry_after: Dict[str, float] = {}
        self._candle_try: Dict[str, float] = {}
        self._last_heartbeat = 0.0
        self._day = today_wib(self.clock())
        self._stop = False
        self._pairs_stale = False
        self.last_signals: Dict[str, dict] = {}

    # ------------------------------------------------------------------ setup
    def _apply(self, cfg: dict) -> None:
        self.cfg = cfg
        self.strategy = TrendStrategy(cfg)
        self.exits = ExitRules(cfg)
        self._exits_cache: Dict[str, ExitRules] = {}
        self.risk = cfg["risk"]
        self.tf = cfg["timeframe"]
        self.step = TIMEFRAMES[self.tf]

    def exits_for(self, pair: str) -> ExitRules:
        """Aturan jual untuk pair ini (stop loss / TP / trailing khusus pair jika diatur)."""
        if pair not in self._exits_cache:
            self._exits_cache[pair] = ExitRules(pair_cfg(self.cfg, pair))
        return self._exits_cache[pair]

    def idr_for(self, pair: str) -> float:
        return float(pair_cfg(self.cfg, pair)["risk"]["idr_per_trade"])

    def _validate_pairs(self) -> list:
        pairs = self.market.load_pairs()
        valid = []
        for p in self.cfg["pairs"]:
            if p not in pairs:
                log.error("Pair %s tidak ditemukan di Indodax — dilewati", p)
                continue
            info = pairs[p]
            if info.min_idr and self.idr_for(p) < info.min_idr:
                log.error("Pair %s: modal per transaksi %s < minimum order %s — dilewati",
                          p, rp(self.idr_for(p)), rp(info.min_idr))
                continue
            valid.append(p)
        if not valid:
            log.warning("Tidak ada pair aktif yang valid — bot hanya menjaga posisi yang sudah terbuka.")
        return valid

    def startup(self) -> None:
        self.pairs = self._validate_pairs()
        if self.broker.mode == "live":
            self._reconcile()
        self.store.save(self.state)
        self.notify.send(
            f"Bot aktif ({self.broker.mode.upper()}). Pair: {', '.join(self.pairs) or '-'} | TF {self.tf}m | "
            f"{rp(self.risk['idr_per_trade'])}/trade | posisi terbuka: {len(self.state.positions)}")

    def reload(self, cfg: dict) -> None:
        """Terapkan pengaturan baru dari database tanpa restart."""
        if cfg["mode"] != self.broker.mode:
            self.notify.send(f"Mode diubah ke {cfg['mode'].upper()} — bot dimulai ulang otomatis.")
            self.exit_code = 75
            self._stop = True
            return
        old_pairs = list(self.pairs)
        self._apply(cfg)
        self.notify.update(self.notify.token, self.notify.chat_id, cfg["telegram"]["enabled"])
        try:
            self.pairs = self._validate_pairs()
            self._pairs_stale = False
        except Exception:
            # daftar pair Indodax gagal dimuat: jangan terus membeli pair yang sudah dinonaktifkan;
            # pair baru divalidasi ulang di tick berikutnya
            self.pairs = [p for p in old_pairs if p in cfg["pairs"]]
            self._pairs_stale = True
            raise
        log.info("Pengaturan baru diterapkan. Pair aktif: %s", ", ".join(self.pairs) or "-")
        if old_pairs != self.pairs:
            self.notify.send(f"Pengaturan diperbarui. Pair aktif: {', '.join(self.pairs) or '-'}")

    def _reconcile(self) -> None:
        """Pastikan posisi di state masih sesuai saldo nyata (mis. jika Anda menjual manual)."""
        bal = self.broker.balances()
        quotes = self.market.quotes()
        for pair, pos in list(self.state.positions.items()):
            info = self.market.info(pair)
            free, locked = bal.get(info.coin, (0.0, 0.0))
            held = free + locked
            if held >= pos.qty * 0.98:
                continue
            price = quotes.get(pair).last if pair in quotes else pos.entry_price
            if held * price < max(info.min_idr, 1):
                self.notify.send(f"{pair}: saldo koin hilang/berkurang (mungkin dijual manual). "
                                 f"Posisi bot dihapus dari catatan.")
                del self.state.positions[pair]
            else:
                ratio = held / pos.qty
                pos.cost_idr *= ratio
                pos.qty = held
                self.notify.send(f"{pair}: saldo koin lebih kecil dari catatan bot; qty disesuaikan ke {held:.8f}")

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        self.startup()
        backoff = 5
        while not self._stop:
            try:
                self.tick()
                backoff = 5
                self._wait(self.cfg["poll_seconds"])
            except KeyboardInterrupt:
                break
            except Exception as e:  # jangan biarkan bot mati karena error sementara
                log.exception("Error di loop utama: %s", e)
                self.notify.send(f"Error: {e}", key="loop_error", min_interval=900)
                self._wait(backoff)
                backoff = min(backoff * 2, 300)
        self.store.save(self.state)
        self.notify.send("Bot berhenti.")

    def stop(self, *_):
        self._stop = True

    def _wait(self, seconds: float) -> None:
        """Tidur dalam potongan 1 detik agar bisa berhenti cepat saat dihentikan systemd."""
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            self.sleep(min(1.0, end - time.monotonic()))

    def _poll_config(self) -> None:
        src = self.config_source
        if not src:
            return
        try:
            cfg = src.poll_config()
            if cfg is not None:
                self.reload(cfg)
            elif self._pairs_stale:
                self.pairs = self._validate_pairs()
                self._pairs_stale = False
                log.info("Pair aktif: %s", ", ".join(self.pairs) or "-")
            creds = src.poll_credentials()
            if creds is not None:
                self._apply_credentials(creds)
        except Exception as e:
            log.warning("Gagal membaca pengaturan dari database: %s", e)
            self.notify.send(f"Gagal membaca pengaturan dari database: {e}", key="cfgerr", min_interval=1800)

    def _apply_credentials(self, creds: dict) -> None:
        self.notify.update(creds.get("telegram_token", ""), creds.get("telegram_chat_id", ""),
                           self.cfg["telegram"]["enabled"])
        api = getattr(self.broker, "api", None)
        if api is not None and creds.get("api_key") and creds.get("secret_key"):
            if (creds["api_key"], creds["secret_key"]) != (api.api_key, api.secret.decode()):
                api.api_key = creds["api_key"]
                api.secret = creds["secret_key"].encode()
                api.sync_time()
                if self.entries_blocked and "API" in self.entries_blocked:
                    self.entries_blocked = None
                self.notify.send("API key Indodax diperbarui dan langsung dipakai bot.")

    def tick(self) -> None:
        now = self.clock()
        self._poll_config()
        if self._stop:
            return
        self._day_rollover(now)
        quotes = self.market.quotes()

        if self.store.flag("SELLALL"):
            self._sell_all(quotes)
            if not any(not p.dust for p in self.state.positions.values()):
                self.store.set_flag("SELLALL", False)
                self.notify.send("SELLALL selesai. Pembelian baru tetap di-pause sampai Anda menjalankan `resume`.")

        # 1) Cek TP / SL / trailing untuk setiap posisi terbuka — setiap tick
        for pair, pos in list(self.state.positions.items()):
            q = quotes.get(pair)
            if not q or pos.dust:
                continue
            # pakai harga bid (harga yang benar-benar bisa didapat saat menjual)
            price = q.bid if q.bid > 0 else q.last
            if price > pos.highest:
                pos.highest = price
            reason = self.exits_for(pair).check(pos.entry_price, pos.highest, price, pos.opened_at, now)
            if reason:
                try:
                    self._close(pair, q, reason)
                except Exception as e:  # error di satu pair tidak boleh menghalangi SL pair lain
                    log.exception("Error saat menjual %s: %s", pair, e)
                    self._retry_after[pair] = now + 30
                    self.notify.send(f"Error saat menjual {pair} ({reason}): {e}",
                                     key=f"sellerr_{pair}", min_interval=600)

        # 2) Sinyal berbasis candle — dievaluasi sekali per candle tertutup baru
        expected = int(now // self.step) * self.step - self.step
        for pair in self.pairs:
            last_done = self.state.last_candle.get(pair, 0)
            if last_done >= expected or now - self._candle_try.get(pair, 0) < 30:
                continue
            self._candle_try[pair] = now
            try:
                info = self.market.info(pair)
                candles = self.market.candles(info.symbol, self.tf, self.cfg["candles_lookback"], end=int(now))
            except Exception as e:
                log.warning("%s: gagal mengambil candle: %s", pair, e)
                continue
            if not candles or candles[-1].time <= last_done:
                continue  # candle baru belum tersedia di server; coba lagi nanti
            self.state.last_candle[pair] = candles[-1].time
            closes = [c.close for c in candles]
            in_pos = pair in self.state.positions
            sig = self.strategy.evaluate(closes, in_pos)
            log.info("%s close=%s %s — %s", pair, f"{px(sig.close)}", sig.action.upper(), sig.reason)
            self.last_signals[pair] = {"time": candles[-1].time, "close": sig.close, "action": sig.action,
                                       "reason": sig.reason, "rsi": sig.rsi, "ema_fast": sig.ema_fast,
                                       "ema_slow": sig.ema_slow, "ema_trend": sig.ema_trend}
            q = quotes.get(pair)
            if not q:
                continue
            if in_pos and sig.action == "exit" and not self.state.positions[pair].dust:
                self._close(pair, q, "trend_reversal")
            elif not in_pos and sig.action == "buy":
                self._try_enter(pair, q, sig.reason)

        self._heartbeat(now, quotes)
        self.store.save(self.state)
        self._write_status(now, quotes)

    # ------------------------------------------------------------------ entry
    def _entry_block_reason(self, pair: str, q: Quote) -> Optional[str]:
        now = self.clock()
        if self.entries_blocked:
            return self.entries_blocked
        if self.store.flag("PAUSE"):
            return "bot di-pause"
        if self.state.halted_day == today_wib(now):
            return "batas rugi harian tercapai"
        if self.state.cooldown_until.get(pair, 0) > now:
            return f"cooldown sampai {fmt_time(self.state.cooldown_until[pair])}"
        active = [p for p in self.state.positions.values() if not p.dust]
        if len(active) >= self.risk["max_open_positions"]:
            return "jumlah posisi maksimum tercapai"
        exposure = sum(p.cost_idr for p in active)
        if exposure + self.idr_for(pair) > self.risk["max_total_exposure_idr"]:
            return f"eksposur total akan melebihi {rp(self.risk['max_total_exposure_idr'])}"
        if q.spread_pct > self.risk["max_spread_pct"]:
            return f"spread {q.spread_pct:.2f}% terlalu lebar"
        if q.vol_idr < self.risk["min_24h_volume_idr"]:
            return f"volume 24 jam {rp(q.vol_idr)} terlalu kecil"
        return None

    def _try_enter(self, pair: str, q: Quote, why: str) -> None:
        block = self._entry_block_reason(pair, q)
        if block:
            log.info("%s: sinyal beli diabaikan — %s", pair, block)
            return
        idr = self.idr_for(pair)
        try:
            free_idr = self.broker.balances().get("idr", (0.0, 0.0))[0]
            if free_idr - idr < self.risk["min_idr_reserve"]:
                log.info("%s: saldo IDR %s tidak cukup (cadangan %s)", pair, rp(free_idr),
                         rp(self.risk["min_idr_reserve"]))
                self.notify.send(f"Sinyal beli {pair} dilewati: saldo IDR {rp(free_idr)} tidak cukup.",
                                 key="no_idr", min_interval=3600)
                return
            info = self.market.info(pair)
            fill = self.broker.buy(info, idr, q)
        except IndodaxError as e:
            self._handle_api_error("beli", pair, e)
            return
        except OrderFailed as e:
            self.notify.send(f"Gagal beli {pair}: {e}")
            return
        now = self.clock()
        self.state.positions[pair] = Position(
            pair=pair, qty=fill.qty, entry_price=fill.avg_price, cost_idr=fill.net_idr,
            opened_at=now, highest=fill.avg_price, mode=self.broker.mode,
            order_id=fill.order_id, client_order_id=fill.client_order_id, reason=why)
        self.store.log_trade(pair=pair, side="BUY", qty=fill.qty, price=fill.avg_price, idr=fill.net_idr,
                             fee_idr=fill.fee_idr, reason=why, order_id=fill.order_id)
        self.store.save(self.state)
        stop, _, tp = self.exits_for(pair).levels(fill.avg_price, fill.avg_price)
        self.notify.send(
            f"BELI {pair.upper()} ({self.broker.mode})\n"
            f"Qty {fill.qty:.8g} @ {px(fill.avg_price)}\nTotal {rp(fill.net_idr)} (fee {rp(fill.fee_idr)})\n"
            f"SL {px(stop)}" + (f" | TP {px(tp)}" if tp else "") + f"\nAlasan: {why}")

    # ------------------------------------------------------------------ exit
    def _close(self, pair: str, q: Quote, reason: str) -> bool:
        pos = self.state.positions[pair]
        now = self.clock()
        if self._retry_after.get(pair, 0) > now:
            return False
        info = self.market.info(pair)
        if q.bid <= 0:
            log.warning("%s: harga bid tidak tersedia, jual ditunda", pair)
            return False
        value = pos.qty * q.bid
        if (info.min_idr and value < info.min_idr) or (info.min_coin and pos.qty < info.min_coin):
            pos.dust = True
            self.notify.send(f"{pair}: nilai posisi {rp(value)} di bawah minimum order — tidak bisa dijual "
                             f"oleh bot. Silakan jual manual. Posisi ditandai 'dust'.")
            return False
        try:
            fill = self.broker.sell(info, pos.qty, q)
        except (IndodaxError, OrderFailed) as e:
            self._retry_after[pair] = now + 30
            if isinstance(e, IndodaxError):
                self._handle_api_error("jual", pair, e)
            else:
                self.notify.send(f"Gagal jual {pair} ({reason}): {e}", key=f"sellfail_{pair}", min_interval=600)
            return False

        sold_ratio = min(1.0, fill.qty / pos.qty) if pos.qty else 1.0
        cost = pos.cost_idr * sold_ratio
        pnl = fill.net_idr - cost
        pnl_pct = pnl / cost * 100 if cost else 0.0
        del self.state.positions[pair]

        day = today_wib(now)
        self.state.daily_pnl[day] = self.state.daily_pnl.get(day, 0.0) + pnl
        self.state.total_realized += pnl
        self.state.trades_count += 1
        # minimal 1 candle jeda sebelum beli lagi pair yang sama (mencegah beli-jual-beli beruntun)
        cool = now + self.step
        if pnl > 0:
            self.state.wins += 1
        else:
            cool = max(cool, now + self.risk["cooldown_minutes_after_loss"] * 60)
        self.state.cooldown_until[pair] = max(self.state.cooldown_until.get(pair, 0), cool)
        self.store.log_trade(pair=pair, side="SELL", qty=fill.qty, price=fill.avg_price, idr=fill.net_idr,
                             fee_idr=fill.fee_idr, pnl_idr=pnl, pnl_pct=pnl_pct, reason=reason,
                             order_id=fill.order_id)
        self.store.save(self.state)
        held_h = (now - pos.opened_at) / 3600
        self.notify.send(
            f"JUAL {pair.upper()} ({self.broker.mode}) — {reason}\n"
            f"Qty {fill.qty:.8g} @ {px(fill.avg_price)} (beli {px(pos.entry_price)})\n"
            f"Hasil {rp(fill.net_idr)} | PnL {'+' if pnl >= 0 else ''}{rp(pnl)} ({pnl_pct:+.2f}%) | {held_h:.1f} jam\n"
            f"PnL hari ini: {rp(self.state.daily_pnl[day])}")

        limit = self.risk["daily_loss_limit_idr"]
        if limit and self.state.daily_pnl[day] <= -limit and self.state.halted_day != day:
            self.state.halted_day = day
            self.notify.send(f"Batas rugi harian {rp(limit)} tercapai. Tidak ada pembelian baru sampai besok "
                             f"(posisi terbuka tetap dijaga TP/SL).")
        return True

    def _sell_all(self, quotes) -> None:
        todo = [p for p, pos in self.state.positions.items() if not pos.dust]
        if not todo:
            return
        self.notify.send(f"Perintah SELLALL: menjual {len(todo)} posisi bot.", key="sellall", min_interval=300)
        for pair in todo:
            q = quotes.get(pair)
            if q and self._retry_after.get(pair, 0) <= self.clock():
                try:
                    self._close(pair, q, "manual_sellall")
                except Exception as e:
                    log.exception("SELLALL %s: %s", pair, e)
                    self._retry_after[pair] = self.clock() + 30

    # ------------------------------------------------------------------ misc
    def _handle_api_error(self, action: str, pair: str, e: IndodaxError) -> None:
        if e.code in AUTH_ERRORS:
            self.entries_blocked = f"masalah API key/izin: {e}"
            self.notify.send(f"Gagal {action} {pair}: {e}\nPembelian baru dihentikan. Periksa API key, "
                             f"izin Trade, dan IP whitelist VPS.", key="auth", min_interval=1800)
        elif e.code == -1021:
            self.notify.send("Jam VPS tidak sinkron dengan server — menyinkronkan ulang.", key="time", min_interval=600)
            api = getattr(self.broker, "api", None)
            if api:
                api.sync_time()
        else:
            self.notify.send(f"Gagal {action} {pair}: {e}", key=f"err_{action}_{pair}", min_interval=600)

    def _day_rollover(self, now: float) -> None:
        day = today_wib(now)
        if day == self._day:
            return
        prev = self._day
        self._day = day
        pnl = self.state.daily_pnl.get(prev, 0.0)
        wr = (self.state.wins / self.state.trades_count * 100) if self.state.trades_count else 0
        self.notify.send(f"Ringkasan {prev}: PnL {rp(pnl)} | total terealisasi {rp(self.state.total_realized)} | "
                         f"{self.state.trades_count} transaksi, win rate {wr:.0f}% | "
                         f"posisi terbuka {len(self.state.positions)}")
        api = getattr(self.broker, "api", None)
        if api:
            api.sync_time()

    def _write_status(self, now: float, quotes) -> None:
        """Snapshot untuk dashboard web (dibaca oleh `python -m bot web`)."""
        try:
            pairs = {}
            for pair in self.pairs:
                q = quotes.get(pair)
                pairs[pair] = {
                    "last": q.last if q else None, "bid": q.bid if q else None, "ask": q.ask if q else None,
                    "spread_pct": q.spread_pct if q else None, "vol_idr": q.vol_idr if q else None,
                    "signal": self.last_signals.get(pair),
                    "cooldown_until": self.state.cooldown_until.get(pair, 0),
                }
            positions = {}
            for pair, pos in self.state.positions.items():
                stop, stop_reason, tp = self.exits_for(pair).levels(pos.entry_price, pos.highest)
                positions[pair] = {"stop": stop, "stop_reason": stop_reason, "take_profit": tp}
            paper_eq = None
            if self.broker.mode == "paper":
                paper_eq = self.broker.bal.get("idr", 0) + sum(
                    v * quotes[c + "idr"].last for c, v in self.broker.bal.items()
                    if c != "idr" and c + "idr" in quotes)
            self.store.write_status({
                "ts": now, "mode": self.broker.mode, "timeframe": self.tf,
                "poll_seconds": self.cfg["poll_seconds"], "entries_blocked": self.entries_blocked,
                "pairs": pairs, "levels": positions, "paper_equity": paper_eq,
                "paper_balances": dict(self.broker.bal) if self.broker.mode == "paper" else None,
            })
        except Exception as e:  # dashboard tidak boleh mengganggu trading
            log.debug("gagal menulis status dashboard: %s", e)

    def _heartbeat(self, now: float, quotes) -> None:
        if now - self._last_heartbeat < 1800:
            return
        self._last_heartbeat = now
        parts = []
        for pair, pos in self.state.positions.items():
            q = quotes.get(pair)
            if q:
                chg = (q.last / pos.entry_price - 1) * 100
                parts.append(f"{pair} {chg:+.2f}%")
        msg = f"heartbeat: {len(self.state.positions)} posisi [{', '.join(parts)}] | PnL hari ini " \
              f"{rp(self.state.daily_pnl.get(today_wib(now), 0.0))}"
        if self.broker.mode == "paper":
            eq = self.broker.bal.get("idr", 0) + sum(
                v * quotes[c + "idr"].last for c, v in self.broker.bal.items() if c != "idr" and c + "idr" in quotes)
            msg += f" | ekuitas simulasi {rp(eq)}"
        log.info(msg)
