"""Optimizer: modul background yang memantau, mengevaluasi, lalu menyetel ulang strategi.

Alur tiap putaran (default 1x per 24 jam, bisa dipicu manual dari halaman Optimasi):

1. Evaluasi perubahan sebelumnya. Jika perubahan terakhir dari optimizer sudah menghasilkan
   cukup transaksi (eval_trades) dan hasilnya lebih buruk dari sebelum perubahan, nilai lama
   dipulihkan otomatis (rollback).
2. Baca jurnal transaksi bot (MFE/MAE, alasan keluar, PnL per pair) sebagai petunjuk.
3. Per pair aktif: ambil candle `lookback_days` hari terakhir, backtest kombinasi stop loss /
   take profit / trailing di sekitar nilai sekarang (maks. ±max_step_pct), dengan pembagian
   data latih 70% dan uji 30%. Kandidat hanya diterima jika lebih baik dari pengaturan sekarang
   di KEDUA bagian data (mengurangi risiko overfitting) dan selisihnya berarti (≥1% modal).
4. Simpan hasil ke tabel optimizer_runs. Mode "auto" menerapkan langsung (di LIVE hanya jika
   apply_in_live diaktifkan); mode "suggest" menunggu persetujuan di dashboard.

Batasan keamanan: nilai selalu dalam BOUNDS, perubahan per putaran dibatasi max_step_pct,
optimizer tidak pernah menyentuh mode, modal, kredensial, atau pengaturan risiko harian.
Backtest bagus tidak menjamin profit ke depan.
"""
from __future__ import annotations

import copy
import itertools
import logging
import statistics
import threading
import time
from typing import Callable, Dict, List, Optional

from .analysis import round_trips, stats
from .backtest import backtest
from .settings import pair_cfg
from .strategy import TrendStrategy

log = logging.getLogger("bot.optimizer")

TUNED = ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct", "trailing_activation_pct")
BOUNDS = {"stop_loss_pct": (0.8, 10.0), "take_profit_pct": (1.5, 20.0),
          "trailing_stop_pct": (0.5, 6.0), "trailing_activation_pct": (0.8, 10.0)}
LABELS = {"stop_loss_pct": "Stop loss", "take_profit_pct": "Take profit",
          "trailing_stop_pct": "Trailing stop", "trailing_activation_pct": "Aktivasi trailing"}
TRAIN_RATIO = 0.7
MIN_IMPROVE_PCT = 1.0        # kandidat harus unggul ≥1% dari modal per transaksi
MIN_N_TRAIN, MIN_N_TEST = 5, 3
ROLLBACK_MARGIN = 0.3        # rata-rata %/transaksi turun lebih dari ini -> rollback
LOCK_TTL = 1800
META_LAST = "optimizer_last_run"
META_LOCK = "optimizer_lock"


def _r1(x: float) -> float:
    return round(float(x) + 1e-9, 1)


def _clamp(p: str, v: float) -> float:
    lo, hi = BOUNDS[p]
    return min(hi, max(lo, v))


def _pctl(xs: List[float], q: float) -> Optional[float]:
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def _sg(x: float, d: int = 1) -> str:
    """Angka bertanda gaya Indonesia: +1,25 / −0,50."""
    s = f"{abs(x):.{d}f}".replace(".", ",")
    return ("+" if x > 0 else "−" if x < 0 else "") + s


def _rp(x: float) -> str:
    return ("−" if x < 0 else "+" if x > 0 else "") + "Rp" + f"{abs(x):,.0f}".replace(",", ".")


def _fmt(v) -> str:
    return "mati" if not v else f"{v:g}%".replace(".", ",")


class Optimizer:
    def __init__(self, db, svc, market, store_for: Callable, notify: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.time):
        self.db = db
        self.svc = svc
        self.market = market          # objek Market (info + candles_range)
        self.store_for = store_for    # mode -> DbStateStore
        self.notify = notify or (lambda text: None)
        self.clock = clock
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._blocked: set = set()

    # ------------------------------------------------------------------ status
    @property
    def running(self) -> bool:
        if self._lock.locked():
            return True
        v = self.db.get_meta(META_LOCK)
        try:
            return bool(v) and self.clock() - float(v) < LOCK_TTL
        except ValueError:
            return False

    def last_run_ts(self) -> Optional[float]:
        v = self.db.get_meta(META_LAST)
        try:
            return float(v) if v else None
        except ValueError:
            return None

    def next_run_ts(self, cfg: dict) -> Optional[float]:
        o = cfg["optimizer"]
        if not o["enabled"]:
            return None
        last = self.last_run_ts()
        return (last or self.clock()) + o["interval_hours"] * 3600

    def trigger(self, user: str = "manual") -> bool:
        """Jalankan satu putaran di thread terpisah. False jika sedang berjalan."""
        if self.running or (self._worker and self._worker.is_alive()):
            return False
        self._worker = threading.Thread(target=self._safe_run, args=("manual", user), daemon=True,
                                        name="optimizer-manual")
        self._worker.start()
        return True

    def _safe_run(self, trigger, user):
        try:
            self.run_once(trigger, user)
        except Exception:
            log.exception("Optimizer gagal")

    # ------------------------------------------------------------------ putaran utama
    def run_once(self, trigger: str = "schedule", user: str = "optimizer") -> Optional[dict]:
        if not self._lock.acquire(blocking=False):
            return None
        try:
            v = self.db.get_meta(META_LOCK)
            if v:
                try:
                    if self.clock() - float(v) < LOCK_TTL:
                        log.info("Optimizer sedang dijalankan proses lain, dilewati.")
                        return None
                except ValueError:
                    pass
            self.db.set_meta(META_LOCK, str(self.clock()))
            try:
                return self._run(trigger, user)
            finally:
                self.db.set_meta(META_LOCK, "")
                self.db.set_meta(META_LAST, str(self.clock()))
        finally:
            self._lock.release()

    def _run(self, trigger: str, user: str) -> dict:
        cfg = self.svc.load()
        o = cfg["optimizer"]
        mode = cfg["mode"]
        now = self.clock()
        rows = round_trips(self.store_for(mode).read_trades())
        notes: List[str] = []

        # 1) evaluasi perubahan sebelumnya
        pending = self._evaluate_previous(cfg, rows, notes)

        data = {"trigger": trigger, "user": user, "notes": notes, "proposals": [],
                "journal": _jstats(rows), "lookback_days": o["lookback_days"], "timeframe": cfg["timeframe"]}
        if any(n.endswith("pengaturan lama dipulihkan.") for n in notes):
            data["summary"] = "Perubahan sebelumnya dikembalikan; optimasi baru ditunda ke putaran berikutnya."
            return self._save(now, mode, "skipped", data)
        if pending:
            data["summary"] = (f"Menunggu evaluasi perubahan #{pending['id']} "
                               f"({pending['after_n']}/{o['eval_trades']} transaksi setelah perubahan).")
            return self._save(now, mode, "skipped", data)
        recent = [r for r in rows if (r["ts"] or 0) >= now - o["lookback_days"] * 86400]
        if len(recent) < o["min_trades"]:
            data["summary"] = (f"Data belum cukup: {len(recent)} transaksi selesai dalam {o['lookback_days']} hari "
                               f"terakhir (minimal {o['min_trades']}). Optimasi dilewati.")
            return self._save(now, mode, "skipped", data)

        # 2-3) cari pengaturan yang lebih baik per pair
        sections = self.svc.sections()
        pairs_before = copy.deepcopy(sections["pairs"])
        open_pos = set(self.store_for(mode).load().positions)
        self._blocked = self._rolled_back_values(now - o["lookback_days"] * 86400)
        proposals = []
        for pair in cfg["pairs"]:
            try:
                p = self._optimize_pair(cfg, pair, [r for r in recent if r["pair"] == pair], open_pos)
            except Exception as e:
                log.warning("Optimizer %s: %s", pair, e)
                notes.append(f"{pair.upper()}: gagal dianalisis ({e})")
                continue
            if p:
                proposals.append(p)
            else:
                notes.append(f"{pair.upper()}: pengaturan sekarang sudah yang terbaik di antara kandidat yang diuji.")
        disables = [p for p in proposals if p.get("disable")]
        if disables and len(disables) >= len(cfg["pairs"]):
            for p in disables:          # sisakan minimal satu pair aktif
                p["disable"] = False
            notes.append("Usulan menonaktifkan semua pair dibatalkan (minimal satu pair tetap aktif).")
            proposals = [p for p in proposals if p["changes"] or p.get("disable")]

        data["proposals"] = proposals
        data["pairs_before"] = pairs_before
        if not proposals:
            data["summary"] = "Tidak ada perubahan: tidak ditemukan pengaturan yang konsisten lebih baik."
            return self._save(now, mode, "no_change", data)

        data["pairs_after"] = self._apply_to_rows(pairs_before, proposals, cfg)
        data["summary"] = f"{len(proposals)} pair punya usulan perubahan: " + "; ".join(
            _describe(p) for p in proposals)
        # usulan lama yang belum diputuskan tidak relevan lagi
        for old in self.db.list_opt_runs(50, status="proposed"):
            self.db.update_opt_run(old["id"], "superseded", old["data"])

        auto = o["mode"] == "auto" and (mode == "paper" or o["apply_in_live"])
        run = self._save(now, mode, "proposed", data)
        if auto:
            run = self.apply_run(run["id"], user="optimizer", _auto=True)
            self.notify("🤖 Optimizer menerapkan perubahan strategi:\n" + data["summary"])
        else:
            why = "mode LIVE (penerapan otomatis dimatikan)" if o["mode"] == "auto" else "mode usulan"
            run["data"]["notes"].append(f"Tidak diterapkan otomatis karena {why}. Setujui di halaman Optimasi.")
            self.db.update_opt_run(run["id"], "proposed", run["data"])
            self.notify("🤖 Optimizer punya usulan (belum diterapkan):\n" + data["summary"])
        return run

    def _save(self, ts, mode, status, data) -> dict:
        rid = self.db.add_opt_run(ts, mode, status, data)
        log.info("Optimizer #%d %s: %s", rid, status, data.get("summary", ""))
        return self.db.get_opt_run(rid)

    # ------------------------------------------------------------------ evaluasi & rollback
    def _evaluate_previous(self, cfg, rows, notes) -> Optional[dict]:
        """Kembalikan info jika perubahan terakhir masih menunggu cukup transaksi."""
        o = cfg["optimizer"]
        for run in self.db.list_opt_runs(20, status="applied"):
            d = run["data"]
            if d.get("evaluation") or run["trading_mode"] != cfg["mode"]:
                continue
            at = d.get("applied_at") or run["ts"]
            pairs = {p["pair"] for p in d.get("proposals", []) if p.get("changes")}
            rel = [r for r in rows if r["pair"] in pairs]
            after = [r for r in rel if (r.get("entry_ts") or r["ts"] or 0) >= at]
            if len(after) < o["eval_trades"]:
                if self.clock() - at > o["lookback_days"] * 86400:
                    d["evaluation"] = {"ts": self.clock(), "result": "insufficient", "after": stats(after),
                                       "text": "Tidak cukup transaksi untuk dievaluasi; dianggap selesai."}
                    self.db.update_opt_run(run["id"], run["status"], d)
                    continue
                return {"id": run["id"], "after_n": len(after)}
            after = after[:o["eval_trades"]]
            before = [r for r in rel if (r["ts"] or 0) < at][-o["eval_trades"]:]
            sa, sb = stats(after), stats(before)
            worse = sa["pnl"] < 0 and sa["avg_pct"] < (sb["avg_pct"] if sb["n"] else 0) - ROLLBACK_MARGIN
            ev = {"ts": self.clock(), "after": sa, "before": sb}
            txt = (f"Setelah perubahan #{run['id']}: {sa['n']} transaksi, rata-rata {_sg(sa['avg_pct'], 2)}%/trx "
                   f"(sebelumnya {_sg(sb['avg_pct'], 2)}%/trx dari {sb['n']} transaksi)." if sb["n"] else
                   f"Setelah perubahan #{run['id']}: {sa['n']} transaksi, rata-rata {_sg(sa['avg_pct'], 2)}%/trx.")
            if worse and o["rollback"]:
                ev["result"] = "rolled_back"
                d["evaluation"] = {**ev, "text": txt + " Hasil memburuk → dikembalikan otomatis."}
                self.db.update_opt_run(run["id"], run["status"], d)
                self.rollback_run(run["id"], user="optimizer", reason="evaluasi otomatis")
                notes.append(txt + " Hasil memburuk, pengaturan lama dipulihkan.")
                self.notify(f"🤖 Optimizer: perubahan #{run['id']} dibatalkan (hasil memburuk). {txt}")
            else:
                ev["result"] = "worse" if worse else "ok"
                d["evaluation"] = {**ev, "text": txt + (" Hasil memburuk (rollback otomatis dimatikan)."
                                                       if worse else " Perubahan dipertahankan.")}
                self.db.update_opt_run(run["id"], run["status"], d)
                notes.append(d["evaluation"]["text"])
        return None

    def _rolled_back_values(self, since: float) -> set:
        out = set()
        for run in self.db.list_opt_runs(100, status="rolled_back"):
            if run["ts"] < since:
                continue
            for p in run["data"].get("proposals", []):
                for k, ch in (p.get("changes") or {}).items():
                    out.add((p["pair"], k, ch["new"]))
        return out

    # ------------------------------------------------------------------ pencarian per pair
    def _candles(self, cfg, pair):
        info = self.market.info(pair)
        end = int(self.clock())
        start = end - cfg["optimizer"]["lookback_days"] * 86400
        return self.market.candles_range(info.symbol, cfg["timeframe"], start, end, now=end)

    def _optimize_pair(self, cfg, pair, jrows, open_pos) -> Optional[dict]:
        o = cfg["optimizer"]
        step = o["max_step_pct"] / 100
        pc = pair_cfg(cfg, pair)
        cur = {p: float(pc["exits"][p]) for p in TUNED}
        stake = float(pc["risk"]["idr_per_trade"])
        candles = self._candles(cfg, pair)
        warm = TrendStrategy(cfg).warmup
        if len(candles) < warm + 50:
            raise ValueError(f"candle terlalu sedikit ({len(candles)})")

        hints = _journal_hints(jrows)
        grid = {}
        for p in TUNED:
            c = cur[p]
            if c <= 0:              # fitur yang dimatikan tetap mati
                grid[p] = [0.0]
                continue
            lo, hi = c * (1 - step), c * (1 + step)
            vals = {c, _clamp(p, _r1(lo)), _clamp(p, _r1(hi)), _clamp(p, _r1((c + lo) / 2)),
                    _clamp(p, _r1((c + hi) / 2))}
            if p in hints:
                vals.add(_clamp(p, _r1(min(hi, max(lo, hints[p])))))
            grid[p] = sorted(vals)

        cut = int(len(candles) * TRAIN_RATIO)
        train_c, test_c = candles[:cut], candles[max(0, cut - warm):]

        def score(params):
            c2 = copy.deepcopy(cfg)
            row = dict(c2["pair_settings"].get(pair) or {"pair": pair, "enabled": True})
            row.update(params)
            c2["pair_settings"] = {pair: row}
            r1, r2 = backtest(c2, pair, train_c), backtest(c2, pair, test_c)
            return {"train": r1.total / stake * 100, "test": r2.total / stake * 100,
                    "n_train": r1.n, "n_test": r2.n, "win_rate": (r1.win_rate * r1.n + r2.win_rate * r2.n)
                    / max(1, r1.n + r2.n)}

        base = score(cur)
        best, best_key = None, None
        for combo in itertools.product(*(grid[p] for p in TUNED)):
            params = dict(zip(TUNED, combo))
            if params == cur:
                continue
            if params["trailing_stop_pct"] and params["trailing_activation_pct"] \
                    and params["trailing_activation_pct"] < params["trailing_stop_pct"] * 0.5:
                continue
            s = score(params)
            if s["n_train"] < MIN_N_TRAIN or s["n_test"] < MIN_N_TEST:
                continue
            d_train, d_test = s["train"] - base["train"], s["test"] - base["test"]
            if d_train < MIN_IMPROVE_PCT or d_test <= 0 or s["test"] <= 0:
                continue
            # utamakan perbaikan yang konsisten; jika setara, pilih yang paling sedikit mengubah
            moved = sum(abs(params[p] - cur[p]) / cur[p] for p in TUNED if cur[p] > 0)
            key = min(d_train, d_test) + (d_train + d_test) * 0.1 - moved * 0.01
            if best_key is None or key > best_key:
                best, best_key = (params, s), key

        journal = _jstats(jrows)
        disable = False
        if (o["allow_disable_pairs"] and pair not in open_pos and journal["n"] >= 8 and journal["pnl"] < 0
                and (journal.get("profit_factor") or 0) < 0.8 and base["train"] < 0 and base["test"] < 0
                and best is None):
            disable = True
        if best is None and not disable:
            return None

        changes = {}
        reason = ""
        new_s = None
        if best:
            params, new_s = best
            raw = (cfg["pair_settings"].get(pair) or {})
            for p in TUNED:
                target = params[p]
                if cur[p] <= 0 or abs(target - cur[p]) < 1e-9:
                    continue
                lim = cur[p] * step
                new = _clamp(p, _r1(cur[p] + max(-lim, min(lim, target - cur[p]))))
                if (pair, p, new) in self._blocked:
                    continue          # nilai ini pernah dicoba lalu dikembalikan karena memburuk
                if abs(new - cur[p]) >= 0.05:
                    changes[p] = {"old": cur[p], "new": new, "raw_old": raw.get(p)}
            if not changes and not disable:
                return None
            reason = (f"Backtest {o['lookback_days']} hari: data latih {_sg(base['train'], 1)}% → {_sg(new_s['train'], 1)}%, "
                      f"data uji {_sg(base['test'], 1)}% → {_sg(new_s['test'], 1)}% (dari modal per transaksi).")
        if disable:
            reason = (f"Rugi di jurnal ({journal['n']} transaksi, {_rp(journal['pnl'])}) dan di backtest "
                      f"(latih {_sg(base['train'], 1)}%, uji {_sg(base['test'], 1)}%). Diusulkan dinonaktifkan.")
        return {"pair": pair, "changes": changes, "disable": disable, "reason": reason,
                "evidence": {"current": base, "candidate": new_s, "journal": journal, "hints": hints,
                             "candles": len(candles), "grid": grid}}

    # ------------------------------------------------------------------ terapkan / tolak / rollback
    @staticmethod
    def _apply_to_rows(rows, proposals, cfg) -> list:
        rows = copy.deepcopy(rows)
        by = {r["pair"]: r for r in rows}
        for p in proposals:
            r = by.get(p["pair"])
            if not r:
                continue
            for k, ch in p["changes"].items():
                r[k] = ch["new"]
            if p.get("disable"):
                r["enabled"] = False
        return rows

    def apply_run(self, run_id: int, user: str, _auto: bool = False) -> dict:
        run = self.db.get_opt_run(run_id)
        if not run:
            raise ValueError("hasil optimasi tidak ditemukan")
        if run["status"] != "proposed":
            raise ValueError(f"hanya usulan berstatus 'proposed' yang bisa diterapkan (status: {run['status']})")
        cfg = self.svc.load()
        if run["trading_mode"] != cfg["mode"]:
            raise ValueError("mode trading sudah berubah sejak usulan dibuat; jalankan optimasi ulang")
        d = run["data"]
        rows = copy.deepcopy(self.svc.sections()["pairs"])
        by = {r["pair"]: r for r in rows}
        for p in d["proposals"]:
            r = by.get(p["pair"])
            if r is None:
                raise ValueError(f"pair {p['pair']} sudah dihapus; jalankan optimasi ulang")
            eff = pair_cfg(cfg, p["pair"])["exits"]
            for k, ch in p["changes"].items():
                if abs(float(eff[k]) - float(ch["old"])) > 1e-9:
                    raise ValueError(f"{p['pair'].upper()}: {LABELS[k]} sudah diubah sejak usulan dibuat "
                                     "(jalankan optimasi ulang)")
                r[k] = ch["new"]
            if p.get("disable"):
                r["enabled"] = False
        self.svc.update("pairs", rows, user=user)
        self.db.audit(user, "optimizer_apply", f"#{run_id}: {d.get('summary', '')}"[:1000])
        d["applied_at"] = self.clock()
        d["applied_by"] = user
        d["auto"] = _auto
        self.db.update_opt_run(run_id, "applied", d)
        return self.db.get_opt_run(run_id)

    def reject_run(self, run_id: int, user: str) -> dict:
        run = self.db.get_opt_run(run_id)
        if not run or run["status"] != "proposed":
            raise ValueError("hanya usulan berstatus 'proposed' yang bisa ditolak")
        d = run["data"]
        d["rejected_by"], d["rejected_at"] = user, self.clock()
        self.db.update_opt_run(run_id, "rejected", d)
        self.db.audit(user, "optimizer_reject", f"#{run_id}")
        return self.db.get_opt_run(run_id)

    def rollback_run(self, run_id: int, user: str, reason: str = "manual") -> dict:
        run = self.db.get_opt_run(run_id)
        if not run or run["status"] != "applied":
            raise ValueError("hanya perubahan berstatus 'applied' yang bisa dikembalikan")
        d = run["data"]
        rows = copy.deepcopy(self.svc.sections()["pairs"])
        by = {r["pair"]: r for r in rows}
        restored, skipped = [], []
        for p in d["proposals"]:
            r = by.get(p["pair"])
            if r is None:
                continue
            for k, ch in p["changes"].items():
                if r.get(k) is not None and abs(float(r[k]) - float(ch["new"])) < 1e-9:
                    r[k] = ch.get("raw_old")
                    restored.append(f"{p['pair'].upper()} {LABELS[k]}")
                else:
                    skipped.append(f"{p['pair'].upper()} {LABELS[k]} (sudah diubah manual)")
            if p.get("disable") and not r["enabled"]:
                r["enabled"] = True
                restored.append(f"{p['pair'].upper()} diaktifkan lagi")
        if restored:
            self.svc.update("pairs", rows, user=user)
        d["rollback"] = {"ts": self.clock(), "by": user, "reason": reason, "restored": restored, "skipped": skipped}
        self.db.update_opt_run(run_id, "rolled_back", d)
        self.db.audit(user, "optimizer_rollback", f"#{run_id} ({reason}): " + ", ".join(restored or ["-"]))
        return self.db.get_opt_run(run_id)

    # ------------------------------------------------------------------ untuk API
    def overview(self, limit: int = 30) -> dict:
        cfg = self.svc.load()
        runs = self.db.list_opt_runs(limit)
        for r in runs:   # bukti detail (grid) tidak perlu dikirim ke browser
            for p in r["data"].get("proposals", []):
                p.get("evidence", {}).pop("grid", None)
            r["data"].pop("pairs_before", None)
            r["data"].pop("pairs_after", None)
        return {"settings": cfg["optimizer"], "trading_mode": cfg["mode"], "running": self.running,
                "last_run": self.last_run_ts(), "next_run": self.next_run_ts(cfg), "runs": runs,
                "bounds": BOUNDS, "now": self.clock()}


def _jstats(rows) -> dict:
    s = stats(rows)
    if not s["n"]:
        return {"n": 0, "pnl": 0.0}
    return {k: s[k] for k in ("n", "win_rate", "pnl", "avg_pct", "profit_factor")}


def _journal_hints(rows) -> Dict[str, float]:
    """Petunjuk dari MFE/MAE jurnal: SL yang tidak memotong pemenang, aktivasi trailing yang
    mengunci profit yang tadinya sempat ada pada transaksi yang akhirnya rugi."""
    out = {}
    rich = [r for r in rows if r.get("mae") is not None and r.get("mfe") is not None]
    wins = [abs(r["mae"]) for r in rich if r["pnl"] > 0]
    if len(wins) >= 8:
        out["stop_loss_pct"] = _pctl(wins, 0.9) + 0.5
    gave_back = [r["mfe"] for r in rich if r["pnl"] <= 0 and r["mfe"] > 0.5]
    if len(gave_back) >= 5:
        out["trailing_activation_pct"] = statistics.median(gave_back) * 0.8
    return {k: round(v, 2) for k, v in out.items()}


def _describe(p) -> str:
    parts = [f"{LABELS[k]} {_fmt(ch['old'])} → {_fmt(ch['new'])}" for k, ch in p["changes"].items()]
    if p.get("disable"):
        parts.append("nonaktifkan")
    return f"{p['pair'].upper()}: " + ", ".join(parts)


class OptimizerThread(threading.Thread):
    """Penjadwal: cek tiap menit apakah sudah waktunya menjalankan optimizer."""

    def __init__(self, opt: Optimizer, check_every: float = 60.0, first_delay: float = 600.0):
        super().__init__(daemon=True, name="optimizer")
        self.opt = opt
        self.check_every = check_every
        self.first_delay = first_delay
        self.stop_event = threading.Event()

    def run(self):
        if self.opt.last_run_ts() is None:   # pertama kali: jalan ±10 menit setelah start
            try:
                cfg = self.opt.svc.load()
                self.opt.db.set_meta(META_LAST, str(self.opt.clock() - cfg["optimizer"]["interval_hours"] * 3600
                                                    + self.first_delay))
            except Exception:
                log.exception("Optimizer: gagal inisialisasi jadwal")
        while not self.stop_event.wait(self.check_every):
            try:
                cfg = self.opt.svc.load()
                nxt = self.opt.next_run_ts(cfg)
                if nxt is not None and self.opt.clock() >= nxt and not self.opt.running:
                    self.opt.run_once("schedule")
            except Exception:
                log.exception("Optimizer: putaran terjadwal gagal")
