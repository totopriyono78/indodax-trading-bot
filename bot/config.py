"""Memuat konfigurasi dari config.yaml dan rahasia dari file .env."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS = {
    "mode": "paper",
    "pairs": ["btcidr", "ethidr", "dogeidr"],
    "timeframe": "15",
    "poll_seconds": 20,
    "candles_lookback": 300,
    "data_dir": "data",
    "strategy": {
        "ema_fast": 9,
        "ema_slow": 21,
        "ema_trend": 100,
        "rsi_period": 14,
        "rsi_min": 50.0,
        "rsi_max": 70.0,
        "require_cross": True,
        "cross_lookback": 3,
        "exit_on_trend_reversal": True,
    },
    "exits": {
        "take_profit_pct": 4.0,
        "stop_loss_pct": 2.5,
        "trailing_stop_pct": 1.5,
        "trailing_activation_pct": 2.0,
        "max_hold_hours": 72,
    },
    "risk": {
        "idr_per_trade": 100000,
        "max_open_positions": 3,
        "max_total_exposure_idr": 300000,
        "min_idr_reserve": 0,
        "daily_loss_limit_idr": 50000,
        "cooldown_minutes_after_loss": 120,
        "max_spread_pct": 0.6,
        "min_24h_volume_idr": 1_000_000_000,
    },
    "fees": {
        "buy_pct": 0.30,
        "sell_pct": 0.51,
    },
    "paper": {
        "starting_idr": 1_000_000,
        "slippage_pct": 0.10,
    },
    "telegram": {
        "enabled": False,
    },
    "optimizer": {
        "enabled": True,            # evaluasi berkala di background
        "mode": "auto",             # "suggest" = hanya usulan, "auto" = terapkan otomatis
        "apply_in_live": False,     # di mode LIVE hanya usulan, kecuali ini diaktifkan
        "interval_hours": 24,
        "min_trades": 20,           # minimal transaksi selesai sebelum mengubah pengaturan
        "lookback_days": 30,        # data candle untuk uji ulang (backtest) usulan
        "max_step_pct": 25,         # perubahan maksimum per langkah (relatif terhadap nilai sekarang)
        "rollback": True,           # kembalikan otomatis jika hasil sesudah perubahan memburuk
        "eval_trades": 15,          # jumlah transaksi sesudah perubahan sebelum dinilai
        "allow_disable_pairs": False,
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 8080,
        "allow_control": True,
    },
}

TIMEFRAMES = {"1": 60, "15": 900, "30": 1800, "60": 3600, "240": 14400,
              "1D": 86400, "3D": 259200, "1W": 604800}


class ConfigError(Exception):
    pass


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_env(path: str | os.PathLike = ".env") -> None:
    """Parser .env sederhana (KEY=VALUE). Tidak menimpa variabel yang sudah ada."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


def load_config(path: str | os.PathLike = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"File konfigurasi {p} tidak ditemukan. Salin config.example.yaml ke config.yaml.")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = _merge(DEFAULTS, raw)
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    errs = []
    if cfg["mode"] not in ("paper", "live"):
        errs.append("mode harus 'paper' atau 'live'")
    if cfg["timeframe"] not in TIMEFRAMES:
        errs.append(f"timeframe harus salah satu dari {list(TIMEFRAMES)}")
    for p in cfg["pairs"]:
        if not isinstance(p, str) or not p.endswith("idr"):
            errs.append(f"pair '{p}' harus berformat seperti 'btcidr' (market IDR)")
    s = cfg["strategy"]
    if not (s["ema_fast"] < s["ema_slow"]):
        errs.append("strategy.ema_fast harus lebih kecil dari ema_slow")
    if s["rsi_min"] >= s["rsi_max"]:
        errs.append("strategy.rsi_min harus lebih kecil dari rsi_max")
    e = cfg["exits"]
    if e["stop_loss_pct"] <= 0:
        errs.append("exits.stop_loss_pct wajib > 0 (bot tanpa stop loss tidak diizinkan)")
    if e["take_profit_pct"] < 0 or e["trailing_stop_pct"] < 0:
        errs.append("take_profit_pct / trailing_stop_pct tidak boleh negatif")
    if e["take_profit_pct"] == 0 and e["trailing_stop_pct"] == 0 and not s["exit_on_trend_reversal"]:
        errs.append("minimal satu cara ambil untung harus aktif (take profit, trailing, atau trend reversal)")
    r = cfg["risk"]
    if r["idr_per_trade"] < 25000:
        errs.append("risk.idr_per_trade minimal Rp25.000 (batas minimum order Indodax Pro)")
    if r["max_total_exposure_idr"] < r["idr_per_trade"]:
        errs.append("risk.max_total_exposure_idr harus >= idr_per_trade")
    if cfg["candles_lookback"] < s["ema_trend"] + 20:
        errs.append("candles_lookback harus minimal ema_trend + 20")
    o = cfg.get("optimizer") or {}
    if o:
        if o.get("mode") not in ("suggest", "auto"):
            errs.append("optimizer.mode harus 'suggest' atau 'auto'")
        for k, lo, hi in (("interval_hours", 1, 168), ("min_trades", 5, 1000), ("lookback_days", 7, 90),
                          ("max_step_pct", 5, 100), ("eval_trades", 5, 500)):
            v = o.get(k)
            if not isinstance(v, (int, float)) or not lo <= v <= hi:
                errs.append(f"optimizer.{k} harus antara {lo} dan {hi}")
    if errs:
        raise ConfigError("Konfigurasi tidak valid:\n - " + "\n - ".join(errs))


def secrets() -> dict:
    return {
        "api_key": os.environ.get("INDODAX_API_KEY", "").strip(),
        "secret_key": os.environ.get("INDODAX_SECRET_KEY", "").strip(),
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
    }
