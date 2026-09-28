"""Titik masuk CLI.

  python -m bot init                  siapkan database, kunci enkripsi, dan akun admin web
  python -m bot user add NAMA | passwd NAMA | list   kelola akun login web
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
import json
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
from .config import ConfigError, TIMEFRAMES, load_env
from .db import Database, SecretError, generate_master_key
from .settings import SettingsService, import_env_secrets, load_bootstrap, pair_cfg, validate_all
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


class Ctx:
    """Database + pengaturan yang dipakai semua perintah."""

    def __init__(self, args):
        load_env(args.env)
        self.env_path = Path(args.env)
        self.boot = load_bootstrap(args.config)
        setup_logging(self.boot["data_dir"], args.verbose)
        self.db = Database.from_env(self.boot["data_dir"])
        self.db.create_all()
        self.svc = SettingsService(self.db, self.boot)
        if self.svc.ensure_seeded():
            logging.getLogger("bot").info("Database pengaturan diisi dari config.yaml / nilai default.")
        self.last_good = Path(self.boot["data_dir"]) / "last_good_config.json"
        try:
            self.cfg = self.svc.load()
            validate_all(self.cfg)
            if getattr(args, "cmd", None) == "run":
                self.last_good.write_text(json.dumps(self.cfg), encoding="utf-8")
        except ConfigError as e:
            log = logging.getLogger("bot")
            cmd = getattr(args, "cmd", None)
            if cmd == "web":
                # dashboard tetap jalan agar pengaturan yang salah bisa diperbaiki dari web
                log.error("%s", e)
            elif cmd == "run" and self.last_good.exists():
                # Jangan biarkan posisi terbuka tanpa stop loss: jalan dengan pengaturan valid terakhir,
                # tanpa membuka posisi baru, sampai pengaturan diperbaiki.
                log.error("%s\nMemakai pengaturan valid terakhir; pembelian baru di-pause.", e)
                self.cfg = json.loads(self.last_good.read_text(encoding="utf-8"))
                (Path(self.boot["data_dir"]) / "PAUSE").touch()
            else:
                raise SystemExit(str(e))

    def creds(self) -> dict:
        return self.svc.credentials()


class DbConfigSource:
    """Dipakai engine untuk mendeteksi perubahan pengaturan / kredensial dari halaman web."""

    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        self.cfg_v = ctx.db.version("config_version")
        self.sec_v = ctx.db.version("secrets_version")

    def poll_config(self):
        v = self.ctx.db.version("config_version")
        if v == self.cfg_v:
            return None
        self.cfg_v = v
        cfg = self.ctx.svc.load()
        validate_all(cfg)
        return cfg

    def poll_credentials(self):
        v = self.ctx.db.version("secrets_version")
        if v == self.sec_v:
            return None
        self.sec_v = v
        return self.ctx.creds()


def make_private(public: PublicClient, creds: dict, required: bool = True):
    s = creds
    if not s["api_key"] or not s["secret_key"]:
        if required:
            raise SystemExit("API key Indodax belum diisi. Isi lewat halaman Pengaturan di dashboard web.")
        return None
    api = PrivateClientV2(s["api_key"], s["secret_key"], public)
    api.sync_time()
    return api


def cmd_check(ctx, args):
    cfg = ctx.cfg
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
        pc = pair_cfg(cfg, p)
        ok = "OK" if pc["risk"]["idr_per_trade"] >= info.min_idr else "X  (modal per transaksi < minimum!)"
        ok += f" | SL {pc['exits']['stop_loss_pct']}% TP {pc['exits']['take_profit_pct']}%"
        line = f"  {p:<10} min order {rp(info.min_idr):>12}  {ok}"
        if q:
            line += f" | harga {px(q.last)} | spread {q.spread_pct:.2f}% | vol 24j {rp(q.vol_idr)}"
            if q.spread_pct > cfg["risk"]["max_spread_pct"] or q.vol_idr < cfg["risk"]["min_24h_volume_idr"]:
                line += "  (saat ini tidak lolos filter spread/volume)"
        print(line)

    print("\n== API key (TAPI v2) ==")
    try:
        creds = ctx.creds()
    except SecretError as e:
        print(f"  X {e}")
        creds = {"api_key": "", "secret_key": "", "telegram_token": "", "telegram_chat_id": ""}
    api = make_private(public, creds, required=False)
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
    s = creds
    n = Notifier(s["telegram_token"], s["telegram_chat_id"], cfg["telegram"]["enabled"])
    if n.enabled:
        n.send("Tes notifikasi dari bot Indodax ✅")
        print("  Pesan tes dikirim — cek Telegram Anda.")
    else:
        print("  (nonaktif)")
    print(f"\nMode saat ini: {cfg['mode'].upper()}")


def cmd_run(ctx, args):
    cfg = ctx.cfg
    data_dir = Path(cfg["data_dir"])
    lock = open(data_dir / f"bot_{cfg['mode']}.lock", "w")
    try:
        try:
            import fcntl  # Linux / macOS (VPS)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt  # Windows (uji coba di PC)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise SystemExit("Bot dengan mode yang sama sudah berjalan (file lock terkunci).")

    public = PublicClient()
    market = Market(public)
    store = StateStore(cfg["data_dir"], cfg["mode"])
    s = ctx.creds()
    notifier = Notifier(s["telegram_token"], s["telegram_chat_id"], cfg["telegram"]["enabled"],
                        prefix="[SIMULASI] " if cfg["mode"] == "paper" else "")
    if cfg["mode"] == "live":
        if not args.yes_live:
            print("MODE LIVE: bot akan memakai uang sungguhan.")
            if input("Ketik 'LIVE' untuk melanjutkan: ").strip() != "LIVE":
                raise SystemExit("Dibatalkan.")
        api = make_private(public, s)
        acc = api.account()
        if not acc.get("canTrade"):
            raise SystemExit("API key tidak punya izin trade (canTrade=false).")
        broker = LiveBroker(cfg, api)
    else:
        broker = PaperBroker(cfg, {})
    engine = Engine(cfg, market, broker, store, notifier, config_source=DbConfigSource(ctx))
    signal.signal(signal.SIGTERM, engine.stop)
    signal.signal(signal.SIGINT, engine.stop)
    logging.getLogger("bot").info("Memulai bot v%s mode=%s", __version__, cfg["mode"])
    engine.run()
    if engine.exit_code:
        sys.exit(engine.exit_code)  # systemd akan menyalakan ulang dengan mode baru


def cmd_status(ctx, args):
    cfg = ctx.cfg
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


def cmd_backtest(ctx, args):
    cfg = ctx.cfg
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


def _ask_password(prompt="Password baru (min. 10 karakter): "):
    import getpass
    while True:
        pw = getpass.getpass(prompt)
        if len(pw) < 10:
            print("Terlalu pendek, minimal 10 karakter.")
            continue
        if getpass.getpass("Ulangi password: ") != pw:
            print("Tidak sama, coba lagi.")
            continue
        return pw


def ensure_master_key(env_path: Path) -> bool:
    """Buat BOT_MASTER_KEY di .env jika belum ada. True jika baru dibuat."""
    import os
    if os.environ.get("BOT_MASTER_KEY", "").strip():
        return False
    key = generate_master_key()
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = [ln for ln in existing.splitlines() if not ln.startswith("BOT_MASTER_KEY=")]
    lines += ["", "# Kunci enkripsi kredensial di database. JANGAN hilang / dibagikan.",
              f"BOT_MASTER_KEY={key}"]
    env_path.write_text("\n".join(lines).lstrip("\n") + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    os.environ["BOT_MASTER_KEY"] = key
    return True


def cmd_init(args):
    import os
    load_env(args.env)
    env_path = Path(args.env)
    if not os.environ.get("BOT_MASTER_KEY", "").strip() and not getattr(args, "reset_secrets", False):
        boot = load_bootstrap(args.config)
        probe = Database.from_env(boot["data_dir"])
        probe.create_all()
        if probe.secret_info():
            raise SystemExit(
                "Database sudah berisi kredensial terenkripsi, tetapi BOT_MASTER_KEY tidak ada di .env.\n"
                "Kembalikan BOT_MASTER_KEY lama dari cadangan Anda. Jika kunci benar-benar hilang, hapus kredensial\n"
                "lama (python -m bot init --reset-secrets) lalu masukkan ulang API key lewat dashboard.")
    if getattr(args, "reset_secrets", False):
        boot = load_bootstrap(args.config)
        probe = Database.from_env(boot["data_dir"])
        probe.create_all()
        for name in list(probe.secret_info()):
            probe.delete_secret(name)
        print("Kredensial lama dihapus dari database.")
    if ensure_master_key(env_path):
        print(f"Kunci enkripsi baru dibuat di {env_path} (BOT_MASTER_KEY). Simpan cadangannya di tempat aman.")
    ctx = Ctx(args)
    print(f"Database: {ctx.db.url.split('@')[-1]}")
    moved = import_env_secrets(ctx.db)
    if moved:
        print(f"Kredensial dari .env dipindahkan ke database (terenkripsi): {', '.join(moved)}.")
        print("Anda boleh menghapus nilai tersebut dari .env.")
    if ctx.db.count_users() == 0:
        print("\nBuat akun admin untuk login dashboard web.")
        name = input("Username [admin]: ").strip() or "admin"
        ctx.db.add_user(name, _ask_password())
        ctx.db.audit(name, "user_add", "akun admin dibuat via CLI")
        print(f"Akun '{name}' dibuat.")
    print("\nSelesai. Jalankan dashboard: python -m bot web  — lalu atur pair, stop loss, dan API key di menu Pengaturan.")


def cmd_user(ctx, args):
    db = ctx.db
    if args.action == "list":
        for u in db.list_users():
            last = fmt_time(u["last_login"]) if u["last_login"] else "belum pernah"
            print(f"  {u['username']:<20} login terakhir: {last}")
        return
    if not args.name:
        raise SystemExit("Sebutkan username, mis. python -m bot user add admin")
    if args.action == "add":
        if any(u["username"] == args.name for u in db.list_users()):
            raise SystemExit("Username sudah ada. Pakai `user passwd` untuk mengganti password.")
        db.add_user(args.name, _ask_password())
        db.audit(args.name, "user_add", "via CLI")
        print(f"Akun '{args.name}' dibuat.")
    elif args.action == "passwd":
        if not db.set_password(args.name, _ask_password()):
            raise SystemExit("Username tidak ditemukan.")
        db.audit(args.name, "password_change", "via CLI")
        print("Password diganti. Semua sesi login akun ini dikeluarkan.")


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
    i = sub.add_parser("init")
    i.add_argument("--reset-secrets", action="store_true", help="hapus kredensial terenkripsi lama (jika kunci hilang)")
    u = sub.add_parser("user")
    u.add_argument("action", choices=["add", "passwd", "list"])
    u.add_argument("name", nargs="?")
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

    if args.cmd == "init":
        return cmd_init(args)
    ctx = Ctx(args)
    cfg = ctx.cfg

    if args.cmd == "user":
        cmd_user(ctx, args)
    elif args.cmd == "check":
        cmd_check(ctx, args)
    elif args.cmd == "run":
        cmd_run(ctx, args)
    elif args.cmd == "status":
        cmd_status(ctx, args)
    elif args.cmd == "backtest":
        cmd_backtest(ctx, args)
    elif args.cmd == "web":
        from .web import serve
        serve(ctx, args.host, args.port)
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
