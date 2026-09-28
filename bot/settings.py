"""Pengaturan bot yang disimpan di database (dapat diubah dari halaman web).

Struktur konfigurasi yang dipakai bot tetap sama seperti sebelumnya (dict `cfg`), ditambah:
  cfg["pairs"]          daftar pair AKTIF, mis. ["btcidr", "pepeidr"]
  cfg["pair_settings"]  {pair: {"enabled", "stop_loss_pct", "take_profit_pct", ...}}; None = pakai pengaturan umum
"""
from __future__ import annotations

import copy
import math
import os
import re
import threading
from pathlib import Path
from typing import Optional

import yaml

from .config import DEFAULTS, ConfigError, _merge, validate
from .db import Database

SECTIONS = ("general", "strategy", "exits", "risk", "fees", "paper", "telegram", "dashboard", "pairs")
GENERAL_KEYS = ("mode", "timeframe", "poll_seconds", "candles_lookback")
PAIR_FIELDS = ("stop_loss_pct", "take_profit_pct", "trailing_stop_pct", "trailing_activation_pct", "idr_per_trade")
PAIR_RE = re.compile(r"^[a-z0-9]{1,20}idr$")
BOOT_DEFAULTS = {"data_dir": "data", "dashboard": {"host": "127.0.0.1", "port": 8080}}

# Tipe tiap field — dipakai untuk membersihkan input dari form web.
FIELD_TYPES = {
    "general": {"mode": str, "timeframe": str, "poll_seconds": int, "candles_lookback": int},
    "strategy": {"ema_fast": int, "ema_slow": int, "ema_trend": int, "rsi_period": int, "rsi_min": float,
                 "rsi_max": float, "require_cross": bool, "cross_lookback": int, "exit_on_trend_reversal": bool},
    "exits": {"take_profit_pct": float, "stop_loss_pct": float, "trailing_stop_pct": float,
              "trailing_activation_pct": float, "max_hold_hours": float},
    "risk": {"idr_per_trade": float, "max_open_positions": int, "max_total_exposure_idr": float,
             "min_idr_reserve": float, "daily_loss_limit_idr": float, "cooldown_minutes_after_loss": float,
             "max_spread_pct": float, "min_24h_volume_idr": float},
    "fees": {"buy_pct": float, "sell_pct": float},
    "paper": {"starting_idr": float, "slippage_pct": float},
    "telegram": {"enabled": bool},
    "dashboard": {"allow_control": bool},
}


def load_bootstrap(path: str = "config.yaml") -> dict:
    """config.yaml kini hanya untuk hal yang dibutuhkan sebelum database terbuka."""
    raw = {}
    p = Path(path)
    if not p.exists() and p.name == "config.yaml" and p.with_name("config.example.yaml").exists():
        p = p.with_name("config.example.yaml")   # mis. di Railway: config.yaml tidak ikut repo
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data_dir = (os.environ.get("DATA_DIR", "").strip() or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
                or raw.get("data_dir", "data"))
    boot = _merge(BOOT_DEFAULTS, {"data_dir": data_dir,
                                  "dashboard": {k: v for k, v in (raw.get("dashboard") or {}).items()
                                                if k in ("host", "port")}})
    boot["_raw"] = raw
    return boot


def _bool(v) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def _finite(v) -> float:
    x = float(v)
    if not math.isfinite(x):  # NaN / inf lolos semua perbandingan validasi (mis. stop loss NaN = tanpa SL)
        raise ValueError("bukan angka")
    return x


def _coerce(section: str, data: dict) -> dict:
    types = FIELD_TYPES[section]
    out = {}
    for k, v in (data or {}).items():
        if k not in types:
            continue
        t = types[k]
        try:
            if t is bool:
                out[k] = _bool(v)
            elif t is str:
                out[k] = str(v)
            else:
                out[k] = int(_finite(v)) if t is int else _finite(v)
        except (TypeError, ValueError):
            raise ConfigError(f"Nilai '{k}' tidak valid: {v!r}")
    return out


def _clean_pairs(rows) -> list:
    seen, out = set(), []
    for r in rows or []:
        pair = str(r.get("pair", "")).strip().lower().replace("_", "").replace("/", "")
        if not pair or pair in seen:
            continue
        seen.add(pair)
        row = {"pair": pair, "enabled": _bool(r.get("enabled", True))}
        for f in PAIR_FIELDS:
            v = r.get(f)
            if v in (None, ""):
                row[f] = None
            else:
                try:
                    row[f] = _finite(v)
                except (TypeError, ValueError):
                    raise ConfigError(f"{pair}: nilai {f} tidak valid ({v!r})")
        out.append(row)
    return out


def sections_from_flat(cfg: dict) -> dict:
    """Ubah cfg gaya lama (config.yaml) menjadi bagian-bagian pengaturan database."""
    ps = cfg.get("pair_settings") or {}
    pairs = []
    for p in cfg.get("pairs", []):
        row = {"pair": p, "enabled": True, **{f: None for f in PAIR_FIELDS}}
        row.update({k: v for k, v in (ps.get(p) or {}).items() if k in PAIR_FIELDS})
        pairs.append(row)
    for p, row in ps.items():
        if p not in cfg.get("pairs", []):
            pairs.append({"pair": p, "enabled": False, **{f: row.get(f) for f in PAIR_FIELDS}})
    return {
        "general": {k: cfg[k] for k in GENERAL_KEYS},
        "strategy": cfg["strategy"], "exits": cfg["exits"], "risk": cfg["risk"], "fees": cfg["fees"],
        "paper": cfg["paper"], "telegram": {"enabled": bool(cfg["telegram"].get("enabled"))},
        "dashboard": {"allow_control": bool(cfg["dashboard"].get("allow_control", True))},
        "pairs": _clean_pairs(pairs),
    }


def assemble(sections: dict, boot: dict) -> dict:
    """Gabungkan DEFAULTS + isi database + bootstrap menjadi cfg lengkap."""
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update({k: v for k, v in (sections.get("general") or {}).items() if k in GENERAL_KEYS})
    for s in ("strategy", "exits", "risk", "fees", "paper", "telegram"):
        cfg[s] = _merge(cfg[s], sections.get(s) or {})
    cfg["dashboard"] = _merge(cfg["dashboard"], sections.get("dashboard") or {})
    cfg["dashboard"].update(boot.get("dashboard", {}))
    cfg["data_dir"] = boot.get("data_dir", "data")
    rows = sections.get("pairs")
    if rows is None:
        rows = [{"pair": p, "enabled": True} for p in DEFAULTS["pairs"]]
    rows = _clean_pairs(rows)
    cfg["pair_settings"] = {r["pair"]: r for r in rows}
    cfg["pairs"] = [r["pair"] for r in rows if r["enabled"]]
    return cfg


def pair_cfg(cfg: dict, pair: str) -> dict:
    """cfg untuk satu pair: pengaturan umum ditimpa pengaturan khusus pair (jika diisi)."""
    row = (cfg.get("pair_settings") or {}).get(pair) or {}
    over = {k: row.get(k) for k in PAIR_FIELDS if row.get(k) is not None}
    if not over:
        return cfg
    c = copy.copy(cfg)
    c["exits"] = dict(cfg["exits"])
    c["risk"] = dict(cfg["risk"])
    for k, v in over.items():
        if k == "idr_per_trade":
            c["risk"]["idr_per_trade"] = v
        else:
            c["exits"][k] = v
    return c


def validate_all(cfg: dict) -> None:
    validate(cfg)
    errs = []
    if cfg["poll_seconds"] < 5:
        errs.append("interval cek harga minimal 5 detik")
    s = cfg["strategy"]
    if min(s["ema_fast"], s["ema_slow"], s["ema_trend"]) < 1 or s["rsi_period"] < 2 or s["cross_lookback"] < 1:
        errs.append("periode EMA / RSI / persilangan terlalu kecil")
    if cfg["exits"].get("max_hold_hours", 0) < 0:
        errs.append("batas lama tahan tidak boleh negatif")
    for pair, row in (cfg.get("pair_settings") or {}).items():
        if not PAIR_RE.match(pair):
            errs.append(f"pair '{pair}' harus market IDR (mis. btcidr)")
            continue
        c = pair_cfg(cfg, pair)
        e, r = c["exits"], c["risk"]
        if e["stop_loss_pct"] <= 0:
            errs.append(f"{pair}: stop loss wajib > 0")
        if e["stop_loss_pct"] >= 50:
            errs.append(f"{pair}: stop loss {e['stop_loss_pct']}% terlalu besar (maks. 50%)")
        if e["take_profit_pct"] < 0 or e["trailing_stop_pct"] < 0 or e["trailing_activation_pct"] < 0:
            errs.append(f"{pair}: take profit / trailing tidak boleh negatif")
        if e["trailing_stop_pct"] >= 100:
            errs.append(f"{pair}: trailing stop harus < 100%")
        if r["idr_per_trade"] < 25000:
            errs.append(f"{pair}: modal per transaksi minimal Rp25.000")
        if r["idr_per_trade"] > r["max_total_exposure_idr"]:
            errs.append(f"{pair}: modal per transaksi melebihi eksposur maksimum")
    if errs:
        raise ConfigError("Pengaturan tidak valid:\n - " + "\n - ".join(errs))


class SettingsService:
    def __init__(self, db: Database, boot: dict):
        self.db = db
        self.boot = boot
        self._lock = threading.Lock()  # validasi + simpan harus satu langkah (request web paralel)

    def ensure_seeded(self, yaml_cfg: Optional[dict] = None) -> bool:
        """Isi database dari config.yaml lama (atau default) saat pertama kali. True jika baru diisi."""
        if self.db.get_settings():
            return False
        raw = self.boot.get("_raw") or {}
        base = _merge(DEFAULTS, yaml_cfg if yaml_cfg is not None else raw)
        self.db.put_settings(sections_from_flat(base), user="init")
        return True

    def load(self) -> dict:
        return assemble(self.db.get_settings(), self.boot)

    def sections(self) -> dict:
        cfg = self.load()
        return sections_from_flat(cfg)

    def update(self, section: str, value, user: str = "") -> dict:
        if section not in SECTIONS:
            raise ConfigError(f"bagian pengaturan tidak dikenal: {section}")
        with self._lock:
            return self._update(section, value, user)

    def _update(self, section: str, value, user: str) -> dict:
        current = self.sections()
        if section == "pairs":
            new_val = _clean_pairs(value)
        else:
            new_val = _merge(current[section], _coerce(section, value))
        candidate = dict(current)
        candidate[section] = new_val
        cfg = assemble(candidate, self.boot)
        validate_all(cfg)
        self.db.put_settings({section: new_val}, user=user)
        return cfg

    # ---- kredensial
    def credentials(self) -> dict:
        """Kredensial dari database; fallback ke .env (kompatibel dengan versi lama)."""
        def get(name, env):
            try:
                v = self.db.get_secret(name)
            except Exception:
                v = None
            return (v or os.environ.get(env, "")).strip()
        return {
            "api_key": get("indodax_api_key", "INDODAX_API_KEY"),
            "secret_key": get("indodax_secret_key", "INDODAX_SECRET_KEY"),
            "telegram_token": get("telegram_token", "TELEGRAM_BOT_TOKEN"),
            "telegram_chat_id": get("telegram_chat_id", "TELEGRAM_CHAT_ID"),
        }


def import_env_secrets(db: Database) -> list:
    """Pindahkan kredensial dari .env ke database (terenkripsi). Kembalikan nama yang dipindah."""
    moved = []
    for name, env in (("indodax_api_key", "INDODAX_API_KEY"), ("indodax_secret_key", "INDODAX_SECRET_KEY"),
                      ("telegram_token", "TELEGRAM_BOT_TOKEN"), ("telegram_chat_id", "TELEGRAM_CHAT_ID")):
        v = os.environ.get(env, "").strip()
        if v and not db.secret_info().get(name):
            db.set_secret(name, v, user="init")
            moved.append(env)
    return moved
