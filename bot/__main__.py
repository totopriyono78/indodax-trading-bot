"""Titik masuk CLI.

  python -m bot check                 cek koneksi, pair, API key & saldo
  python -m bot run                   jalankan bot (mode sesuai config: paper / live)
  python -m bot status                tampilkan posisi & PnL dari catatan bot
  python -m bot backtest --days 60    uji strategi dengan data historis
  python -m bot backtest --days 90 --sweep   bandingkan kombinasi TP/SL/trailing
  python -m bot web                   dashboard web untuk memantau bot
  python -m bot pause | resume        hentikan / lanjutkan pembelian baru
  python -m bot sellall               minta bot yang sedang berjalan menjual semua posisinya
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .backtest import backtest, format_result, sweep
from .broker import LiveBroker, PaperBroker
from .client import IndodaxError, PrivateClientV2, PublicClient
from .config import ConfigError, TIMEFRAMES, load_config, load_env, secrets
from .engine import Engine, px, rp
from .market import Market
from .notifier import Notifier
from .state import StateStore, fmt_time, today_wib


def setup_logging(data_dir: str, verbose: bool = False) -> None:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = RotatingFileHandler(Path(data_dir) / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [sh, fh]
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def make_private(public: PublicClient, required: bool = True):
    s = secrets()
    if not s["api_key"] or not s["secret_key"]:
        if required:
            raise SystemExit("INDODAX_API_KEY / INDODAX_SECRET_KEY belum diisi di file .env")
        return None
    api = PrivateClientV2(s["api_key"], s["secret_key"], public)
    api.sync_time()
    return api


def cmd_check(cfg, args):
    public = PublicClient()
    market = Market(public)
    print("== Koneksi ke Indodax ==")
    t0 = time.time()
    st = public.server_time()
    print(f"  OK — waktu server {fmt_time(st / 1000)} WIB, selisih jam VPS {st - t0 * 1000:+.0f} ms")
    if abs(st - t0 * 1000) > 1000:
        print("  ! Jam VPS meleset > 1 detik. Aktifkan sinkronisasi waktu: sudo timedatectl set-ntp true")

    print("\n== Pair ==")
    pairs = market.load_pairs()
    quotes = market.quotes()
    for p in cfg["pairs"]:
        if p not in pairs:
            print(f"  X {p}: tidak ada di Indodax")
            continue
        info, q = pairs[p], quotes.get(p)
        ok = "OK" if cfg["risk"]["idr_per_trade"] >= info.min_idr else "X  (idr_per_trade < minimum!)"
        line = f"  {p:<10} min order {rp(info.min_idr):>12}  {ok}"
        if q:
            line += f" | harga {px(q.last)} | spread {q.spread_pct:.2f}% | vol 24j {rp(q.vol_idr)}"
            if q.spread_pct > cfg["risk"]["max_spread_pct"] or q.vol_idr < cfg["risk"]["min_24h_volume_idr"]:
                line += "  (saat ini tidak lolos filter spread/volume)"
        print(line)

    print("\n== API key (TAPI v2) ==")
    api = make_private(public, required=False)
    if not api:
        print("  (belum diisi — cukup untuk mode paper & backtest)")
    else:
        try:
            acc = api.account()
            print(f"  OK — uid {acc.get('uid')} | canTrade={acc.get('canTrade')} | canWithdraw={acc.get('canWithdraw')}")
            if acc.get("canWithdraw"):
                print("  ! Akun/key mengizinkan withdraw. Untuk bot, buat key TANPA izin withdraw.")
            for b in acc.get("balances", []):
                if float(b.get("free") or 0) + float(b.get("locked") or 0) > 0:
                    print(f"    {b['asset']:<6} free {b['free']}  locked {b['locked']}")
            oo = api.open_orders()
            print(f"  Order terbuka: {len(oo) if isinstance(oo, list) else oo}")
        except IndodaxError as e:
            print(f"  X Gagal: {e}")
            if e.code in (-1002, -2014):
                print("    Pastikan ini key TAPI v2 (bukan key TAPI lama).")
            if e.code == -2015:
                print("    Periksa izin key dan IP whitelist (IP publik VPS: `curl -4 ifconfig.me`).")

    print("\n== Telegram ==")
    s = secrets()
    n = Notifier(s["telegram_token"], s["telegram_chat_id"], cfg["telegram"]["enabled"])
    if n.enabled:
        n.send("Tes notifikasi dari bot Indodax ✅")
        print("  Pesan tes dikirim — cek Telegram Anda.")
    else:
        print("  (nonaktif)")
    print(f"\nMode saat ini: {cfg['mode'].upper()}")


def cmd_run(cfg, args):
    data_dir = Path(cfg["data_dir"])
    lock = open(data_dir / f"bot_{cfg['mode']}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("Bot dengan mode yang sama sudah berjalan (file lock terkunci).")

    public = PublicClient()
    market = Market(public)
    store = StateStore(cfg["data_dir"], cfg["mode"])
    s = secrets()
    notifier = Notifier(s["telegram_token"], s["telegram_chat_id"], cfg["telegram"]["enabled"],
                        prefix="[SIMULASI] " if cfg["mode"] == "paper" else "")
    if cfg["mode"] == "live":
        if not args.yes_live:
            print("MODE LIVE: bot akan memakai uang sungguhan.")
            if input("Ketik 'LIVE' untuk melanjutkan: ").strip() != "LIVE":
                raise SystemExit("Dibatalkan.")
        api = make_private(public)
        acc = api.account()
        if not acc.get("canTrade"):
            raise SystemExit("API key tidak punya izin trade (canTrade=false).")
        broker = LiveBroker(cfg, api)
    else:
        broker = PaperBroker(cfg, {})
    engine = Engine(cfg, market, broker, store, notifier)
    signal.signal(signal.SIGTERM, engine.stop)
    signal.signal(signal.SIGINT, engine.stop)
    logging.getLogger("bot").info("Memulai bot v%s mode=%s", __version__, cfg["mode"])
    engine.run()


def cmd_status(cfg, args):
    store = StateStore(cfg["data_dir"], cfg["mode"])
    st = store.load()
    print(f"Mode: {cfg['mode'].upper()}")
    flags = Path(cfg["data_dir"])
    if (flags / "PAUSE").exists():
        print("Status: PAUSE (tidak membuka posisi baru)")
    quotes = {}
    try:
        quotes = Market(PublicClient()).quotes()
    except Exception as e:
        print(f"(tidak bisa mengambil harga terkini: {e})")
    print(f"\nPosisi terbuka ({len(st.positions)}):")
    unreal = 0.0
    for pair, p in st.positions.items():
        q = quotes.get(pair)
        line = f"  {pair:<10} qty {p.qty:.8g} beli {px(p.entry_price)} modal {rp(p.cost_idr)} sejak {fmt_time(p.opened_at)}"
        if q:
            val = p.qty * q.bid
            unreal += val - p.cost_idr
            line += f" | sekarang {px(q.last)} ({(q.last / p.entry_price - 1) * 100:+.2f}%)"
        if p.dust:
            line += " [DUST]"
        print(line)
    today = today_wib()
    wr = st.wins / st.trades_count * 100 if st.trades_count else 0
    print(f"\nPnL belum terealisasi (perkiraan, sebelum fee jual): {rp(unreal)}")
    print(f"PnL hari ini: {rp(st.daily_pnl.get(today, 0.0))}")
    print(f"PnL terealisasi total: {rp(st.total_realized)} | {st.trades_count} transaksi | win rate {wr:.0f}%")
    if cfg["mode"] == "paper" and st.paper_balances:
        eq = st.paper_balances.get("idr", 0) + sum(
            v * quotes[c + "idr"].last for c, v in st.paper_balances.items() if c != "idr" and c + "idr" in quotes)
        start = cfg["paper"]["starting_idr"]
        print(f"Ekuitas simulasi: {rp(eq)} (awal {rp(start)}, {(eq / start - 1) * 100:+.2f}%)")
    recent = sorted(st.daily_pnl.items())[-7:]
    if recent:
        print("\nPnL 7 hari terakhir:")
        for d, v in recent:
            print(f"  {d}: {rp(v)}")


def cmd_backtest(cfg, args):
    if args.timeframe:
        cfg["timeframe"] = args.timeframe
    market = Market(PublicClient())
    market.load_pairs()
    pairs = args.pairs.split(",") if args.pairs else cfg["pairs"]
    step = TIMEFRAMES[cfg["timeframe"]]
    end = int(time.time())
    start = end - args.days * 86400 - step * (cfg["strategy"]["ema_trend"] + 10)
    data = {}
    for p in pairs:
        info = market.info(p)
        print(f"Mengambil candle {p} ({args.days} hari, TF {cfg['timeframe']})...", flush=True)
        data[p] = market.candles_range(info.symbol, cfg["timeframe"], start, end)
    print(f"\nFee beli {cfg['fees']['buy_pct']}% | fee jual {cfg['fees']['sell_pct']}% | "
          f"slippage {cfg['paper']['slippage_pct']}% | modal {rp(cfg['risk']['idr_per_trade'])}/trade\n")
    if args.sweep:
        print("Kombinasi terbaik di 70% data awal (TRAIN), lalu diuji di 30% data akhir (TEST):")
        print(f"  {'TP%':>4} {'SL%':>4} {'Trail%':>6} | {'PnL train':>12} {'n':>4} | {'PnL test':>12} {'n':>4}")
        for train, test, n1, n2, tp, sl, tr in sweep(cfg, data, top=args.top):
            print(f"  {tp:>4} {sl:>4} {tr:>6} | {rp(train):>12} {n1:>4} | {rp(test):>12} {n2:>4}")
        print("\nPilih kombinasi yang tetap positif di kolom TEST — hasil TRAIN saja mudah menipu (overfitting).")
        return
    total = 0.0
    for p, candles in data.items():
        r = backtest(cfg, p, candles)
        total += r.total
        print(format_result(r))
    print(f"\nTotal PnL semua pair: {rp(total)}")
    print("Catatan: backtest ≠ jaminan hasil ke depan. Uji juga di mode paper sebelum live.")


def cmd_flag(cfg, name, create=True, msg=""):
    f = Path(cfg["data_dir"]) / name
    if create:
        f.touch()
    else:
        f.unlink(missing_ok=True)
    if msg:
        print(msg)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bot", description="Bot trading Indodax")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    r = sub.add_parser("run")
    r.add_argument("--yes-live", action="store_true", help="lewati konfirmasi mode live (untuk systemd)")
    sub.add_parser("status")
    b = sub.add_parser("backtest")
    b.add_argument("--days", type=int, default=60)
    b.add_argument("--pairs", help="mis. btcidr,dogeidr (default: dari config)")
    b.add_argument("--timeframe", choices=list(TIMEFRAMES))
    b.add_argument("--sweep", action="store_true")
    b.add_argument("--top", type=int, default=10)
    w = sub.add_parser("web")
    w.add_argument("--host")
    w.add_argument("--port", type=int)
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("sellall")
    args = ap.parse_args(argv)

    load_env(args.env)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        raise SystemExit(str(e))
    setup_logging(cfg["data_dir"], args.verbose)

    if args.cmd == "check":
        cmd_check(cfg, args)
    elif args.cmd == "run":
        cmd_run(cfg, args)
    elif args.cmd == "status":
        cmd_status(cfg, args)
    elif args.cmd == "backtest":
        cmd_backtest(cfg, args)
    elif args.cmd == "web":
        from .web import serve
        serve(cfg, args.host, args.port)
    elif args.cmd == "pause":
        cmd_flag(cfg, "PAUSE", True, "Bot tidak akan membuka posisi baru. Posisi terbuka tetap dijaga TP/SL.")
    elif args.cmd == "resume":
        cmd_flag(cfg, "PAUSE", False, "Bot kembali boleh membuka posisi baru.")
    elif args.cmd == "sellall":
        cmd_flag(cfg, "PAUSE", True, "")
        cmd_flag(cfg, "SELLALL", True, "Permintaan jual semua dikirim; bot yang berjalan akan memprosesnya "
                                       "dalam beberapa detik (dan terus mencoba sampai semua terjual).\n"
                                       "Pembelian baru di-pause. Jalankan `python -m bot resume` untuk melanjutkan.")


if __name__ == "__main__":
    main()
